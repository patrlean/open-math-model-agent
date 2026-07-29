"""Provider abstraction layer.

The agent talks to LLMs only through this interface, so the backend model can be
swapped (DeepSeek / OpenAI / Anthropic / local) without touching agent logic.

Internally we standardize on the OpenAI-style message + tool-call schema, since
most providers are OpenAI-compatible. Non-compatible providers (e.g. Anthropic)
convert to/from this shape inside their own adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import time
import uuid
from typing import Any, Callable, Iterator


RequestObserver = Callable[[str, dict[str, Any]], None]
_REQUEST_CONTEXT: ContextVar[dict[str, Any]] = ContextVar(
    "model_request_context",
    default={},
)


@contextmanager
def model_request_context(**metadata: Any) -> Iterator[None]:
    """Attach concurrency-safe Agent identity to provider requests."""
    token = _REQUEST_CONTEXT.set({
        **_REQUEST_CONTEXT.get(),
        **{key: value for key, value in metadata.items() if value is not None},
    })
    try:
        yield
    finally:
        _REQUEST_CONTEXT.reset(token)


@dataclass
class ToolCall:
    """A single tool/function call requested by the model."""

    id: str
    name: str
    arguments: str  # raw JSON string as returned by the model


@dataclass
class Usage:
    """Token accounting for one request, read from the provider's response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    uncached_input_tokens: int = 0
    unclassified_input_tokens: int = 0
    estimated_cost_cny: float = 0.0
    priced_tokens: int = 0
    unpriced_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.total_tokens + other.total_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.uncached_input_tokens + other.uncached_input_tokens,
            self.unclassified_input_tokens + other.unclassified_input_tokens,
            self.estimated_cost_cny + other.estimated_cost_cny,
            self.priced_tokens + other.priced_tokens,
            self.unpriced_tokens + other.unpriced_tokens,
        )

    def to_dict(self) -> dict[str, int | float | bool]:
        """Serialize the cumulative usage without losing cache provenance."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "unclassified_input_tokens": self.unclassified_input_tokens,
            "estimated_cost_cny": round(self.estimated_cost_cny, 8),
            "priced_tokens": self.priced_tokens,
            "unpriced_tokens": self.unpriced_tokens,
            "cache_breakdown_complete": self.unclassified_input_tokens == 0,
            "pricing_complete": self.unpriced_tokens == 0,
        }


def usage_from_api(raw_usage: Any) -> Usage:
    """Normalize OpenAI-compatible usage fields, including vendor cache fields.

    DeepSeek exposes ``prompt_cache_hit_tokens`` and
    ``prompt_cache_miss_tokens``. OpenAI-style providers commonly expose
    ``prompt_tokens_details.cached_tokens`` instead. When neither is present,
    input tokens remain explicitly unclassified rather than being silently
    treated as cache misses.
    """
    prompt = int(getattr(raw_usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(raw_usage, "completion_tokens", 0) or 0)
    total = int(getattr(raw_usage, "total_tokens", 0) or 0)
    if not total:
        total = prompt + completion

    hit_raw = getattr(raw_usage, "prompt_cache_hit_tokens", None)
    miss_raw = getattr(raw_usage, "prompt_cache_miss_tokens", None)
    details = getattr(raw_usage, "prompt_tokens_details", None)
    details_cached = (
        getattr(details, "cached_tokens", None)
        if details is not None else None
    )

    if hit_raw is not None or miss_raw is not None:
        cached = max(0, int(hit_raw or 0))
        uncached = max(
            0,
            int(miss_raw) if miss_raw is not None else prompt - cached,
        )
        unclassified = max(0, prompt - cached - uncached)
    elif details_cached is not None:
        cached = max(0, int(details_cached or 0))
        uncached = max(0, prompt - cached)
        unclassified = 0
    else:
        cached = 0
        uncached = 0
        unclassified = prompt

    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cached_input_tokens=cached,
        uncached_input_tokens=uncached,
        unclassified_input_tokens=unclassified,
        unpriced_tokens=total,
    )


@dataclass
class ChatResponse:
    """Normalized model response, provider-independent."""

    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    raw: Any = None  # underlying SDK object, for debugging
    reasoning_content: str | None = None


class Provider(ABC):
    """Base class for all LLM backends."""

    def __init__(self, model: str, **kwargs: Any) -> None:
        self.model = model
        self.request_observer: RequestObserver | None = kwargs.get(
            "request_observer"
        )

    def _begin_request(
        self,
        params: dict[str, Any],
        *,
        transport_attempt: int = 1,
        retry_reason: str | None = None,
    ) -> tuple[str, float]:
        request_id = uuid.uuid4().hex
        started = time.time()
        observer = getattr(self, "request_observer", None)
        if observer is not None:
            try:
                detached = json.loads(json.dumps(
                    params,
                    ensure_ascii=False,
                    default=str,
                ))
                observer("request", {
                    "request_id": request_id,
                    "ts": started,
                    "provider": type(self).__name__,
                    "model": self.model,
                    "transport_attempt": transport_attempt,
                    "retry_reason": retry_reason,
                    "context": dict(_REQUEST_CONTEXT.get()),
                    "params": detached,
                })
            except Exception:
                # Debug logging must never prevent a model request.
                pass
        return request_id, started

    def _finish_request(
        self,
        request_id: str,
        started: float,
        *,
        usage: Usage | None = None,
        finish_reason: str | None = None,
        error: Exception | str | None = None,
    ) -> None:
        observer = getattr(self, "request_observer", None)
        if observer is None:
            return
        finished = time.time()
        try:
            observer("response", {
                "request_id": request_id,
                "ts": finished,
                "duration_seconds": round(finished - started, 6),
                "status": "error" if error is not None else "completed",
                "usage": (usage or Usage()).to_dict(),
                "finish_reason": finish_reason,
                "error": (
                    f"{type(error).__name__}: {error}"
                    if isinstance(error, Exception)
                    else error
                ),
            })
        except Exception:
            pass

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """Send a chat request and return a normalized response.

        `messages` and `tools` use the OpenAI schema. `kwargs` (temperature,
        max_tokens, ...) are passed through to the underlying call.
        """
        raise NotImplementedError
