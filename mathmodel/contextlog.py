"""Durable, standalone logging for exact model API request contexts.

The mathematical-modeling dashboard intentionally does not read this file.
The Context Inspector server exposes it on a separate local port.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

CONTEXT_LOG_FILENAME = "context_requests.jsonl"
CONTEXT_INDEX_FILENAME = "context_requests.index.jsonl"
CONTEXT_INDEX_VERSION = 1
_WM_PROTOCOL_HEADER = (
    "[working memory protocol — append-only-v1; system-managed; immutable]"
)
_WM_SNAPSHOT_HEADER = (
    "[working memory snapshot — append-only-v1; system-managed; not user input]"
)

_index_locks_guard = threading.Lock()
_index_locks: dict[Path, threading.RLock] = {}


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
            encoded = (
                json.dumps(record, ensure_ascii=False, default=str) + "\n"
            ).encode("utf-8")
            with self.path.open("ab") as handle:
                source_offset = handle.tell()
                handle.write(encoded)
            _append_context_index_record(
                self.path,
                _index_record(
                    record,
                    source_offset=source_offset,
                    source_end=source_offset + len(encoded),
                ),
            )


def _last_sequence(path: Path) -> int:
    if not path.is_file():
        return 0
    if _context_index_path(path).is_file():
        summaries = read_context_request_summaries(path)
        return max(
            (int(item.get("sequence", 0) or 0) for item in summaries),
            default=0,
        )
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


def _apply_response(
    target: dict[str, Any],
    record: dict[str, Any],
) -> None:
    target["status"] = record.get("status", "completed")
    target["response_ts"] = record.get("ts")
    target["duration_seconds"] = record.get("duration_seconds")
    target["usage"] = record.get("usage") or {}
    target["finish_reason"] = record.get("finish_reason")
    if record.get("error"):
        target["error"] = record["error"]


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
                    _apply_response(by_id[request_id], record)
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
    tools = params.get("tools") or []
    context = record.get("context") or {}
    system_source = str(
        context.get("system_prompt_source") or "Agent system prompt"
    )
    items: list[dict[str, Any]] = []
    tool_definitions = (
        _item(
            category="tool_definition",
            label=f"Available Tool Definitions · {len(tools)}",
            content=tools,
            source="Request tools parameter",
        )
        if tools
        else None
    )
    tool_definitions_inserted = False

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
            rendered_content = str(content or "")
            if index == 0:
                items.append(_item(
                    category="system_prompt",
                    label="System Prompt",
                    content=content,
                    message_index=index,
                    source=system_source,
                ))
            elif rendered_content.startswith(_WM_PROTOCOL_HEADER):
                items.append(_item(
                    category="working_memory",
                    label="Working Memory Protocol · append-only-v1",
                    content=content,
                    message_index=index,
                    source="Immutable memory protocol",
                    metadata={"memory_kind": "protocol"},
                ))
                if tool_definitions is not None:
                    items.append(tool_definitions)
                    tool_definitions_inserted = True
            elif rendered_content.startswith(_WM_SNAPSHOT_HEADER):
                envelope: dict[str, str] = {}
                for line in rendered_content.splitlines()[1:8]:
                    if line == "---":
                        break
                    key, separator, value = line.partition(":")
                    if separator:
                        envelope[key.strip()] = value.strip()
                epoch = envelope.get("epoch", "?")
                version = envelope.get("version", "?")
                items.append(_item(
                    category="working_memory",
                    label=f"Working Memory Snapshot · E{epoch}.V{version}",
                    content=content,
                    message_index=index,
                    source="Append-only memory snapshot",
                    metadata={"memory_kind": "snapshot", **envelope},
                ))
            elif index == 1 and rendered_content.startswith("[working memory"):
                items.append(_item(
                    category="working_memory",
                    label="Working Memory / System Instruction",
                    content=content,
                    message_index=index,
                    source="Regenerated working memory",
                ))
                if tool_definitions is not None:
                    items.append(tool_definitions)
                    tool_definitions_inserted = True
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

    # OpenAI-compatible APIs carry tool definitions in the request's top-level
    # ``tools`` field rather than inside ``messages``. The inspector presents
    # them at their logical context position: directly after regenerated
    # working memory and before the user input. Requests without working memory
    # retain a safe fallback immediately after the system prompt.
    if tool_definitions is not None and not tool_definitions_inserted:
        system_prompt_index = next(
            (
                index
                for index, item in enumerate(items)
                if item["category"] == "system_prompt"
            ),
            -1,
        )
        items.insert(system_prompt_index + 1, tool_definitions)

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


def _context_index_path(path: str | Path) -> Path:
    return Path(path).with_name(CONTEXT_INDEX_FILENAME)


def _index_lock(path: Path) -> threading.RLock:
    resolved = path.resolve()
    with _index_locks_guard:
        return _index_locks.setdefault(resolved, threading.RLock())


def _index_record(
    record: dict[str, Any],
    *,
    source_offset: int,
    source_end: int,
) -> dict[str, Any]:
    kind = str(record.get("kind") or "checkpoint")
    compact: dict[str, Any] = {
        "version": CONTEXT_INDEX_VERSION,
        "kind": kind,
        "source_offset": source_offset,
        "source_end": source_end,
    }
    request_id = str(record.get("request_id") or "")
    if kind == "request" and request_id:
        pending = dict(record)
        pending["status"] = "pending"
        compact["request_id"] = request_id
        compact["summary"] = request_summary(pending)
    elif kind == "response" and request_id:
        compact["request_id"] = request_id
        compact["response"] = {
            "status": record.get("status", "completed"),
            "ts": record.get("ts"),
            "duration_seconds": record.get("duration_seconds"),
            "usage": record.get("usage") or {},
            "finish_reason": record.get("finish_reason"),
            "error": record.get("error"),
        }
    else:
        compact["kind"] = "checkpoint"
    return compact


def _append_context_index_record(
    source: Path,
    compact: dict[str, Any],
) -> None:
    index_path = _context_index_path(source)
    encoded = (
        json.dumps(compact, ensure_ascii=False, default=str) + "\n"
    ).encode("utf-8")
    with _index_lock(index_path):
        with index_path.open("ab") as handle:
            handle.write(encoded)


def _scan_context_log(
    source: Path,
    *,
    start: int = 0,
) -> list[dict[str, Any]]:
    compact_records: list[dict[str, Any]] = []
    try:
        with source.open("rb") as handle:
            handle.seek(start)
            while True:
                source_offset = handle.tell()
                line = handle.readline()
                if not line:
                    break
                # A recorder in another process may still be writing the last
                # record. Leave that tail unindexed until its newline arrives
                # instead of turning half a JSON object into a checkpoint.
                if not line.endswith(b"\n"):
                    break
                source_end = handle.tell()
                try:
                    record = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    record = {"kind": "checkpoint"}
                compact_records.append(_index_record(
                    record,
                    source_offset=source_offset,
                    source_end=source_end,
                ))
    except OSError:
        return []
    return compact_records


def _write_context_index(
    index_path: Path,
    records: list[dict[str, Any]],
) -> None:
    temporary = index_path.with_name(
        f".{index_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("wb") as handle:
            for record in records:
                handle.write((
                    json.dumps(record, ensure_ascii=False, default=str) + "\n"
                ).encode("utf-8"))
        os.replace(temporary, index_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_context_index(
    index_path: Path,
) -> tuple[list[dict[str, Any]], int, bool]:
    raw_records: dict[int, dict[str, Any]] = {}
    if not index_path.is_file():
        return [], 0, False
    try:
        with index_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return [], 0, False
                if record.get("version") != CONTEXT_INDEX_VERSION:
                    return [], 0, False
                try:
                    source_offset = int(record["source_offset"])
                    source_end = int(record["source_end"])
                except (KeyError, TypeError, ValueError):
                    return [], 0, False
                if source_offset < 0 or source_end <= source_offset:
                    return [], 0, False
                raw_records[source_offset] = record
    except OSError:
        return [], 0, False

    ordered = [raw_records[offset] for offset in sorted(raw_records)]
    expected_offset = 0
    for record in ordered:
        if int(record["source_offset"]) != expected_offset:
            return [], expected_offset, False
        expected_offset = int(record["source_end"])

    requests: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for record in ordered:
        request_id = str(record.get("request_id") or "")
        if record.get("kind") == "request" and request_id:
            item = {
                "summary": dict(record.get("summary") or {}),
                "request_offset": int(record["source_offset"]),
                "response_offset": None,
            }
            requests.append(item)
            by_id[request_id] = item
        elif record.get("kind") == "response" and request_id in by_id:
            response = dict(record.get("response") or {})
            _apply_response(by_id[request_id]["summary"], response)
            by_id[request_id]["response_offset"] = int(
                record["source_offset"]
            )
    return requests, expected_offset, True


def _ensure_context_index(
    path: str | Path,
) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.is_file():
        return []
    index_path = _context_index_path(source)
    with _index_lock(index_path):
        source_size = source.stat().st_size
        requests, indexed_end, valid = _read_context_index(index_path)
        if not valid or indexed_end > source_size:
            _write_context_index(index_path, _scan_context_log(source))
        elif indexed_end < source_size:
            for compact in _scan_context_log(source, start=indexed_end):
                _append_context_index_record(source, compact)
        requests, indexed_end, valid = _read_context_index(index_path)
        if not valid or indexed_end > source.stat().st_size:
            _write_context_index(index_path, _scan_context_log(source))
            requests, _indexed_end, valid = _read_context_index(index_path)
        return requests if valid else []


def build_context_index(
    path: str | Path,
    *,
    force: bool = False,
) -> Path:
    """Build or increment the compact request index for one context log."""
    source = Path(path)
    index_path = _context_index_path(source)
    if force:
        with _index_lock(index_path):
            _write_context_index(index_path, _scan_context_log(source))
    else:
        _ensure_context_index(source)
    return index_path


def read_context_request_summaries(
    path: str | Path,
) -> list[dict[str, Any]]:
    """Read lightweight request summaries without loading full prompts."""
    return [
        dict(item["summary"])
        for item in _ensure_context_index(path)
    ]


def _read_record_at(source: Path, offset: int | None) -> dict[str, Any]:
    if offset is None:
        return {}
    try:
        with source.open("rb") as handle:
            handle.seek(offset)
            return json.loads(
                handle.readline().decode("utf-8", errors="replace")
            )
    except (OSError, json.JSONDecodeError):
        return {}


def read_context_request(
    path: str | Path,
    request_id: str,
) -> dict[str, Any] | None:
    """Read one request directly using byte offsets from the compact index."""
    source = Path(path)
    indexed = next(
        (
            item for item in _ensure_context_index(source)
            if str(item["summary"].get("request_id") or "") == request_id
        ),
        None,
    )
    if indexed is None:
        return None
    record = _read_record_at(source, indexed["request_offset"])
    if not record:
        return None
    record["status"] = "pending"
    response = _read_record_at(source, indexed["response_offset"])
    if response:
        _apply_response(record, response)
    return record


def request_detail(record: dict[str, Any]) -> dict[str, Any]:
    return {
        **request_summary(record),
        "context": record.get("context") or {},
        "items": classify_request(record),
        "raw_request": record.get("params") or {},
    }


def context_log_stats(path: str | Path) -> dict[str, Any]:
    requests = read_context_request_summaries(path)
    latest = requests[-1] if requests else None
    return {
        "request_count": len(requests),
        "latest_request_ts": latest.get("ts") if latest else None,
        "latest_model": latest.get("model") if latest else None,
    }


def utc_timestamp() -> float:
    return time.time()
