"""Optional context-compaction strategies and measurement helpers.

The legacy strategy remains implemented in ``Agent``.  This module contains
the experimental split strategy so it can be selected per frozen experiment
without changing the default behavior of existing runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any


LEGACY_MONOLITHIC = "legacy_monolithic"
SPLIT_USER_AGENT_V1 = "split_user_agent_v1"
INCREMENTAL_SUMMARY_V1 = "incremental_summary_v1"
EXTERNALIZED_TOOL_RESULTS_V1 = "externalized_tool_results_v1"
INCREMENTAL_SUMMARY_PRESERVE_THINKING_V1 = (
    "incremental_summary_preserve_thinking_v1"
)
CHECKPOINT_SUMMARY_V2 = "checkpoint_summary_v2"
POLICY_TOOL_PRUNING_V2 = "policy_tool_pruning_v2"
CHECKPOINT_TOOL_PRUNING_V2 = "checkpoint_tool_pruning_v2"
SUPPORTED_COMPACTION_STRATEGIES = {
    LEGACY_MONOLITHIC,
    SPLIT_USER_AGENT_V1,
    INCREMENTAL_SUMMARY_V1,
    EXTERNALIZED_TOOL_RESULTS_V1,
    INCREMENTAL_SUMMARY_PRESERVE_THINKING_V1,
    CHECKPOINT_SUMMARY_V2,
    POLICY_TOOL_PRUNING_V2,
    CHECKPOINT_TOOL_PRUNING_V2,
}

MERGED_USER_HISTORY_HEADER = (
    "[Earlier user messages merged verbatim; these are historical messages, "
    "not a new request]"
)
AGENT_TRACE_SUMMARY_HEADER = (
    "[Earlier assistant reasoning and tool activity compacted by the Summary API]"
)
AGENT_SUMMARY_LEDGER_HEADER = (
    "[Earlier agent execution summaries — append-only; never replace or rewrite]"
)
TOOL_RESULT_EXTERNALIZED_HEADER = (
    "[Tool result externalized — exact content is stored in the workspace]"
)
READ_RESULT_REPLAY_HEADER = (
    "[Read result evicted — replay from the canonical file; "
    "do not archive this read again]"
)
PRESERVED_THINKING_TRACE_HEADER = (
    "[Earlier reasoning and tool-call trace preserved verbatim; already summarized]"
)
PRESERVED_TOOL_RESULT_HEADER = (
    "[Earlier Tool Result preserved verbatim; already summarized]"
)
EXECUTION_CHECKPOINT_HEADER = (
    "[Earlier execution checkpoint — mutable current state; replaces the previous "
    "checkpoint]"
)
TOOL_RESULT_PRUNED_HEADER = (
    "[Old Tool Result pruned by tool-specific policy; exact event remains in the "
    "persistent log]"
)

AGENT_TRACE_SUMMARY_SYSTEM_PROMPT = """\
You compress an earlier segment of an agent conversation. The input contains
assistant responses, private reasoning, tool calls with arguments, tool results,
and system-managed orchestration messages. Do not invent facts or repeat
unimportant narration. Preserve exact file paths, important numbers, failures,
decisions, and unfinished requirements.

Return only these three short sections:
Progress:
Key facts:
Open work:

This summary will replace the raw execution trace, so keep information needed to
continue the work while remaining concise.
"""

INCREMENTAL_SUMMARY_SYSTEM_PROMPT = """\
You create a delta summary for one newly compacted segment of an agent
conversation. You receive an immutable PRIOR SUMMARY for context and a NEW
AGENT TRACE containing assistant responses, private reasoning, tool calls with
arguments, tool results, and system-managed orchestration messages.

Use the prior summary to understand continuity, but do not rewrite, repeat, or
summarize it. Summarize only facts introduced by the NEW AGENT TRACE. Do not
invent facts. Preserve exact file paths, important numbers, failures, decisions,
and unfinished requirements.

Return only these three short sections for the new delta:
Progress:
Key facts:
Open work:
"""

CHECKPOINT_SUMMARY_SYSTEM_PROMPT = """\
You compact the execution history of a long-running mathematical-modeling agent.
Create an operational handoff checkpoint, not a chronological conversation summary.
Another agent must be able to continue correctly without the removed raw history.

