"""Regression checks for verification triggering on dashboard follow-ups."""

from __future__ import annotations

import tempfile
from pathlib import Path

from mathmodel.agent.loop import Agent
from mathmodel.providers.base import ChatResponse, Provider, ToolCall, Usage
from mathmodel.tools.base import Tool, ToolContext, ToolRegistry


class SequenceProvider(Provider):
    def __init__(self, responses: list[ChatResponse]) -> None:
        super().__init__("fake")
        self.responses = list(responses)

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        return self.responses.pop(0)


def _response(
    text: str | None,
    tool_calls: list[ToolCall] | None = None,
) -> ChatResponse:
    return ChatResponse(
        text=text,
        tool_calls=tool_calls or [],
        usage=Usage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
    )


def _agent(
    workdir: Path,
    responses: list[ChatResponse],
    verifier_calls: list[tuple[str, int]],
) -> Agent:
    registry = ToolRegistry()
    registry.register(Tool(
        name="run_code",
        description="fake mutating computation",
        parameters={"type": "object", "properties": {}},
        handler=lambda _ctx, _args: "exit_code=0",
    ))

    def verify(candidate: str, attempt: int) -> dict:
        verifier_calls.append((candidate, attempt))
        return {
            "verdict": "PASS",
            "summary": "ok",
            "issues": [],
            "attempt": attempt,
        }

    return Agent(
        provider=SequenceProvider(responses),
        registry=registry,
        ctx=ToolContext(workdir=workdir, sandbox=None),
        system_prompt="test",
        max_steps=3,
        final_verifier=verify,
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        workdir = Path(temporary)

        clarification_verifier_calls: list[tuple[str, int]] = []
        clarification = _agent(
            workdir,
            [_response("没有，我使用的是圆柱体几何。")],
            clarification_verifier_calls,
        )
        result = clarification.run(
            "你把真目标简化成一个点了吗？",
            verify_on_completion=False,
        )
        assert "圆柱体" in result
        assert clarification_verifier_calls == []

        fresh_verifier_calls: list[tuple[str, int]] = []
        fresh = _agent(
            workdir,
            [_response("初始建模结果")],
            fresh_verifier_calls,
        )
        fresh.run("求解问题", verify_on_completion=True)
        assert len(fresh_verifier_calls) == 1

        revision_verifier_calls: list[tuple[str, int]] = []
        revision = _agent(
            workdir,
            [
                _response(None, [ToolCall("call-1", "run_code", "{}")]),
                _response("已根据质疑重新计算并修订。"),
            ],
            revision_verifier_calls,
        )
        revision.run("请修正模型", verify_on_completion=False)
        assert len(revision_verifier_calls) == 1

    print("follow-up verification trigger checks: passed")


if __name__ == "__main__":
    main()
