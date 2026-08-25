"""Conversation-level tool reliability metrics derived from durable events.

Context request payloads contain cumulative message history, so counting tool
messages there would count the same call again on every later model request.
This module instead pairs the unique assistant/tool-result events written to
``events.jsonl``.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

AGENT_TYPES = ("main", "verifier", "subagent")
_TERMINAL_RUN_STATUSES = {"done", "error", "stopped", "cancelled"}
_LATEX_TOOLS = {"write_paper", "edit_paragraph"}
_VERDICT_TOOLS = {"submit_verification", "submit_verification_fragment"}
_ERROR_PREFIXES = (
    "[error]",
    "[edit error]",
    "[render error]",
    "[ingest error]",
)
_PROTOCOL_FAILURE_MARKERS = (
    "arguments not valid json",
    "unknown tool",
    "missing required",
    "is required when",
    "target is required",
    "status=rejected reason=invalid_edit",
    "cannot insert",
    "was not found",
    "no exact ",
)


def _read_events(path: str | Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except OSError:
        pass
    return events


def _agent_identity(event: dict[str, Any]) -> tuple[str, str]:
    if event.get("kind") == "verification_progress":
        attempt = event.get("attempt", "")
        role = str(event.get("role") or "verifier")
        scope = event.get("scope_id") or event.get("scope") or ""
        return "verifier", f"verifier:{attempt}:{role}:{scope}"
    if event.get("subagent") is not None:
        subagent = event.get("subagent")
        return "subagent", f"subagent:{subagent}"
    return "main", "main"


def _tool_calls(event: dict[str, Any]) -> list[str]:
    is_assistant = event.get("kind") == "assistant"
    is_verifier_assistant = (
        event.get("kind") == "verification_progress"
        and event.get("phase") == "assistant"
    )
    if not (is_assistant or is_verifier_assistant):
        return []
    names: list[str] = []
    for raw in event.get("tool_calls") or []:
        name: Any = None
        if isinstance(raw, (list, tuple)) and raw:
            name = raw[0]
        elif isinstance(raw, dict):
            function = raw.get("function")
            if isinstance(function, dict):
                name = function.get("name")
            else:
                name = raw.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _tool_result(event: dict[str, Any]) -> tuple[str, str] | None:
    is_result = event.get("kind") == "tool_result"
    is_verifier_result = (
        event.get("kind") == "verification_progress"
        and event.get("phase") == "tool_result"
    )
    if not (is_result or is_verifier_result):
        return None
    name = str(event.get("name") or "unknown").strip() or "unknown"
    return name, str(event.get("observation") or "")


def _timed_out(event: dict[str, Any], observation: str) -> bool:
    lowered = observation.lower()
    return bool(
        event.get("timed_out")
        or "timed_out=true" in lowered
        or "timed out after" in lowered
        or "[sandbox] timed out" in lowered
    )


def _cancelled(observation: str) -> bool:
    lowered = observation.lower()
    return any(marker in lowered for marker in (
        "[stopped by user]",
        "stopped=true",
        "cancelled by user",
    ))


def _protocol_success(observation: str) -> bool:
    lowered = observation.strip().lower()
    if lowered.startswith(_ERROR_PREFIXES):
        return False
    return not any(marker in lowered for marker in _PROTOCOL_FAILURE_MARKERS)


def _compile_success(observation: str) -> bool:
    lowered = observation.lower()
    return bool(re.search(r"\bcompiled(?:\s+ok|\s+and\b)", lowered))


def _acceptance_success(tool: str, observation: str) -> bool:
    lowered = observation.lower()
    if "acceptance passed" in lowered:
        return True
    # Older write_paper versions returned only this compact success message.
    return bool(
        tool == "write_paper"
        and _compile_success(observation)
        and "acceptance failed" not in lowered
        and "acceptance is still pending" not in lowered
    )


def _objective_success(
    tool: str,
    observation: str,
    *,
    protocol_success: bool,
) -> bool:
    lowered = observation.lower()
    if not protocol_success:
        return False
    if tool == "run_code":
        match = re.search(r"\bexit_code=(-?\d+)", lowered)
        return bool(match and int(match.group(1)) == 0 and "timed_out=true" not in lowered)
    if tool in _LATEX_TOOLS:
        return _compile_success(observation)
    if tool in _VERDICT_TOOLS:
        return "verdict recorded" in lowered or "verification result recorded" in lowered
    if any(marker in lowered for marker in (
        "compile failed",
        "status=rolled_back",
        "status=rejected",
        "traceback (most recent call last)",
    )):
        return False
    return True


def _new_attempt(
    *,
    agent_type: str,
    agent_key: str,
    tool: str,
    order: int,
    batch: int,
    call_observed: bool,
) -> dict[str, Any]:
    return {
        "agent_type": agent_type,
        "agent_key": agent_key,
        "tool": tool,
        "order": order,
        "batch": batch,
        "call_observed": call_observed,
        "result_observed": False,
        "timed_out": False,
        "cancelled": False,
        "protocol_success": None,
        "objective_success": None,
        "compile_success": None,
        "acceptance_success": None,
        "verdict_success": None,
        "retry": False,
        "recovered_retry": False,
    }


def _pair_attempts(events: list[dict[str, Any]], run_status: str) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    unresolved: dict[tuple[str, str], deque[int]] = defaultdict(deque)
    order = 0
    batch = 0
    for event in events:
        calls = _tool_calls(event)
        if calls:
            batch += 1
            agent_type, agent_key = _agent_identity(event)
            for tool in calls:
                order += 1
                attempts.append(_new_attempt(
                    agent_type=agent_type,
                    agent_key=agent_key,
                    tool=tool,
                    order=order,
                    batch=batch,
                    call_observed=True,
                ))
                unresolved[(agent_key, tool)].append(len(attempts) - 1)
            continue

        result = _tool_result(event)
        if result is None:
            continue
        tool, observation = result
        agent_type, agent_key = _agent_identity(event)
        queue = unresolved[(agent_key, tool)]
        if queue:
            attempt = attempts[queue.popleft()]
        else:
            # Preserve legacy or partially-captured results instead of silently
            # dropping them from reliability statistics.
            order += 1
            batch += 1
            attempt = _new_attempt(
                agent_type=agent_type,
                agent_key=agent_key,
                tool=tool,
                order=order,
                batch=batch,
                call_observed=False,
            )
            attempts.append(attempt)
        timed_out = _timed_out(event, observation)
        cancelled = _cancelled(observation)
        protocol_success = (
            False if timed_out or cancelled else _protocol_success(observation)
        )
        objective_success = (
            False if timed_out or cancelled else _objective_success(
                tool,
                observation,
                protocol_success=protocol_success,
            )
        )
        attempt.update({
            "result_observed": True,
            "timed_out": timed_out,
            "cancelled": cancelled,
            "protocol_success": protocol_success,
            "objective_success": objective_success,
            "compile_success": (
                _compile_success(observation) if tool in _LATEX_TOOLS else None
            ),
            "acceptance_success": (
                _acceptance_success(tool, observation) if tool in _LATEX_TOOLS else None
            ),
            "verdict_success": (
                objective_success if tool in _VERDICT_TOOLS else None
            ),
        })

    interrupted = run_status in _TERMINAL_RUN_STATUSES
    for attempt in attempts:
        if not attempt["result_observed"]:
            attempt["cancelled"] = interrupted

    by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        by_agent[attempt["agent_key"]].append(attempt)
    for agent_attempts in by_agent.values():
        agent_attempts.sort(key=lambda item: item["order"])
        previous: dict[str, Any] | None = None
        for attempt in agent_attempts:
            if (
                previous is not None
                and attempt["batch"] != previous["batch"]
                and attempt["tool"] == previous["tool"]
                and previous["result_observed"]
                and previous["objective_success"] is False
            ):
                attempt["retry"] = True
                attempt["recovered_retry"] = attempt["objective_success"] is True
            if attempt["result_observed"]:
                previous = attempt
    return attempts


def _empty_bucket() -> dict[str, int]:
    return {
        "total_calls": 0,
        "completed_calls": 0,
        "pending_calls": 0,
        "interrupted_calls": 0,
        "protocol_evaluated": 0,
        "protocol_successes": 0,
        "objective_evaluated": 0,
        "objective_successes": 0,
        "failed_calls": 0,
        "timed_out_calls": 0,
        "cancelled_calls": 0,
        "compile_attempts": 0,
        "compile_successes": 0,
        "acceptance_attempts": 0,
        "acceptance_successes": 0,
        "verdict_attempts": 0,
        "verdict_successes": 0,
        "retry_attempts": 0,
        "recovered_retries": 0,
    }


def _bucket(attempts: list[dict[str, Any]]) -> dict[str, int]:
    result = _empty_bucket()
    result["total_calls"] = len(attempts)
    for attempt in attempts:
        if not attempt["result_observed"]:
            key = "interrupted_calls" if attempt["cancelled"] else "pending_calls"
            result[key] += 1
            continue
        result["completed_calls"] += 1
        if attempt["timed_out"]:
            result["timed_out_calls"] += 1
        if attempt["cancelled"]:
            result["cancelled_calls"] += 1
        else:
            result["protocol_evaluated"] += 1
            result["objective_evaluated"] += 1
            if attempt["protocol_success"]:
                result["protocol_successes"] += 1
            if attempt["objective_success"]:
                result["objective_successes"] += 1
            else:
                result["failed_calls"] += 1
        if attempt["compile_success"] is not None:
            result["compile_attempts"] += 1
            result["compile_successes"] += int(attempt["compile_success"])
        if attempt["acceptance_success"] is not None:
            result["acceptance_attempts"] += 1
            result["acceptance_successes"] += int(attempt["acceptance_success"])
        if attempt["verdict_success"] is not None:
            result["verdict_attempts"] += 1
            result["verdict_successes"] += int(attempt["verdict_success"])
        if attempt["retry"]:
            result["retry_attempts"] += 1
            result["recovered_retries"] += int(attempt["recovered_retry"])
    return result


def _group(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    by_tool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        by_tool[attempt["tool"]].append(attempt)
    tools = [
        {"name": name, **_bucket(tool_attempts)}
        for name, tool_attempts in by_tool.items()
    ]
    tools.sort(key=lambda item: (-item["total_calls"], item["name"]))
    return {"summary": _bucket(attempts), "tools": tools}


def conversation_tool_metrics(
    events_path: str | Path,
    *,
    run_status: str = "unknown",
) -> dict[str, Any]:
    """Return unique-call metrics for one conversation event log."""
    attempts = _pair_attempts(_read_events(events_path), run_status)
    groups = {"all": _group(attempts)}
    for agent_type in AGENT_TYPES:
        groups[agent_type] = _group([
            attempt for attempt in attempts
            if attempt["agent_type"] == agent_type
        ])
    return {
        "run_status": run_status,
        "groups": groups,
        "definitions": {
            "completed": "A durable tool_result was recorded for the call.",
            "protocol": "Arguments and the returned contract passed deterministic validation.",
            "objective": "The tool-specific objective succeeded; LaTeX tools must compile and run_code must exit 0.",
            "retry": "The same Agent immediately retried the same tool after a failed objective.",
        },
    }