You receive the previous checkpoint, verbatim historical user messages, and only the
new execution events since that checkpoint. Tool Results may already be replaced by
deterministic references or head/tail previews. Update the checkpoint to reflect the
latest true state. Replace obsolete or superseded facts instead of accumulating
contradictory versions.

Return only these concise sections:
## Goal
## Constraints and Preferences
## Progress
### Done
### In Progress
### Blocked
## Key Decisions
## Failed Attempts
## Critical Facts
## Relevant Files and Artifacts
## Next Steps

Rules:
- Preserve information that can change future actions, especially exact paths,
  identifiers, important numbers, unresolved errors, validated results, and explicit
  user constraints.
- Prefer current state over narration. Record concrete next actions.
- Do not reproduce verbose reasoning, large code, paper text, or Tool Results.
- When facts are recoverable from files or structured working state, reference the
  authoritative path instead of copying the content.
- Keep only failures worth remembering to prevent an expensive repeated attempt.
- Never invent completion, validation, files, numbers, or user requirements.
"""

_INTERNAL_USER_PREFIXES = (
    "[Orchestrator ",
    "[Final delivery ",
    "[Independent verifier ",
    "[Earlier conversation compacted.",
)


def normalize_compaction_strategy(value: Any) -> str:
    strategy = str(value or LEGACY_MONOLITHIC).strip().lower()
    return strategy if strategy in SUPPORTED_COMPACTION_STRATEGIES else LEGACY_MONOLITHIC


def json_measure(value: Any) -> dict[str, int]:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return {
        "chars": len(serialized),
        "bytes": len(serialized.encode("utf-8")),
    }


def tail_cut_preserving_tool_batch(
    history: list[dict[str, Any]],
    keep_tail_messages: int,
) -> int:
    """Return a cut that retains at least N messages and no orphan tool result."""
    cut = max(0, len(history) - max(1, keep_tail_messages))
    if cut >= len(history):
        return len(history)
    if history[cut].get("role") != "tool":
        return cut
    while cut > 0 and history[cut].get("role") == "tool":
        cut -= 1
    while cut > 0 and history[cut].get("role") != "assistant":
        cut -= 1
    return cut


def _is_user_history(message: dict[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    content = str(message.get("content") or "")
    if content.startswith(MERGED_USER_HISTORY_HEADER):
        return True
    return not content.startswith(_INTERNAL_USER_PREFIXES)


def partition_compaction_head(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    user_history: list[dict[str, Any]] = []
    agent_trace: list[dict[str, Any]] = []
    for message in messages:
        (user_history if _is_user_history(message) else agent_trace).append(message)
    return user_history, agent_trace


def merge_user_history(messages: list[dict[str, Any]]) -> str:
    blocks = [MERGED_USER_HISTORY_HEADER]
    for index, message in enumerate(messages, start=1):
        content = message.get("content")
        if isinstance(content, str):
            rendered = content
        else:
            rendered = json.dumps(content, ensure_ascii=False, default=str)
        blocks.append(f"\n--- Earlier user message {index} ---\n{rendered}")
    return "".join(blocks)


def merge_user_history_append_only(messages: list[dict[str, Any]]) -> str:
    """Preserve an existing verbatim user ledger and append newly old messages."""
    blocks = [MERGED_USER_HISTORY_HEADER]
    next_index = 1
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            rendered = content
        else:
            rendered = json.dumps(content, ensure_ascii=False, default=str)
        if rendered.startswith(MERGED_USER_HISTORY_HEADER):
            existing = rendered[len(MERGED_USER_HISTORY_HEADER):]
            if existing:
                blocks.append(existing)
                next_index += existing.count("--- Earlier user message ")
            continue
        blocks.append(
            f"\n--- Earlier user message {next_index} ---\n{rendered}"
        )
        next_index += 1
    return "".join(blocks)


def _is_summary_message(message: dict[str, Any]) -> bool:
    content = str(message.get("content") or "")
    return content.startswith((
        AGENT_SUMMARY_LEDGER_HEADER,
        AGENT_TRACE_SUMMARY_HEADER,
        EXECUTION_CHECKPOINT_HEADER,
        "[Earlier conversation compacted.",
    ))


def partition_incremental_compaction_head(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    user_history: list[dict[str, Any]] = []
    prior_summaries: list[dict[str, Any]] = []
    new_agent_trace: list[dict[str, Any]] = []
    for message in messages:
        if _is_summary_message(message):
            prior_summaries.append(message)
        elif _is_user_history(message):
            user_history.append(message)
        else:
            new_agent_trace.append(message)
    return user_history, prior_summaries, new_agent_trace


def partition_thinking_preserving_compaction_head(
    messages: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Separate visible messages from a losslessly retained technical trace."""
    user_history: list[dict[str, Any]] = []
    prior_summaries: list[dict[str, Any]] = []
    summary_trace: list[dict[str, Any]] = []
    preserved_trace: list[dict[str, Any]] = []

    for message in messages:
        if _is_summary_message(message):
            prior_summaries.append(message)
            continue
        if _is_user_history(message):
            user_history.append(message)
            continue
        role = message.get("role")
        if role == "assistant":
            already_summarized = str(message.get("content") or "").startswith(
                PRESERVED_THINKING_TRACE_HEADER
            )
            if not already_summarized:
                # The Summary API sees the complete new assistant message,
                # including full thinking and tool calls. Reasoning/tool-call
                # fields are also retained below as a lossless copy.
                summary_trace.append(message)
            if message.get("reasoning_content") or message.get("tool_calls"):
                retained = dict(message)
                retained["content"] = PRESERVED_THINKING_TRACE_HEADER
                preserved_trace.append(retained)
            continue
        if role == "tool":
            already_summarized = str(message.get("content") or "").startswith((
                TOOL_RESULT_EXTERNALIZED_HEADER,
                READ_RESULT_REPLAY_HEADER,
                PRESERVED_TOOL_RESULT_HEADER,
            ))
            if not already_summarized:
                summary_trace.append(message)
            preserved_trace.append(message)
            continue
        summary_trace.append(message)
    return user_history, prior_summaries, summary_trace, preserved_trace


