"""Provider adapter for OpenAI-compatible Chat Completions APIs."""

from __future__ import annotations

from typing import Any

from openai import OpenAI

from .base import ChatResponse, Provider, ToolCall, usage_from_api


class OpenAICompatibleProvider(Provider):
    """Use a provider that implements the OpenAI Chat Completions contract."""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(model, **kwargs)
        if not api_key:
            raise RuntimeError("当前模型供应商尚未配置 API Key。")
        if not base_url:
            raise RuntimeError("当前模型供应商尚未配置 Base URL。")
        self.client = OpenAI(api_key=api_key, base_url=base_url)

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
        request_id, started = self._begin_request(params)
        try:
            resp = self.client.chat.completions.create(**params)
        except Exception as exc:
            self._finish_request(request_id, started, error=exc)
            raise
        choice = resp.choices[0]
        msg = choice.message

        tool_calls = [
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=tc.function.arguments,
            )
            for tc in (getattr(msg, "tool_calls", None) or [])
        ]
        raw_usage = getattr(resp, "usage", None)
        usage = usage_from_api(raw_usage)
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
