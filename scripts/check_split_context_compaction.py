"""Deterministic checks for the opt-in split context compaction strategy."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from mathmodel.agent.context_compaction import (
    AGENT_SUMMARY_LEDGER_HEADER,
    AGENT_TRACE_SUMMARY_SYSTEM_PROMPT,
    EXTERNALIZED_TOOL_RESULTS_V1,
    INCREMENTAL_SUMMARY_PRESERVE_THINKING_V1,
    INCREMENTAL_SUMMARY_SYSTEM_PROMPT,
    INCREMENTAL_SUMMARY_V1,
    MERGED_USER_HISTORY_HEADER,
    PRESERVED_THINKING_TRACE_HEADER,
    PRESERVED_TOOL_RESULT_HEADER,
    READ_RESULT_REPLAY_HEADER,
    SPLIT_USER_AGENT_V1,
    TOOL_RESULT_EXTERNALIZED_HEADER,
)
from mathmodel.agent.loop import Agent
from mathmodel.experiment import CONTEXT_PROFILES
from mathmodel.providers.base import ChatResponse, Provider, Usage
from mathmodel.tools.base import ToolContext, ToolRegistry


class SummaryProvider(Provider):
    def __init__(self) -> None:
        super().__init__("summary-fake")
        self.requests: list[list[dict]] = []

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        self.requests.append(json.loads(json.dumps(messages)))
        request_index = len(self.requests)
        return ChatResponse(
            text=(
                f"Progress: delta {request_index}; inspected data.\n"
                "Key facts: results/value.json contains 42.\n"
                "Open work: write the paper."
            ),
            usage=Usage(prompt_tokens=120, completion_tokens=30, total_tokens=150),
        )


def main() -> None:
    events: list[tuple[str, dict]] = []
    with tempfile.TemporaryDirectory() as temporary:
        provider = SummaryProvider()
        context = ToolContext(
            workdir=Path(temporary),
            sandbox=None,  # type: ignore[arg-type]
            settings={"context": {"working_memory_mode": "replace"}},
        )
        agent = Agent(
            provider,
            ToolRegistry(),
            context,
            "SYSTEM",
            compact_threshold_tokens=1,
            keep_tail_messages=3,
            compaction_strategy=SPLIT_USER_AGENT_V1,
            on_event=lambda kind, data: events.append((kind, data)),
        )
        agent.messages = agent.messages[:2] + [
            {"role": "user", "content": "First exact user query."},
            {
                "role": "assistant",
                "content": "I will inspect the data.",
                "reasoning_content": "The key column is probably value.",
                "tool_calls": [{
                    "id": "old-call",
                    "type": "function",
                    "function": {
                        "name": "run_code",
                        "arguments": '{"code":"print(42)"}',
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "old-call",
                "content": "results/value.json written with 42",
            },
            {
                "role": "user",
                "content": "[Orchestrator status — this is not user input]\ncontinue",
            },
            {"role": "user", "content": "Second exact user query."},
            {"role": "assistant", "content": "The calculation is complete."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "tail-call",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "tail-call", "content": "recent result"},
            {"role": "user", "content": "Recent user request."},
            {"role": "assistant", "content": "Recent response."},
        ]
        before = len(agent.messages)
        agent.context_tokens = 256_000
        agent._maybe_compact()

        assert len(provider.requests) == 1, "split strategy must call Summary API once"
        summary_request = provider.requests[0]
        assert summary_request[0]["content"] == AGENT_TRACE_SUMMARY_SYSTEM_PROMPT
        summary_input = summary_request[1]["content"]
        assert "First exact user query." not in summary_input
        assert "Second exact user query." not in summary_input
        assert "The key column is probably value." in summary_input
        assert 'print(42)' in summary_input
        assert "results/value.json written with 42" in summary_input
        assert "Orchestrator status" in summary_input

        merged = agent.messages[2]
        assert merged["role"] == "user"
        assert merged["content"].startswith(MERGED_USER_HISTORY_HEADER)
        assert "First exact user query." in merged["content"]
        assert "Second exact user query." in merged["content"]
        assert agent.messages[3]["role"] == "assistant"
        assert "Progress:" in agent.messages[3]["content"]

        tail = agent.messages[4:]
        assert len(tail) == 4, "tool boundary should expand the requested 3-message tail"
        assert tail[0]["role"] == "assistant"
        assert tail[1]["role"] == "tool"
        assert tail[1]["tool_call_id"] == tail[0]["tool_calls"][0]["id"]
        assert len(agent.messages) < before

        done = next(data for kind, data in events if kind == "compact_done")
        assert done["strategy"] == SPLIT_USER_AGENT_V1
        assert done["summary_calls"] == 1
        assert done["keeping"] == 4
        assert done["context_chars_after"] < done["context_chars_before"]
        assert done["user_merged_chars"] > 0
        assert done["agent_summary_chars"] > 0
        assert done["summary_usage"]["total_tokens"] == 150

    incremental_events: list[tuple[str, dict]] = []
    with tempfile.TemporaryDirectory() as temporary:
        provider = SummaryProvider()
        context = ToolContext(
            workdir=Path(temporary),
            sandbox=None,  # type: ignore[arg-type]
            settings={"context": {"working_memory_mode": "replace"}},
        )
        agent = Agent(
            provider,
            ToolRegistry(),
            context,
            "SYSTEM",
            compact_threshold_tokens=1,
            keep_tail_messages=2,
            compaction_strategy=INCREMENTAL_SUMMARY_V1,
            on_event=lambda kind, data: incremental_events.append((kind, data)),
        )
        agent.messages = agent.messages[:2] + [
            {"role": "user", "content": "Historical exact user question."},
            {
                "role": "assistant",
                "reasoning_content": "First private reasoning.",
                "content": "First response.",
            },
            {"role": "user", "content": "First tail user."},
            {"role": "assistant", "content": "First tail response."},
        ]
        agent.context_tokens = 256_000
        agent._maybe_compact()
        assert len(provider.requests) == 1
        first_prompt = provider.requests[0]
        assert first_prompt[0]["content"] == INCREMENTAL_SUMMARY_SYSTEM_PROMPT
        assert "(none — this is the first compaction)" in first_prompt[1]["content"]
        assert "Historical exact user question." not in first_prompt[1]["content"]
        first_ledger = next(
            message["content"] for message in agent.messages
            if str(message.get("content") or "").startswith(
                AGENT_SUMMARY_LEDGER_HEADER
            )
        )
        assert "delta 1" in first_ledger

        agent.messages.extend([
            {
                "role": "assistant",
                "reasoning_content": "Second private reasoning.",
                "content": "Second response.",
                "tool_calls": [{
                    "id": "second-call",
                    "type": "function",
                    "function": {
                        "name": "run_code",
                        "arguments": '{"code":"print(84)"}',
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "second-call",
                "content": "Second tool result is 84.",
            },
            {"role": "user", "content": "Second tail user."},
            {"role": "assistant", "content": "Second tail response."},
        ])
        agent.context_tokens = 256_000
        agent._maybe_compact()
        assert len(provider.requests) == 2, "one Summary API call per compaction"
        second_input = provider.requests[1][1]["content"]
        assert "delta 1" in second_input, "prior summary must be reference context"
        assert "Second private reasoning." in second_input
        assert "Second tool result is 84." in second_input
        assert "Historical exact user question." not in second_input
        second_ledger = next(
            message["content"] for message in agent.messages
            if str(message.get("content") or "").startswith(
                AGENT_SUMMARY_LEDGER_HEADER
            )
        )
        assert second_ledger.count("delta 1") == 1
        assert "delta 2" in second_ledger
        merged_user_ledger = next(
            message["content"] for message in agent.messages
            if str(message.get("content") or "").startswith(
                MERGED_USER_HISTORY_HEADER
            )
        )
        assert merged_user_ledger.count("Historical exact user question.") == 1
        latest_done = [
            data for kind, data in incremental_events if kind == "compact_done"
        ][-1]
        assert latest_done["prior_summary_chars"] > 0
        assert latest_done["delta_summary_chars"] > 0

    thinking_events: list[tuple[str, dict]] = []
    with tempfile.TemporaryDirectory() as temporary:
        workdir = Path(temporary)
        provider = SummaryProvider()
        context = ToolContext(
            workdir=workdir,
            sandbox=None,  # type: ignore[arg-type]
            settings={"context": {"working_memory_mode": "replace"}},
        )
        agent = Agent(
            provider,
            ToolRegistry(),
            context,
            "SYSTEM",
            compact_threshold_tokens=1,
            keep_tail_messages=2,
            compaction_strategy=INCREMENTAL_SUMMARY_PRESERVE_THINKING_V1,
            tool_result_externalize_threshold_tokens=20,
            tool_result_preview_chars=80,
            on_event=lambda kind, data: thinking_events.append((kind, data)),
        )
        first_result = "first-raw-tool-result-" * 120
        agent.messages = agent.messages[:2] + [
            {"role": "user", "content": "Exact hybrid user question."},
            {
                "role": "assistant",
                "content": "First visible assistant message.",
                "reasoning_content": "FULL THINKING ALPHA must survive exactly.",
                "tool_calls": [{
                    "id": "hybrid-call-1",
                    "type": "function",
                    "function": {
                        "name": "run_code",
                        "arguments": '{"code":"print(123)"}',
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "hybrid-call-1",
                "content": first_result,
            },
            {
                "role": "user",
                "content": "[Orchestrator status — not user input]\ncontinue",
            },
            {"role": "user", "content": "Hybrid recent user."},
            {"role": "assistant", "content": "Hybrid recent response."},
        ]
        agent.context_tokens = 256_000
        agent._maybe_compact()
        assert len(provider.requests) == 1
        first_summary_input = provider.requests[0][1]["content"]
        assert "FULL THINKING ALPHA must survive exactly." in first_summary_input
        assert "First visible assistant message." in first_summary_input
        assert 'print(123)' in first_summary_input
        assert first_result in first_summary_input
        assert "Orchestrator status" in first_summary_input
        assert "Exact hybrid user question." not in first_summary_input
        preserved_alpha = next(
            message for message in agent.messages
            if message.get("reasoning_content")
            == "FULL THINKING ALPHA must survive exactly."
        )
        assert preserved_alpha["content"] == PRESERVED_THINKING_TRACE_HEADER
        assert preserved_alpha["tool_calls"][0]["id"] == "hybrid-call-1"
        first_stub = next(
            message["content"] for message in agent.messages
            if message.get("tool_call_id") == "hybrid-call-1"
        )
        assert first_stub.startswith(TOOL_RESULT_EXTERNALIZED_HEADER)

        agent.messages.extend([
            {
                "role": "assistant",
                "content": "Second visible assistant message.",
                "reasoning_content": "FULL THINKING BETA must also survive exactly.",
                "tool_calls": [{
                    "id": "hybrid-call-2",
                    "type": "function",
                    "function": {
                        "name": "log_decision",
                        "arguments": '{"decision":"keep both thoughts"}',
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "hybrid-call-2",
                "content": "decision logged.",
            },
            {"role": "user", "content": "Second hybrid tail user."},
            {"role": "assistant", "content": "Second hybrid tail response."},
        ])
        agent.context_tokens = 256_000
        agent._maybe_compact()
        assert len(provider.requests) == 2
        second_summary_input = provider.requests[1][1]["content"]
        assert "delta 1" in second_summary_input
        assert "FULL THINKING BETA must also survive exactly." in second_summary_input
        assert "decision logged." in second_summary_input
        assert "FULL THINKING ALPHA must survive exactly." not in second_summary_input
        preserved_reasoning = {
            message.get("reasoning_content") for message in agent.messages
            if message.get("reasoning_content")
        }
        assert preserved_reasoning == {
            "FULL THINKING ALPHA must survive exactly.",
            "FULL THINKING BETA must also survive exactly.",
        }
        second_result = next(
            message["content"] for message in agent.messages
            if message.get("tool_call_id") == "hybrid-call-2"
        )
        assert second_result.startswith(PRESERVED_TOOL_RESULT_HEADER)
        summary_ledger = next(
            message["content"] for message in agent.messages
            if str(message.get("content") or "").startswith(
                AGENT_SUMMARY_LEDGER_HEADER
            )
        )
        assert summary_ledger.count("delta 1") == 1
        assert "delta 2" in summary_ledger
        latest_thinking_done = [
            data for kind, data in thinking_events if kind == "compact_done"
        ][-1]
        assert latest_thinking_done["summary_calls"] == 1
        assert latest_thinking_done["preserved_reasoning_chars"] >= (
            len("FULL THINKING ALPHA must survive exactly.")
            + len("FULL THINKING BETA must also survive exactly.")
        )

    external_events: list[tuple[str, dict]] = []
    with tempfile.TemporaryDirectory() as temporary:
        workdir = Path(temporary)
        provider = SummaryProvider()
        context = ToolContext(
            workdir=workdir,
            sandbox=None,  # type: ignore[arg-type]
            settings={"context": {"working_memory_mode": "replace"}},
        )
        agent = Agent(
            provider,
            ToolRegistry(),
            context,
            "SYSTEM",
            compact_threshold_tokens=1,
            keep_tail_messages=2,
            compaction_strategy=EXTERNALIZED_TOOL_RESULTS_V1,
            tool_result_externalize_threshold_tokens=20,
            tool_result_preview_chars=80,
            on_event=lambda kind, data: external_events.append((kind, data)),
        )
        exact_result = "exact-large-run-result-" * 120
        source = workdir / "results" / "source.txt"
        source.parent.mkdir()
        source.write_text("source line\n" * 240)
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        read_result = (
            "path: results/source.txt\n"
            "view: text\n"
            f"sha256: {source_sha}\n"
            f"size_bytes: {source.stat().st_size}\n"
            "total_lines: 240\n"
            "returned_lines: 1-160\n"
            "has_more_before: false\n"
            "has_more_after: true\n"
            "previous_start_line: null\n"
            "next_start_line: 161\n\n"
            "content:\n" + ("source line\n" * 160)
        )
        agent.messages = agent.messages[:2] + [
            {
                "role": "assistant",
                "content": "Calling a tool.",
                "tool_calls": [
                    {
                        "id": "large-call",
                        "type": "function",
                        "function": {
                            "name": "run_code",
                            "arguments": '{"code":"print(42)"}',
                        },
                    },
                    {
                        "id": "read-call",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"results/source.txt"}',
                        },
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "large-call",
                "content": exact_result,
            },
            {
                "role": "tool",
                "tool_call_id": "read-call",
                "content": read_result,
            },
            {"role": "user", "content": "Recent user stays raw."},
            {"role": "assistant", "content": "Recent response stays raw."},
        ]
        agent.context_tokens = 256_000
        agent._maybe_compact()
        assert not provider.requests, "externalization strategy must not call Summary API"
        run_stub = agent.messages[3]["content"]
        assert run_stub.startswith(TOOL_RESULT_EXTERNALIZED_HEADER)
        relative = next(
            line.removeprefix("Full result: ")
            for line in run_stub.splitlines()
            if line.startswith("Full result: ")
        )
        assert (workdir / relative).read_text() == exact_result
        read_stub = agent.messages[4]["content"]
        assert read_stub.startswith(READ_RESULT_REPLAY_HEADER)
        assert "Canonical file: results/source.txt" in read_stub
        assert "Returned lines: 1-160" in read_stub
        assert '"end_line": 160' in read_stub
        assert '"start_line": 1' in read_stub
        assert len(list((workdir / "tool_result_logs").glob("*.txt"))) == 1
        assert agent.messages[-2]["content"] == "Recent user stays raw."
        assert agent.messages[-1]["content"] == "Recent response stays raw."
        done = next(data for kind, data in external_events if kind == "compact_done")
        assert done["summary_calls"] == 0
        assert done["externalized_tool_results"] == 2
        assert len(done["tool_result_log_files"]) == 1
        assert done["tool_result_replay_references"] == [{
            "tool_call_id": "read-call",
            "canonical_path": "results/source.txt",
            "start_line": 1,
            "end_line": 160,
            "sha256": source_sha,
        }]
        assert done["tool_result_tokens_saved_estimate"] > 0

        # Reading the archived run_code result later must replay the same
        # canonical file instead of producing tool_result_logs/B -> C chains.
        archived_read_result = (
            f"path: {relative}\n"
            "view: text\n"
            "sha256: replay-sha\n"
            "size_bytes: 4096\n"
            "total_lines: 220\n"
            "returned_lines: 1-160\n"
            "has_more_before: false\n"
            "has_more_after: true\n"
            "previous_start_line: null\n"
            "next_start_line: 161\n\n"
            "content:\n" + ("archived output line\n" * 160)
        )
        agent.messages.extend([
            {
                "role": "assistant",
                "content": "Re-reading the canonical archive.",
                "tool_calls": [{
                    "id": "archive-read-call",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": relative}),
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "archive-read-call",
                "content": archived_read_result,
            },
            {"role": "user", "content": "Latest user."},
            {"role": "assistant", "content": "Latest response."},
        ])
        agent.context_tokens = 256_000
        agent._maybe_compact()
        archive_replay_stub = next(
            message["content"] for message in agent.messages
            if message.get("tool_call_id") == "archive-read-call"
        )
        assert archive_replay_stub.startswith(READ_RESULT_REPLAY_HEADER)
        assert f"Canonical file: {relative}" in archive_replay_stub
        assert "canonical Tool Result log" in archive_replay_stub
        assert len(list((workdir / "tool_result_logs").glob("*.txt"))) == 1

        event_count = len(external_events)
        agent.context_tokens = 256_000
        agent._maybe_compact()
        assert len(external_events) == event_count, "file references must not be reprocessed"

    assert CONTEXT_PROFILES["control"]["compaction_strategy"] == "legacy_monolithic"
    assert CONTEXT_PROFILES["monolithic-256k"]["compact_threshold_tokens"] == 256_000
    assert CONTEXT_PROFILES["split-256k"] == {
        "compact_threshold_tokens": 256_000,
        "keep_tail_messages": 10,
        "compaction_strategy": SPLIT_USER_AGENT_V1,
    }
    assert CONTEXT_PROFILES["incremental-summary-256k"] == {
        "compact_threshold_tokens": 256_000,
        "keep_tail_messages": 10,
        "compaction_strategy": INCREMENTAL_SUMMARY_V1,
    }
    assert CONTEXT_PROFILES["externalized-results-256k"] == {
        "compact_threshold_tokens": 256_000,
        "keep_tail_messages": 10,
        "compaction_strategy": EXTERNALIZED_TOOL_RESULTS_V1,
        "tool_result_externalize_threshold_tokens": 1_000,
        "tool_result_preview_chars": 600,
    }
    assert CONTEXT_PROFILES["summary-preserve-thinking-256k"] == {
        "compact_threshold_tokens": 256_000,
        "keep_tail_messages": 10,
        "compaction_strategy": INCREMENTAL_SUMMARY_PRESERVE_THINKING_V1,
        "tool_result_externalize_threshold_tokens": 1_000,
        "tool_result_preview_chars": 600,
    }
    print("context compaction experiment checks: passed")


if __name__ == "__main__":
    main()