def mark_preserved_tool_results(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Mark non-externalized Tool Results so later deltas do not resummarize them."""
    marked: list[dict[str, Any]] = []
    for message in messages:
        content = str(message.get("content") or "")
        if (
            message.get("role") == "tool"
            and not content.startswith((
                TOOL_RESULT_EXTERNALIZED_HEADER,
                READ_RESULT_REPLAY_HEADER,
                PRESERVED_TOOL_RESULT_HEADER,
            ))
        ):
            replacement = dict(message)
            replacement["content"] = (
                f"{PRESERVED_TOOL_RESULT_HEADER}\n{content}"
            )
            marked.append(replacement)
        else:
            marked.append(message)
    return marked


def render_prior_summary(messages: list[dict[str, Any]]) -> str:
    return "\n\n".join(str(message.get("content") or "") for message in messages)


def render_execution_checkpoint(summary: str) -> str:
    body = summary.strip() or "(checkpoint unavailable)"
    return f"{EXECUTION_CHECKPOINT_HEADER}\n{body}"


def append_summary_delta(
    prior_messages: list[dict[str, Any]],
    delta: str,
    compaction_index: int,
) -> str:
    parts = [AGENT_SUMMARY_LEDGER_HEADER]
    for message in prior_messages:
        content = str(message.get("content") or "")
        if content.startswith(AGENT_SUMMARY_LEDGER_HEADER):
            content = content[len(AGENT_SUMMARY_LEDGER_HEADER):].lstrip("\n")
        if content:
            parts.append(content)
    if delta.strip():
        parts.append(
            f"--- Compaction {compaction_index} delta ---\n{delta.strip()}"
        )
    return "\n\n".join(parts)


def serialize_agent_trace(messages: list[dict[str, Any]]) -> str:
    """Serialize the full compressible trace, including reasoning and tool I/O."""
    records: list[dict[str, Any]] = []
    for message in messages:
        record = {
            key: message[key]
            for key in (
                "role",
                "content",
                "reasoning_content",
                "tool_calls",
                "tool_call_id",
                "name",
            )
            if key in message
        }
        records.append(record)
    return json.dumps(records, ensure_ascii=False, indent=2, default=str)


def estimate_tokens(value: Any) -> int:
    """Match the Context Inspector's display-only message token estimate."""
    rendered = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, default=str)
    )
    if not rendered:
        return 0
    ascii_count = sum(1 for char in rendered if ord(char) < 128)
    non_ascii_count = len(rendered) - ascii_count
    return max(1, round(ascii_count / 4 + non_ascii_count / 1.5))


