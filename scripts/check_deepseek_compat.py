"""Regression check for DeepSeek thinking-mode tool-choice compatibility."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
from openai import BadRequestError

from mathmodel.providers.base import Provider
from mathmodel.providers.deepseek import (
    DeepSeekProvider,
    _TOOL_CHOICE_SUPPORT,
)


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **params):
        self.calls.append(params)
        thinking_type = (
            (params.get("extra_body") or {})
            .get("thinking", {})
            .get("type")
        )
        if "tool_choice" in params and thinking_type != "disabled":
            response = httpx.Response(
                400,
                request=httpx.Request("POST", "https://api.deepseek.test/chat"),
            )
            raise BadRequestError(
                "Thinking mode does not support this tool_choice",
                response=response,
                body={"error": {"message": "Thinking mode does not support this tool_choice"}},
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    reasoning_content="Checked the available evidence.",
                    tool_calls=[SimpleNamespace(
                        id="submit-1",
                        function=SimpleNamespace(
                            name="submit_verification",
                            arguments='{"verdict":"PASS"}',
                        ),
                    )],
                ),
                finish_reason="tool_calls",
            )],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            ),
        )


def _provider(completions: _FakeCompletions) -> DeepSeekProvider:
    provider = DeepSeekProvider.__new__(DeepSeekProvider)
    Provider.__init__(provider, model="thinking-test")
    provider._capability_key = ("https://api.deepseek.test", "thinking-test")
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    provider.pricing = {}
    provider.reasoning_effort = "high"
    return provider


def main() -> None:
    _TOOL_CHOICE_SUPPORT.clear()
    tools = [{
        "type": "function",
        "function": {
            "name": "submit_verification",
            "description": "Submit the verdict.",
            "parameters": {"type": "object"},
        },
    }]
    forced = {
        "type": "function",
        "function": {"name": "submit_verification"},
    }

    first_client = _FakeCompletions()
    response = _provider(first_client).chat(
        [{"role": "user", "content": "Submit now."}],
        tools=tools,
        tool_choice=forced,
    )
    assert len(first_client.calls) == 2
    assert first_client.calls[0]["tool_choice"] == forced
    assert first_client.calls[1]["tool_choice"] == forced
    assert first_client.calls[1]["extra_body"] == {
        "thinking": {"type": "disabled"}
    }
    assert response.tool_calls[0].name == "submit_verification"
    assert response.reasoning_content == "Checked the available evidence."

    second_client = _FakeCompletions()
    # The capability result is shared across provider instances, so parallel
    # verifier workers do not each incur their own avoidable 400 response.
    response = _provider(second_client).chat(
        [{"role": "user", "content": "Submit again."}],
        tools=tools,
        tool_choice=forced,
    )
    assert len(second_client.calls) == 1
    assert second_client.calls[0]["tool_choice"] == forced
    assert second_client.calls[0]["extra_body"] == {
        "thinking": {"type": "disabled"}
    }
    assert response.tool_calls[0].name == "submit_verification"

    print("OK: forced finalization disables thinking once and caches compatibility.")


if __name__ == "__main__":
    main()
