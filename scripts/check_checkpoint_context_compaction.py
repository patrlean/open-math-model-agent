"""Deterministic regression checks for checkpoint + policy-pruning V2."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from mathmodel.agent.context_compaction import (
    CHECKPOINT_SUMMARY_SYSTEM_PROMPT,
    CHECKPOINT_TOOL_PRUNING_V2,
    EXECUTION_CHECKPOINT_HEADER,
    READ_RESULT_REPLAY_HEADER,
    TOOL_RESULT_PRUNED_HEADER,
    prune_tool_results_by_policy,
)
from mathmodel.agent.loop import Agent
from mathmodel.experiment import CONTEXT_PROFILES
from mathmodel.providers.base import ChatResponse, Provider, Usage
from mathmodel.tools.base import ToolContext, ToolRegistry


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments),
            },
        }],
    }


class CheckpointProvider(Provider):
    def __init__(self) -> None:
        super().__init__("checkpoint-fake")
        self.requests: list[list[dict]] = []

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        self.requests.append(json.loads(json.dumps(messages)))
        index = len(self.requests)
        return ChatResponse(
            text=(
                "## Goal\nFinish the paper.\n"
                "## Constraints and Preferences\nKeep exact user requirements.\n"
                "## Progress\n### Done\n- inspected data\n"
                "### In Progress\n- modeling\n### Blocked\n- none\n"
                "## Key Decisions\n- use verified files\n"
                "## Failed Attempts\n- none\n"
                f"## Critical Facts\n- checkpoint version {index}\n"
                "## Relevant Files and Artifacts\n- results/value.json\n"
                "## Next Steps\n1. Finish model."
            ),
            usage=Usage(prompt_tokens=200, completion_tokens=80, total_tokens=280),
        )


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        workdir = Path(temporary)
        messages = [
            _call("read-1", "read_file", {"path": "src/large.py", "start_line": 1}),
            {
                "role": "tool",
                "tool_call_id": "read-1",
                "content": (
                    "path: src/large.py\nsha256: abc\ntotal_lines: 900\n"
                    "returned_lines: 1-500\n" + "source line\n" * 1_500
                ),
            },
            _call("run-1", "run_code", {"code": "print('x')"}),
            {
                "role": "tool",
                "tool_call_id": "run-1",
                "content": "program output\n" + "x" * 5_000 + "\nTRACEBACK-END",
            },
            _call("paper-1", "write_paper", {"title": "paper"}),
            {
                "role": "tool",
                "tool_call_id": "paper-1",
                "content": "paper mutation result\n" + "p" * 8_000,
            },
            _call("recent-1", "run_code", {"code": "print('recent')"}),
            {
                "role": "tool",
                "tool_call_id": "recent-1",
                "content": "recent\n" + "r" * 8_000,
            },
        ]
        pruned, metrics = prune_tool_results_by_policy(
            messages,
            workdir=workdir,
            level="aggressive",
            recent_tool_results=1,
            prune_index=1,
        )
        assert metrics["pruned_tool_results"] == 2
        assert metrics["reference_only_tool_results"] == 1
        assert metrics["archived_tool_results"] == 1
        assert metrics["tool_result_tokens_saved_estimate"] > 0
        assert pruned[1]["content"].startswith(READ_RESULT_REPLAY_HEADER)
        assert pruned[3]["content"].startswith(TOOL_RESULT_PRUNED_HEADER)
        assert "TRACEBACK-END" in pruned[3]["content"], "run_code tail must survive"
        assert pruned[5] == messages[5], "write_paper result must never be pruned"
        assert pruned[7] == messages[7], "newest Tool Result must remain raw"
        logs = list((workdir / "tool_result_logs").glob("*.txt"))
        assert len(logs) == 1
        assert logs[0].read_text().endswith("TRACEBACK-END")

        repeated, repeated_metrics = prune_tool_results_by_policy(
            pruned,
            workdir=workdir,
            level="aggressive",
            recent_tool_results=1,
            prune_index=2,
        )
        assert repeated == pruned
        assert repeated_metrics["pruned_tool_results"] == 0
        assert len(list((workdir / "tool_result_logs").glob("*.txt"))) == 1

    events: list[tuple[str, dict]] = []
    with tempfile.TemporaryDirectory() as temporary:
        provider = CheckpointProvider()
        context = ToolContext(
            workdir=Path(temporary),
            sandbox=None,  # type: ignore[arg-type]
            settings={
                "context": {
                    "working_memory_mode": "replace",
                    "tool_prune_threshold_tokens": 1,
                    "tool_prune_aggressive_threshold_tokens": 1,
                    "tool_prune_recent_results": 0,
                }
            },
        )
        agent = Agent(
            provider,
            ToolRegistry(),
            context,
            "SYSTEM",
            compact_threshold_tokens=1,
            keep_tail_messages=2,
            compaction_strategy=CHECKPOINT_TOOL_PRUNING_V2,
            on_event=lambda kind, data: events.append((kind, data)),
        )
        agent.messages = agent.messages[:2] + [
            {"role": "user", "content": "Exact historical requirement."},
            _call("old-read", "read_file", {"path": "results/value.json"}),
            {
                "role": "tool",
                "tool_call_id": "old-read",
                "content": (
                    "path: results/value.json\nsha256: 123\ntotal_lines: 800\n"
                    "returned_lines: 1-500\n" + "42\n" * 2_000
                ),
            },
            {"role": "assistant", "content": "Old analysis complete."},
            {"role": "user", "content": "Recent request."},
            {"role": "assistant", "content": "Recent response."},
        ]
        agent.context_tokens = 256_000
        agent._maybe_compact()
        assert len(provider.requests) == 1
        request = provider.requests[0]
        assert request[0]["content"] == CHECKPOINT_SUMMARY_SYSTEM_PROMPT
        request_text = request[1]["content"]
        assert "Exact historical requirement." in request_text
        assert READ_RESULT_REPLAY_HEADER in request_text
        assert request_text.count("42\n") < 100
        checkpoints = [
            message for message in agent.messages
            if str(message.get("content") or "").startswith(
                EXECUTION_CHECKPOINT_HEADER
            )
        ]
        assert len(checkpoints) == 1
        assert "checkpoint version 1" in checkpoints[0]["content"]

        agent.messages.extend([
            {"role": "user", "content": "New exact constraint."},
            {"role": "assistant", "content": "Apply the new constraint."},
            {"role": "user", "content": "Second recent request."},
            {"role": "assistant", "content": "Second recent response."},
        ])
        agent.context_tokens = 256_000
        agent._maybe_compact()
        assert len(provider.requests) == 2
        second_request = provider.requests[1][1]["content"]
        assert "checkpoint version 1" in second_request
        assert "New exact constraint." in second_request
        checkpoints = [
            message for message in agent.messages
            if str(message.get("content") or "").startswith(
                EXECUTION_CHECKPOINT_HEADER
            )
        ]
        assert len(checkpoints) == 1, "mutable checkpoint must replace, not append"
        assert "checkpoint version 2" in checkpoints[0]["content"]
        assert "checkpoint version 1" not in checkpoints[0]["content"]
        assert any(kind == "tool_prune_done" for kind, _ in events)
        assert sum(kind == "compact_done" for kind, _ in events) == 2

    combined = CONTEXT_PROFILES["checkpoint-pruning-256k"]
    assert combined["compaction_strategy"] == CHECKPOINT_TOOL_PRUNING_V2
    assert combined["tool_prune_threshold_tokens"] == 166_400
    assert combined["tool_prune_aggressive_threshold_tokens"] == 204_800
    assert CONTEXT_PROFILES["checkpoint-summary-256k"]["compaction_strategy"] == (
        "checkpoint_summary_v2"
    )
    assert CONTEXT_PROFILES["policy-pruning-control"]["compaction_strategy"] == (
        "policy_tool_pruning_v2"
    )

    print("checkpoint context compaction checks passed")


if __name__ == "__main__":
    main()
