"""Deterministic checks for model request context capture and classification."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from mathmodel.agent.loop import Agent
from mathmodel.context_inspector import server as inspector_server
from mathmodel.contextlog import (
    CONTEXT_INDEX_FILENAME,
    ContextRecorder,
    build_context_index,
    classify_request,
    context_log_stats,
    read_context_request,
    read_context_request_summaries,
    read_context_requests,
    request_detail,
)
from mathmodel.providers.base import ChatResponse, Provider, ToolCall, Usage
from mathmodel.tools.base import Tool, ToolContext, ToolRegistry
from mathmodel.tool_metrics import conversation_tool_metrics


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

        index_path = run / CONTEXT_INDEX_FILENAME
        assert index_path.is_file()
        indexed_summaries = read_context_request_summaries(
            run / "context_requests.jsonl"
        )
        assert [item["sequence"] for item in indexed_summaries] == [1, 2]
        assert indexed_summaries[1]["status"] == "completed"
        indexed_record = read_context_request(
            run / "context_requests.jsonl",
            records[1]["request_id"],
        )
        assert indexed_record is not None
        assert indexed_record["params"] == records[1]["params"]
        assert context_log_stats(run / "context_requests.jsonl") == {
            "request_count": 2,
            "latest_request_ts": records[1]["ts"],
            "latest_model": "fake-context-model",
        }

        # Legacy logs self-migrate once, then serve summaries and details from
        # compact index records plus direct byte-offset reads.
        index_path.unlink()
        assert build_context_index(
            run / "context_requests.jsonl"
        ) == index_path
        assert [
            item["sequence"]
            for item in read_context_request_summaries(
                run / "context_requests.jsonl"
            )
        ] == [1, 2]

        # A reader can race a recorder in another process. An incomplete tail
        # must remain pending and become indexable once its newline arrives.
        partial_log = workspace / "partial-context.jsonl"
        partial_record = json.dumps({
            "kind": "request",
            "request_id": "partial-request",
            "sequence": 1,
            "ts": 123.0,
            "params": {"model": "partial-model", "messages": []},
            "context": {},
        }).encode("utf-8")
        split_at = len(partial_record) // 2
        partial_log.write_bytes(partial_record[:split_at])
        assert read_context_request_summaries(partial_log) == []
        with partial_log.open("ab") as handle:
            handle.write(partial_record[split_at:] + b"\n")
        partial_summaries = read_context_request_summaries(partial_log)
        assert len(partial_summaries) == 1
        assert partial_summaries[0]["request_id"] == "partial-request"

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
            served = inspector_server.get_request(
                "test-run",
                records[1]["request_id"],
            )
            assert served["items"] == second["items"]

            # Tool metrics use the unique event stream, not cumulative API
            # contexts. Exercise main/sub-agent/verifier classification and the
            # specialized LaTeX/verdict outcomes deterministically.
            metric_events = [
                {"kind": "assistant", "tool_calls": [["run_code", "{}"]]},
                {"kind": "tool_result", "name": "run_code", "observation": "exit_code=1 timed_out=False"},
                {"kind": "assistant", "tool_calls": [["run_code", "{}"]]},
                {"kind": "tool_result", "name": "run_code", "observation": "exit_code=0 timed_out=False"},
                {"kind": "assistant", "subagent": 1, "tool_calls": [["edit_paragraph", "{}"]]},
                {"kind": "tool_result", "subagent": 1, "name": "edit_paragraph", "observation": "localized edit compiled and paper acceptance PASSED -> paper/main.pdf"},
                {"kind": "verification_progress", "phase": "assistant", "role": "verifier", "attempt": 1, "tool_calls": [["submit_verification", "{}"]]},
                {"kind": "verification_progress", "phase": "tool_result", "role": "verifier", "attempt": 1, "name": "submit_verification", "observation": "Verification verdict recorded."},
                {"kind": "assistant", "tool_calls": [["read_file", "{}"]]},
            ]
            events_path = run / "events.jsonl"
            events_path.write_text("".join(
                json.dumps(event) + "\n" for event in metric_events
            ))
            metrics = inspector_server.get_tool_metrics("test-run")
            assert metrics == conversation_tool_metrics(
                events_path,
                run_status="done",
            )
            overall = metrics["groups"]["all"]["summary"]
            assert overall["total_calls"] == 5
            assert overall["completed_calls"] == 4
            assert overall["interrupted_calls"] == 1
            assert overall["objective_successes"] == 3
            assert overall["compile_successes"] == 1
            assert overall["acceptance_successes"] == 1
            assert overall["verdict_successes"] == 1
            assert overall["retry_attempts"] == 1
            assert overall["recovered_retries"] == 1
            assert metrics["groups"]["main"]["summary"]["total_calls"] == 3
            assert metrics["groups"]["subagent"]["summary"]["total_calls"] == 1
            assert metrics["groups"]["verifier"]["summary"]["total_calls"] == 1
        finally:
            inspector_server.WORKSPACE = original_workspace

    print("context inspector capture checks: passed")


if __name__ == "__main__":
    main()
