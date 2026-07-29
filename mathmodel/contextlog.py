"""Durable, standalone logging for exact model API request contexts.

The mathematical-modeling dashboard intentionally does not read this file.
The Context Inspector server exposes it on a separate local port.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

CONTEXT_LOG_FILENAME = "context_requests.jsonl"


def _json_clone(value: Any) -> Any:
    """Detach mutable provider payloads and make them JSON-safe."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class ContextRecorder:
    """Append request/response records without blocking or corrupting peers."""

    def __init__(self, path: str | Path, run_id: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self._lock = threading.RLock()
        self._sequence = _last_sequence(self.path)

    def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        with self._lock:
            record = {
                "kind": kind,
                "run_id": self.run_id,
                **_json_clone(payload),
            }
            if kind == "request":
                self._sequence += 1
                record["sequence"] = self._sequence
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(record, ensure_ascii=False, default=str) + "\n"
                )


def _last_sequence(path: Path) -> int:
    if not path.is_file():
        return 0
    maximum = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                maximum = max(maximum, int(record.get("sequence", 0) or 0))
    except OSError:
        return 0
    return maximum


def read_context_requests(path: str | Path) -> list[dict[str, Any]]:
    """Merge append-only response updates into their originating requests."""
    source = Path(path)
    if not source.is_file():
        return []

    requests: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    try:
        with source.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                request_id = str(record.get("request_id") or "")
                if not request_id:
                    continue
                if record.get("kind") == "request":
                    item = dict(record)
                    item["status"] = "pending"
                    requests.append(item)
                    by_id[request_id] = item
                elif record.get("kind") == "response" and request_id in by_id:
                    target = by_id[request_id]
                    target["status"] = record.get("status", "completed")
                    target["response_ts"] = record.get("ts")
                    target["duration_seconds"] = record.get("duration_seconds")
                    target["usage"] = record.get("usage") or {}
                    target["finish_reason"] = record.get("finish_reason")
                    if record.get("error"):
                        target["error"] = record["error"]
    except OSError:
        return []
    return requests


def _estimate_tokens(value: Any) -> int:
    """Conservative display-only estimate; API usage remains authoritative."""
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


def _item(
    *,
    category: str,
    label: str,
    content: Any,
    message_index: int | None = None,
    source: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "category": category,
        "label": label,
        "content": content,
        "message_index": message_index,
        "source": source,
        "metadata": metadata or {},
        "estimated_tokens": _estimate_tokens(content),
    }


def classify_request(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten an OpenAI-style request into readable context categories."""
    params = record.get("params") or {}
    messages = params.get("messages") or []
    context = record.get("context") or {}
    system_source = str(
        context.get("system_prompt_source") or "Agent system prompt"
    )
    items: list[dict[str, Any]] = []

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            items.append(_item(
                category="metadata",
                label="Unknown message",
                content=message,
                message_index=index,
            ))
            continue
        role = str(message.get("role") or "unknown")
        content = message.get("content")
        if role == "system":
            if index == 0:
                items.append(_item(
                    category="system_prompt",
                    label="System Prompt",
                    content=content,
                    message_index=index,
                    source=system_source,
                ))
            elif index == 1 and str(content).startswith("[working memory"):
                items.append(_item(
                    category="working_memory",
                    label="Working Memory / System Instruction",
                    content=content,
                    message_index=index,
                    source="Regenerated working memory",
                ))
            else:
                items.append(_item(
                    category="system_instruction",
                    label="System Instruction",
                    content=content,
                    message_index=index,
                    source="Runtime orchestrator",
                ))
            continue

        if role == "user":
            internal = str(content or "").startswith("[") and (
                "not user input" in str(content or "").split("\n", 1)[0].lower()
                or "compacted" in str(content or "").split("\n", 1)[0].lower()
            )
            items.append(_item(
                category="system_instruction" if internal else "user_input",
                label="Internal Instruction" if internal else "User Input",
                content=content,
                message_index=index,
                source="Runtime orchestrator" if internal else "User",
            ))
            continue

        if role == "tool":
            items.append(_item(
                category="tool_result",
                label="Tool Result",
                content=content,
                message_index=index,
                metadata={"tool_call_id": message.get("tool_call_id")},
            ))
            continue

        if role == "assistant":
            reasoning = message.get("reasoning_content")
            if reasoning:
                items.append(_item(
                    category="reasoning",
                    label="Reasoning / Internal Reasoning",
                    content=reasoning,
                    message_index=index,
                ))
            if content:
                items.append(_item(
                    category="assistant_response",
                    label="Assistant Response",
                    content=content,
                    message_index=index,
                ))
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                arguments = function.get("arguments")
                try:
                    parsed_arguments = json.loads(arguments or "{}")
                except (json.JSONDecodeError, TypeError):
                    parsed_arguments = arguments
                items.append(_item(
                    category="tool_call",
                    label=f"Tool Call · {function.get('name', 'unknown')}",
                    content={
                        "name": function.get("name"),
                        "arguments": parsed_arguments,
                    },
                    message_index=index,
                    metadata={"tool_call_id": call.get("id")},
                ))
            if not content and not reasoning and not message.get("tool_calls"):
                items.append(_item(
                    category="assistant_response",
                    label="Assistant Response",
                    content="",
                    message_index=index,
                ))
            continue

        items.append(_item(
            category="metadata",
            label=f"Message · {role}",
            content=content,
            message_index=index,
        ))

    tools = params.get("tools") or []
    if tools:
        items.append(_item(
            category="tool_definition",
            label=f"Available Tool Definitions · {len(tools)}",
            content=tools,
            source="Request tools parameter",
        ))

    request_parameters = {
        key: value
        for key, value in params.items()
        if key not in {"messages", "tools", "model"}
    }
    if request_parameters:
        items.append(_item(
            category="metadata",
            label="Request Parameters",
            content=request_parameters,
            source="Provider request",
        ))
    return items


def request_summary(record: dict[str, Any]) -> dict[str, Any]:
    params = record.get("params") or {}
    usage = record.get("usage") or {}
    return {
        "request_id": record.get("request_id"),
        "sequence": record.get("sequence"),
        "ts": record.get("ts"),
        "response_ts": record.get("response_ts"),
        "duration_seconds": record.get("duration_seconds"),
        "status": record.get("status"),
        "provider": record.get("provider"),
        "model": record.get("model") or params.get("model"),
        "agent_role": (record.get("context") or {}).get(
            "agent_role", "Unclassified"
        ),
        "agent_scope": (record.get("context") or {}).get("agent_scope", ""),
        "phase": (record.get("context") or {}).get("phase", "model_request"),
        "step": (record.get("context") or {}).get("step"),
        "transport_attempt": record.get("transport_attempt", 1),
        "usage": usage,
        "message_count": len(params.get("messages") or []),
        "tool_definition_count": len(params.get("tools") or []),
        "estimated_input_tokens": sum(
            item["estimated_tokens"] for item in classify_request(record)
        ),
        "error": record.get("error"),
    }


def request_detail(record: dict[str, Any]) -> dict[str, Any]:
    return {
        **request_summary(record),
        "context": record.get("context") or {},
        "items": classify_request(record),
        "raw_request": record.get("params") or {},
    }


def context_log_stats(path: str | Path) -> dict[str, Any]:
    requests = read_context_requests(path)
    latest = requests[-1] if requests else None
    return {
        "request_count": len(requests),
        "latest_request_ts": latest.get("ts") if latest else None,
        "latest_model": latest.get("model") if latest else None,
    }


def utc_timestamp() -> float:
    return time.time()
