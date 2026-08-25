"""DeepSeek provider adapter.

DeepSeek exposes an OpenAI-compatible API, so we reuse the `openai` SDK and only
point it at DeepSeek's base_url. Token usage is read from the response's `usage`
field (used by the context manager to decide when to compact).
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from openai import BadRequestError, OpenAI

from .base import ChatResponse, Provider, ToolCall, Usage, usage_from_api

DEFAULT_BASE_URL = "https://api.deepseek.com"
_TOOL_CHOICE_SUPPORT: dict[tuple[str, str], bool] = {}
_TOOL_CHOICE_SUPPORT_LOCK = threading.Lock()


def _clock_minutes(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":", 1))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(value)
    return hour * 60 + minute


def deepseek_request_pricing(
    pricing: dict[str, Any],
    model: str,
    request_started_at: float,
) -> tuple[dict[str, float], float]:
    """Resolve model rates and the Beijing-time multiplier for one request."""
    configured = pricing.get("deepseek_cny_per_million") or {}
    candidate = (
        configured.get(model.strip().lower())
        if isinstance(configured, dict) else None
    )
    # Backward compatibility with the former single-model flat rate object.
    rates = candidate if isinstance(candidate, dict) else configured
    normalized_rates = {
        key: float(rates[key])
        for key in ("cached_input", "uncached_input", "output")
        if isinstance(rates, dict) and isinstance(rates.get(key), (int, float))
    }
    if len(normalized_rates) != 3:
        return {}, 1.0

    peak = pricing.get("deepseek_peak") or {}
    if not isinstance(peak, dict) or not peak.get("enabled", False):
        return normalized_rates, 1.0
    try:
        local = datetime.fromtimestamp(
            request_started_at,
            ZoneInfo(str(peak.get("timezone") or "Asia/Shanghai")),
        )
        minute = local.hour * 60 + local.minute
        is_peak = any(
            _clock_minutes(str(start)) <= minute < _clock_minutes(str(end))
            for start, end in peak.get("windows", ())
        )
        multiplier = float(peak.get("multiplier", 2.0)) if is_peak else 1.0
    except (TypeError, ValueError, KeyError):
        multiplier = 1.0
    return normalized_rates, multiplier


def deepseek_usage_cost_cny(
    usage: Usage,
    pricing: dict[str, Any],
    model: str,
    request_started_at: float,
) -> float | None:
    """Price one API response using its model and request-start timestamp."""
    if usage.unclassified_input_tokens != 0:
        return None
    rates, multiplier = deepseek_request_pricing(
        pricing,
        model,
        request_started_at,
    )
    if len(rates) != 3:
        return None
    return (
        usage.cached_input_tokens * rates["cached_input"]
        + usage.uncached_input_tokens * rates["uncached_input"]
        + usage.completion_tokens * rates["output"]
    ) * multiplier / 1_000_000


def _thinking_mode_rejects_tool_choice(exc: BadRequestError) -> bool:
    message = str(exc).lower()
    return "thinking mode" in message and "tool_choice" in message


class DeepSeekProvider(Provider):
    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        pricing: dict[str, Any] | None = None,
        reasoning_effort: str = "high",
        **kwargs: Any,
    ) -> None:
        super().__init__(model, **kwargs)
        resolved_base_url = base_url or DEFAULT_BASE_URL
        self._capability_key = (resolved_base_url, model)
        key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY not set (put it in the project .env file)."
            )
        self.client = OpenAI(api_key=key, base_url=resolved_base_url)
        self.pricing = dict(pricing or {})
        self.reasoning_effort = reasoning_effort

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        params: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            params["tools"] = tools
        params.update(kwargs)
        params.setdefault("reasoning_effort", self.reasoning_effort)

        with _TOOL_CHOICE_SUPPORT_LOCK:
            forced_tool_choice_supported = _TOOL_CHOICE_SUPPORT.get(
                self._capability_key
            )
        if forced_tool_choice_supported is False and "tool_choice" in params:
            params.pop("reasoning_effort", None)
            extra_body = dict(params.get("extra_body") or {})
            extra_body["thinking"] = {"type": "disabled"}
            params["extra_body"] = extra_body

        request_id, started = self._begin_request(params)
        try:
            resp = self.client.chat.completions.create(**params)
        except BadRequestError as exc:
            self._finish_request(request_id, started, error=exc)
            if "tool_choice" not in params or not _thinking_mode_rejects_tool_choice(exc):
                raise
            # DeepSeek thinking models support tools but reject forced
            # ``tool_choice``. The official API supports switching only this
            # finalization request to non-thinking mode, where forced tool choice
            # remains available. Retrying is safe because the rejected request
            # did not run.
            with _TOOL_CHOICE_SUPPORT_LOCK:
                _TOOL_CHOICE_SUPPORT[self._capability_key] = False
            extra_body = dict(params.get("extra_body") or {})
            extra_body["thinking"] = {"type": "disabled"}
            params["extra_body"] = extra_body
            params.pop("reasoning_effort", None)
            request_id, started = self._begin_request(
                params,
                transport_attempt=2,
                retry_reason="thinking_mode_rejected_tool_choice",
            )
            try:
                resp = self.client.chat.completions.create(**params)
            except Exception as retry_exc:
                self._finish_request(request_id, started, error=retry_exc)
                raise
        except Exception as exc:
            self._finish_request(request_id, started, error=exc)
            raise
        choice = resp.choices[0]
        msg = choice.message

        tool_calls: list[ToolCall] = []
        for tc in getattr(msg, "tool_calls", None) or []:
            tool_calls.append(
                ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
            )

        u = getattr(resp, "usage", None)
        usage = usage_from_api(u)
        estimated_cost = deepseek_usage_cost_cny(
            usage,
            getattr(self, "pricing", {}),
            self.model,
            started,
        )
        if estimated_cost is not None:
            usage.estimated_cost_cny = estimated_cost
            usage.priced_tokens = usage.total_tokens
            usage.unpriced_tokens = 0

        self._finish_request(
            request_id,
            started,
            usage=usage,
            finish_reason=choice.finish_reason,
        )

        return ChatResponse(
            text=msg.content,
            reasoning_content=getattr(msg, "reasoning_content", None),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=choice.finish_reason,
            raw=resp,
        )