def _tool_calls_by_id(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            function = call.get("function") or {}
            if call_id:
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        parsed_arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        parsed_arguments = {}
                elif isinstance(arguments, dict):
                    parsed_arguments = arguments
                else:
                    parsed_arguments = {}
                calls[call_id] = {
                    "name": str(function.get("name") or "tool"),
                    "arguments": parsed_arguments,
                }
    return calls


def _safe_file_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned[:80] or "tool"


def _local_preview(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    head = max(1, round(limit * 0.72))
    tail = max(1, limit - head)
    omitted = len(content) - head - tail
    return (
        content[:head]
        + f"\n... [{omitted} characters omitted; read the file for the exact result] ...\n"
        + content[-tail:]
    )


def _read_file_replay_stub(
    *,
    call_id: str,
    arguments: dict[str, Any],
    rendered: str,
    token_count: int,
) -> tuple[str, dict[str, Any]] | None:
    """Describe how to replay a read without archiving the read result again."""
    metadata: dict[str, str] = {}
    for key in ("path", "sha256", "total_lines", "returned_lines"):
        match = re.search(rf"(?m)^{key}:\s*(.+?)\s*$", rendered)
        if match:
            metadata[key] = match.group(1)
    source_path = str(arguments.get("path") or metadata.get("path") or "").strip()
    if not source_path:
        return None

    start_line = arguments.get("start_line")
    end_line = arguments.get("end_line")
    returned = re.fullmatch(r"(\d+)-(\d+)", metadata.get("returned_lines", ""))
    if returned:
        start_line = int(returned.group(1))
        end_line = int(returned.group(2))
    replay_args: dict[str, Any] = {"path": source_path}
    if isinstance(start_line, int) and start_line > 0:
        replay_args["start_line"] = start_line
    if isinstance(end_line, int) and end_line > 0:
        replay_args["end_line"] = end_line

    source_kind = (
        "canonical Tool Result log"
        if Path(source_path).parts[:1] == ("tool_result_logs",)
        else "canonical workspace file"
    )
    stub_lines = [
        READ_RESULT_REPLAY_HEADER,
        "Tool: read_file",
        f"Tool call ID: {call_id or 'unknown'}",
        f"Canonical source type: {source_kind}",
        f"Canonical file: {source_path}",
        f"Original returned characters: {len(rendered)}",
        f"Estimated tokens: {token_count}",
    ]
    if metadata.get("sha256"):
        stub_lines.append(f"Source SHA256 at read time: {metadata['sha256']}")
    if metadata.get("total_lines"):
        stub_lines.append(f"Source total lines at read time: {metadata['total_lines']}")
    if metadata.get("returned_lines"):
        stub_lines.append(f"Returned lines: {metadata['returned_lines']}")
    stub_lines.extend([
        "Replay with read_file arguments:",
        json.dumps(replay_args, ensure_ascii=False, sort_keys=True),
        (
            "If the current file SHA256 differs, treat it as a newer file version; "
            "do not follow another generated Tool Result path."
        ),
    ])
    return "\n".join(stub_lines), {
        "tool_call_id": call_id,
        "canonical_path": source_path,
        "start_line": replay_args.get("start_line"),
        "end_line": replay_args.get("end_line"),
        "sha256": metadata.get("sha256"),
    }


def externalize_tool_results(
    messages: list[dict[str, Any]],
    *,
    workdir: Path,
    threshold_tokens: int,
    preview_chars: int,
    compaction_index: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Replace large tool results with local previews and lossless file refs."""
    calls = _tool_calls_by_id(messages)
    output: list[dict[str, Any]] = []
    files: list[str] = []
    replay_references: list[dict[str, Any]] = []
    raw_chars = raw_tokens = stub_chars = stub_tokens = 0
    externalized = 0

    for sequence, message in enumerate(messages, start=1):
        content = message.get("content")
        if (
            message.get("role") != "tool"
            or str(content or "").startswith((
                TOOL_RESULT_EXTERNALIZED_HEADER,
                READ_RESULT_REPLAY_HEADER,
            ))
        ):
            output.append(message)
            continue
        rendered = (
            content
            if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False, default=str)
        )
        token_count = estimate_tokens(rendered)
        if token_count < max(1, threshold_tokens):
            output.append(message)
            continue

        call_id = str(message.get("tool_call_id") or "")
        call = calls.get(call_id) or {}
        tool_name = str(call.get("name") or message.get("name") or "tool")
        if tool_name == "read_file":
            replay = _read_file_replay_stub(
                call_id=call_id,
                arguments=call.get("arguments") or {},
                rendered=rendered,
                token_count=token_count,
            )
            if replay is not None:
                stub, replay_reference = replay
                replacement = dict(message)
                replacement["content"] = stub
                output.append(replacement)
                replay_references.append(replay_reference)
                externalized += 1
                raw_chars += len(rendered)
                raw_tokens += token_count
                stub_chars += len(stub)
                stub_tokens += estimate_tokens(stub)
                continue
        digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        identity = _safe_file_part(call_id or digest[:16])
        filename = (
            f"{compaction_index:03d}-{sequence:05d}-"
            f"{_safe_file_part(tool_name)}-{identity}.txt"
        )
        relative = Path("tool_result_logs") / filename
        target = workdir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_text(
            encoding="utf-8", errors="replace"
        ) != rendered:
            temporary = target.with_name(
                f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temporary.write_text(rendered, encoding="utf-8")
            temporary.replace(target)

        preview = _local_preview(rendered, max(80, preview_chars))
        stub = (
            f"{TOOL_RESULT_EXTERNALIZED_HEADER}\n"
            f"Tool: {tool_name}\n"
            f"Tool call ID: {call_id or 'unknown'}\n"
            f"Original characters: {len(rendered)}\n"
            f"Estimated tokens: {token_count}\n"
            f"SHA256: {digest}\n"
            f"Full result: {relative.as_posix()}\n"
            "Local preview (not a semantic summary):\n"
            f"{preview}"
        )
        replacement = dict(message)
        replacement["content"] = stub
        output.append(replacement)
        externalized += 1
        files.append(relative.as_posix())
        raw_chars += len(rendered)
        raw_tokens += token_count
        stub_chars += len(stub)
        stub_tokens += estimate_tokens(stub)

    return output, {
        "externalized_tool_results": externalized,
        "tool_result_chars_before": raw_chars,
        "tool_result_chars_after": stub_chars,
        "tool_result_tokens_before_estimate": raw_tokens,
        "tool_result_tokens_after_estimate": stub_tokens,
        "tool_result_tokens_saved_estimate": max(0, raw_tokens - stub_tokens),
        "tool_result_log_files": files,
        "tool_result_replay_references": replay_references,
    }


# Policies are intentionally conservative for state-changing and hard-to-recover
# tools. Thresholds were selected from experiments/tool-token-stats: the expensive
# tail is concentrated in read_file, load_skill*, results_get, search_literature,
# run_code, and a few large sub-agent/fetch results.
_NEVER_PRUNE_TOOL_RESULTS = {
    "ask_user",
    "describe_image",
    "edit_paragraph",
    "ingest_problem",
    "log_decision",
    "plan_write",
    "set_task_status",
    "spawn_subagent",
    "write_paper",
}

_TOOL_RESULT_PRUNING_POLICIES: dict[str, dict[str, Any]] = {
    # Recoverable from the original tool target. These use references instead of
    # making another copy, which also avoids read_file -> archive -> read loops.
    "read_file": {
        "strategy": "reference",
        "moderate_tokens": 700,
        "aggressive_tokens": 350,
        "head_chars": 0,
        "tail_chars": 0,
    },
    "load_skill": {
        "strategy": "reference",
        "moderate_tokens": 2_000,
        "aggressive_tokens": 1_000,
        "head_chars": 700,
        "tail_chars": 0,
    },
    "load_skill_file": {
        "strategy": "reference",
        "moderate_tokens": 1_600,
        "aggressive_tokens": 800,
        "head_chars": 700,
        "tail_chars": 0,
    },
    "results_get": {
        "strategy": "reference",
        "moderate_tokens": 1_600,
        "aggressive_tokens": 900,
        "head_chars": 650,
        "tail_chars": 350,
    },
    "inspect_paper_blocks": {
        "strategy": "reference",
        "moderate_tokens": 1_800,
        "aggressive_tokens": 900,
        "head_chars": 550,
        "tail_chars": 450,
    },
    # Potentially irrecoverable or expensive to repeat: preserve the exact result
    # in tool_result_logs and keep a tool-aware preview in the Context View.
    "run_code": {
        "strategy": "archive",
        "moderate_tokens": 1_200,
        "aggressive_tokens": 700,
        "head_chars": 500,
        "tail_chars": 2_200,
    },
    "search_literature": {
        "strategy": "archive",
        "moderate_tokens": 1_500,
        "aggressive_tokens": 800,
        "head_chars": 1_800,
        "tail_chars": 500,
    },
    "web_fetch": {
        "strategy": "archive",
        "moderate_tokens": 1_800,
        "aggressive_tokens": 900,
        "head_chars": 1_200,
        "tail_chars": 800,
    },
    "web_search": {
        "strategy": "archive",
        "moderate_tokens": 1_400,
        "aggressive_tokens": 800,
        "head_chars": 1_000,
        "tail_chars": 500,
    },
    "collect_subagent_results": {
        "strategy": "archive",
        "moderate_tokens": 2_200,
        "aggressive_tokens": 1_400,
        "head_chars": 1_200,
        "tail_chars": 1_200,
    },
}


def _policy_preview(content: str, *, head_chars: int, tail_chars: int) -> str:
    if head_chars <= 0 and tail_chars <= 0:
        return ""
    if len(content) <= head_chars + tail_chars:
        return content
    omitted = len(content) - head_chars - tail_chars
    head = content[:head_chars] if head_chars else ""
    tail = content[-tail_chars:] if tail_chars else ""
    separator = (
        f"\n... [{omitted} characters omitted by tool policy; "
        "recover or read the exact result when needed] ...\n"
    )
    return head + separator + tail


def _policy_reference_stub(
    *,
    tool_name: str,
    call_id: str,
    arguments: dict[str, Any],
    rendered: str,
    token_count: int,
    head_chars: int,
    tail_chars: int,
) -> str:
    preview = _policy_preview(
        rendered,
        head_chars=head_chars,
        tail_chars=tail_chars,
    )
    lines = [
        TOOL_RESULT_PRUNED_HEADER,
        f"Tool: {tool_name}",
        f"Tool call ID: {call_id or 'unknown'}",
        f"Original characters: {len(rendered)}",
        f"Estimated tokens: {token_count}",
        "Recoverable: yes",
        "Replay the original tool with these arguments:",
        json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str),
    ]
    if preview:
        lines.extend(["Retained preview:", preview])
    return "\n".join(lines)


def prune_tool_results_by_policy(
    messages: list[dict[str, Any]],
    *,
    workdir: Path,
    level: str,
    recent_tool_results: int,
    prune_index: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Prune old Tool Results using recoverability and tool-specific size rules.

    Raw event/context logs are not modified. Recoverable outputs become replay
    references; expensive/non-deterministic outputs are archived losslessly. The
    latest N Tool Results stay raw regardless of size.
    """
    aggressive = level == "aggressive"
    threshold_key = "aggressive_tokens" if aggressive else "moderate_tokens"
    calls = _tool_calls_by_id(messages)
    tool_positions = [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "tool"
        and not str(message.get("content") or "").startswith((
            TOOL_RESULT_EXTERNALIZED_HEADER,
            TOOL_RESULT_PRUNED_HEADER,
            READ_RESULT_REPLAY_HEADER,
        ))
    ]
    recent_count = max(0, int(recent_tool_results))
    protected = set(tool_positions[-recent_count:]) if recent_count else set()
    output: list[dict[str, Any]] = []
    files: list[str] = []
    replay_references: list[dict[str, Any]] = []
    by_tool: dict[str, dict[str, int]] = {}
    raw_chars = raw_tokens = stub_chars = stub_tokens = 0
    reference_count = archive_count = 0

    for sequence, message in enumerate(messages, start=1):
        content = message.get("content")
        content_text = str(content or "")
        if (
            message.get("role") != "tool"
            or sequence - 1 in protected
            or content_text.startswith((
                TOOL_RESULT_EXTERNALIZED_HEADER,
                TOOL_RESULT_PRUNED_HEADER,
                READ_RESULT_REPLAY_HEADER,
            ))
        ):
            output.append(message)
            continue

        call_id = str(message.get("tool_call_id") or "")
        call = calls.get(call_id) or {}
        tool_name = str(call.get("name") or message.get("name") or "tool")
        if tool_name in _NEVER_PRUNE_TOOL_RESULTS:
            output.append(message)
            continue
        policy = _TOOL_RESULT_PRUNING_POLICIES.get(tool_name, {
            "strategy": "archive",
            "moderate_tokens": 3_000,
            "aggressive_tokens": 1_800,
            "head_chars": 900,
            "tail_chars": 900,
        })
        rendered = (
            content
            if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False, default=str)
        )
        token_count = estimate_tokens(rendered)
        if token_count < int(policy[threshold_key]):
            output.append(message)
            continue

        arguments = call.get("arguments") or {}
        strategy = str(policy["strategy"])
        if tool_name == "read_file":
            replay = _read_file_replay_stub(
                call_id=call_id,
                arguments=arguments,
                rendered=rendered,
                token_count=token_count,
            )
            if replay is not None:
                stub, replay_reference = replay
                replay_references.append(replay_reference)
            else:
                stub = _policy_reference_stub(
                    tool_name=tool_name,
                    call_id=call_id,
                    arguments=arguments,
                    rendered=rendered,
                    token_count=token_count,
                    head_chars=0,
                    tail_chars=0,
                )
            reference_count += 1
        elif strategy == "reference":
            stub = _policy_reference_stub(
                tool_name=tool_name,
                call_id=call_id,
                arguments=arguments,
                rendered=rendered,
                token_count=token_count,
                head_chars=int(policy["head_chars"]),
                tail_chars=int(policy["tail_chars"]),
            )
            reference_count += 1
        else:
            digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
            identity = _safe_file_part(call_id or digest[:16])
            filename = (
                f"prune-{prune_index:03d}-{sequence:05d}-"
                f"{_safe_file_part(tool_name)}-{identity}.txt"
            )
            relative = Path("tool_result_logs") / filename
            target = workdir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or target.read_text(
                encoding="utf-8", errors="replace"
            ) != rendered:
                temporary = target.with_name(
                    f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
                )
                temporary.write_text(rendered, encoding="utf-8")
                temporary.replace(target)
            preview = _policy_preview(
                rendered,
                head_chars=int(policy["head_chars"]),
                tail_chars=int(policy["tail_chars"]),
            )
            stub = "\n".join([
                TOOL_RESULT_PRUNED_HEADER,
                f"Tool: {tool_name}",
                f"Tool call ID: {call_id or 'unknown'}",
                f"Original characters: {len(rendered)}",
                f"Estimated tokens: {token_count}",
                f"SHA256: {digest}",
                f"Exact result: {relative.as_posix()}",
                "Retained head/tail preview:",
                preview,
            ])
            files.append(relative.as_posix())
            archive_count += 1

        replacement = dict(message)
        replacement["content"] = stub
        output.append(replacement)
        after_tokens = estimate_tokens(stub)
        raw_chars += len(rendered)
        raw_tokens += token_count
        stub_chars += len(stub)
        stub_tokens += after_tokens
        tool_metrics = by_tool.setdefault(tool_name, {
            "count": 0,
            "tokens_before": 0,
            "tokens_after": 0,
            "tokens_saved": 0,
        })
        tool_metrics["count"] += 1
        tool_metrics["tokens_before"] += token_count
        tool_metrics["tokens_after"] += after_tokens
        tool_metrics["tokens_saved"] += max(0, token_count - after_tokens)

    return output, {
        "prune_level": "aggressive" if aggressive else "moderate",
        "pruned_tool_results": reference_count + archive_count,
        "reference_only_tool_results": reference_count,
        "archived_tool_results": archive_count,
        "protected_recent_tool_results": len(protected),
        "tool_result_chars_before": raw_chars,
        "tool_result_chars_after": stub_chars,
        "tool_result_tokens_before_estimate": raw_tokens,
        "tool_result_tokens_after_estimate": stub_tokens,
        "tool_result_tokens_saved_estimate": max(0, raw_tokens - stub_tokens),
        "tool_result_log_files": files,
        "tool_result_replay_references": replay_references,
        "tool_result_pruning_by_tool": by_tool,
    }
