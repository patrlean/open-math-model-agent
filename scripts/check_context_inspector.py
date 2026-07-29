"""Deterministic checks for model request context capture and classification."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from mathmodel.agent.loop import Agent
from mathmodel.context_inspector import server as inspector_server
from mathmodel.contextlog import (
    ContextRecorder,
    classify_request,
    read_context_requests,
    request_detail,
)
from mathmodel.providers.base import ChatResponse, Provider, ToolCall, Usage
from mathmodel.tools.base import Tool, ToolContext, ToolRegistry


class InstrumentedFakeProvider(Provider):
    def __init__(self, observer) -> None:
        super().__init__(
            model="fake-context-model",
            request_observer=observer,
        )
        self.calls = 0

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        self.calls += 1
        params = {
            "model": self.model,
            "messages": messages,
            "tools": tools or [],
            **kwargs,
        }
        request_id, started = self._begin_request(params)
        usage = Usage(
            prompt_tokens=120 + self.calls,
            completion_tokens=10,
            total_tokens=130 + self.calls,
            unclassified_input_tokens=120 + self.calls,
            unpriced_tokens=130 + self.calls,
        )
        if self.calls == 1:
            response = ChatResponse(
                text="I will inspect the file.",
                reasoning_content="The user requested a deterministic inspection.",
                tool_calls=[ToolCall(
                    id="tool-1",
                    name="read_file",
                    arguments=json.dumps({"path": "problem.md"}),
                )],
                usage=usage,
                finish_reason="tool_calls",
            )
        else:
            response = ChatResponse(
                text="Inspection complete.",
                usage=usage,
                finish_reason="stop",
            )
        self._finish_request(
            request_id,
            started,
            usage=response.usage,
            finish_reason=response.finish_reason,
        )
        return response


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        run = workspace / "test-run"
        run.mkdir(parents=True)
        (run / "meta.json").write_text(json.dumps({
            "name": "Context test",
            "created": 100,
            "status": "done",
        }))
        recorder = ContextRecorder(
            run / "context_requests.jsonl",
            "test-run",
        )
        provider = InstrumentedFakeProvider(recorder)
        registry = ToolRegistry()
        registry.register(Tool(
            name="read_file",
            description="Read one file.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=lambda _ctx, args: f"contents of {args['path']}",
        ))
        agent = Agent(
            provider=provider,
            registry=registry,
            ctx=ToolContext(workdir=run, sandbox=None),
            system_prompt="You are the system prompt.",
            max_steps=2,
            agent_role="Main Agent",
            system_prompt_source="agent.md",
        )
        result = agent.run("Read the modeling problem.")
        assert result == "Inspection complete."

        records = read_context_requests(run / "context_requests.jsonl")
        assert len(records) == 2
        assert [record["sequence"] for record in records] == [1, 2]
        assert all(record["status"] == "completed" for record in records)
        assert records[0]["usage"]["prompt_tokens"] == 121
        assert records[0]["context"]["agent_role"] == "Main Agent"
        assert records[0]["context"]["system_prompt_source"] == "agent.md"

        first_items = classify_request(records[0])
        first_categories = {item["category"] for item in first_items}
        assert {
            "system_prompt",
            "working_memory",
            "user_input",
            "tool_definition",
        }.issubset(first_categories)
        first_order = [item["category"] for item in first_items]
        assert first_order.index("system_prompt") < first_order.index(
            "working_memory"
        )
        assert first_order.index("working_memory") < first_order.index(
            "tool_definition"
        )
        assert first_order.index("tool_definition") < first_order.index(
            "user_input"
        )
        for role in ("Main Agent", "Subagent 1", "Verification Agent"):
            role_record = {
                **records[0],
                "context": {
                    **records[0]["context"],
                    "agent_role": role,
                },
            }
            role_order = [
                item["category"] for item in classify_request(role_record)
            ]
            assert role_order.index("working_memory") + 1 == role_order.index(
                "tool_definition"
            )

        second = request_detail(records[1])
        second_categories = {item["category"] for item in second["items"]}
        assert {
            "assistant_response",
            "reasoning",
            "tool_call",
            "tool_result",
        }.issubset(second_categories)
        tool_call = next(
            item for item in second["items"]
            if item["category"] == "tool_call"
        )
        assert tool_call["content"]["name"] == "read_file"
        assert tool_call["content"]["arguments"] == {"path": "problem.md"}
        assert second["raw_request"]["messages"][-1]["role"] == "tool"

        original_workspace = inspector_server.WORKSPACE
        try:
            inspector_server.WORKSPACE = workspace
            listed = inspector_server.list_context_runs()
            assert listed[0]["id"] == "test-run"
            assert listed[0]["request_count"] == 2
            summaries = inspector_server.list_run_requests("test-run")
            assert [item["sequence"] for item in summaries] == [2, 1]
            served = inspector_server.get_request("test-run", records[1]["request_id"])
            assert served["items"] == second["items"]
        finally:
            inspector_server.WORKSPACE = original_workspace

    print("context inspector capture checks: passed")


if __name__ == "__main__":
    main()
