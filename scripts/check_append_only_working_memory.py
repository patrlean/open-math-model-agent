"""Deterministic append-only Working Memory protocol check (no network).

Run: ./.venv/bin/python -m scripts.check_append_only_working_memory
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from mathmodel.agent.loop import Agent, load_agent_state
from mathmodel.contextlog import classify_request
from mathmodel.providers.base import ChatResponse, Provider, Usage
from mathmodel.tools.base import ToolContext, ToolRegistry


class FakeProvider(Provider):
    def __init__(self, model: str) -> None:
        super().__init__(model)
        self.requests: list[list[dict]] = []

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        self.requests.append(json.loads(json.dumps(messages)))
        return ChatResponse(
            text="SUMMARY: preserved durable state and recent tool evidence.",
            tool_calls=[],
            usage=Usage(10, 5, 15),
        )


def _snapshot_messages(agent: Agent) -> list[dict]:
    return [
        message
        for message in agent.messages
        if str(message.get("content") or "").startswith(
            "[working memory snapshot — append-only-v1"
        )
    ]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        (workdir / "results").mkdir()
        (workdir / "results" / "initial.json").write_text('{"value": 1}')
        (workdir / "decisions.md").write_text("# Decisions\n\n- use baseline A")
        state_path = workdir / "session_state.json"
        settings = {
            "context": {
                "working_memory_mode": "append_only",
                "compact_threshold_tokens": 1,
            }
        }
        context = ToolContext(
            workdir=workdir,
            sandbox=None,  # type: ignore[arg-type]
            settings=settings,
        )
        probe_provider = FakeProvider("fake")
        probe = Agent(
            probe_provider,
            ToolRegistry(),
            context,
            "SYSTEM",
            max_steps=1,
        )
        probe.run("first user request", verify_on_completion=False)
        first_request = probe_provider.requests[0]
        assert [message["role"] for message in first_request] == [
            "system",
            "system",
            "user",
            "system",
        ]
        assert "working memory protocol" in first_request[1]["content"]
        assert "working memory snapshot" in first_request[3]["content"]
        print("[0] API request orders protocol, user input, then appended snapshot")

        agent = Agent(
            FakeProvider("fake"),
            ToolRegistry(),
            context,
            "SYSTEM",
            compact_threshold_tokens=1,
            keep_tail_messages=4,
            state_path=state_path,
        )

        protocol = json.loads(json.dumps(agent.messages[1]))
        assert "working memory protocol" in protocol["content"]
        assert agent._refresh_working_memory() is True
        assert agent._wm_version == 1
        immutable_prefix = json.loads(json.dumps(agent.messages))
        assert agent._refresh_working_memory() is False
        assert agent.messages == immutable_prefix
        print("[1] stable protocol + digest suppress unchanged snapshots")

        # A changed state cannot be inserted between a tool call and its result.
        (workdir / "decisions.md").write_text(
            "# Decisions\n\n- use baseline A\n- reject method B"
        )
        agent.messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "run_code", "arguments": "{}"},
            }],
        })
        assert agent._refresh_working_memory() is False
        assert agent.messages[-1]["role"] == "assistant"
        agent.messages.append({
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "exit_code=0",
        })
        before_append = json.loads(json.dumps(agent.messages))
        assert agent._refresh_working_memory() is True
        assert agent.messages[:-1] == before_append
        assert agent.messages[-2]["role"] == "tool"
        assert agent._wm_version == 2
        assert "reject method B" in agent.messages[-1]["content"]
        print("[2] changed memory appends only after the complete tool batch")

        agent._save_state()
        persisted = load_agent_state(state_path)
        assert persisted is not None
        resumed = Agent(
            FakeProvider("fake"),
            ToolRegistry(),
            context,
            "SYSTEM",
            compact_threshold_tokens=1,
            keep_tail_messages=4,
            initial_messages=persisted["messages"],
            initial_runtime_controls=persisted["runtime_controls"],
        )
        assert resumed._wm_epoch == 1 and resumed._wm_version == 2
        (workdir / "results" / "next.json").write_text('{"value": 2}')
        assert resumed._refresh_working_memory() is True
        assert resumed._wm_version == 3
        print("[3] checkpoint resume continues the epoch/version sequence")

        # Add ordinary paired history, then compact. Old snapshots disappear,
        # the immutable protocol survives, and one complete snapshot starts E2.
        for index in range(8):
            resumed.messages.append({"role": "user", "content": f"turn {index}"})
            resumed.messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"compact-{index}",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            })
            resumed.messages.append({
                "role": "tool",
                "tool_call_id": f"compact-{index}",
                "content": f"result {index}",
            })
        resumed.context_tokens = 100
        resumed._maybe_compact()
        assert resumed.messages[1] == protocol
        snapshots = _snapshot_messages(resumed)
        assert len(snapshots) == 1
        assert "epoch: 2" in snapshots[0]["content"]
        assert "version: 1" in snapshots[0]["content"]
        assert "results/next.json" in snapshots[0]["content"]
        assert resumed.messages[-1] is snapshots[0]
        print("[4] compaction opens a new epoch with one complete snapshot")

        items = classify_request({
            "params": {
                "model": "fake",
                "messages": resumed.messages,
                "tools": [{
                    "type": "function",
                    "function": {"name": "read_file"},
                }],
            },
            "context": {"system_prompt_source": "test"},
        })
        memory_items = [
            item for item in items if item["category"] == "working_memory"
        ]
        assert [item["metadata"]["memory_kind"] for item in memory_items] == [
            "protocol",
            "snapshot",
        ]
        categories = [item["category"] for item in items]
        assert categories.index("tool_definition") == (
            categories.index("working_memory") + 1
        )
        assert any(item["category"] == "tool_result" for item in items)
        print("[5] Context Inspector distinguishes protocol/snapshot/tool traffic")

    print("\nOK: append-only Working Memory protocol invariants pass.")


if __name__ == "__main__":
    main()
