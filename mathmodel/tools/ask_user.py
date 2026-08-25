"""Durable human-in-the-loop questions for the lead Agent.

``ask_user`` no longer parks a Python thread on an in-memory Event.  The tool
writes a durable question, returns a private suspension marker to the Agent
loop, and the loop checkpoints the open tool call before releasing its worker.
The dashboard later appends the matching tool result and resumes the same
transcript.  A process restart between asking and answering is therefore safe.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..project_state import attach_change_question, register_change_request
from .base import Tool, ToolContext

_LOCK = threading.RLock()
_RESULT_PREFIX = "[ask_user_result] "
PENDING_OBSERVATION_PREFIX = "[ask_user_pending] "
PENDING_QUESTION_FILENAME = "pending_question.json"
ANSWERED_QUESTION_FILENAME = "answered_question.json"

_PARAMS = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["question", "change_confirmation"],
            "description": (
                "Use change_confirmation when a follow-up would change the model, "
                "parameters, computation, artifacts, or paper. Otherwise use question."
            ),
        },
        "title": {
            "type": "string",
            "description": "Short card title, especially for a change confirmation.",
        },
        "summary": {
            "type": "string",
            "description": "One concise sentence describing the proposed change.",
        },
        "question": {"type": "string", "description": "The decision to show the user."},
        "impacts": {
            "type": "array",
            "description": "Concrete downstream areas that will be recalculated or updated.",
            "items": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "change": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["target", "change"],
            },
        },
        "budget": {
            "type": "object",
            "description": "A user-facing estimate/cap. Backend enforcement remains authoritative.",
            "properties": {
                "currency": {"type": "string"},
                "estimated_additional_cost": {"type": "number"},
                "max_additional_cost": {"type": "number"},
                "note": {"type": "string"},
            },
        },
        "options": {
            "type": "array",
            "description": (
                "2-5 concrete choices. For change_confirmation use stable ids "
                "confirm, adjust, cancel in that order."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "description": {
                        "type": "string",
                        "description": "Optional detail shown under the label.",
                    },
                },
                "required": ["label"],
            },
        },
        "allow_custom": {
            "type": "boolean",
            "description": (
                "Whether the user may type a free-text answer instead of picking "
                "an option (default true)."
            ),
        },
    },
    "required": ["question"],
}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    os.replace(temporary, path)


def _pending_path(workdir: Path) -> Path:
    return workdir / PENDING_QUESTION_FILENAME


def _answered_path(workdir: Path) -> Path:
    return workdir / ANSWERED_QUESTION_FILENAME


def is_pending_observation(observation: str) -> bool:
    return observation.startswith(PENDING_OBSERVATION_PREFIX)


def pending_record_from_observation(observation: str) -> dict[str, Any]:
    if not is_pending_observation(observation):
        raise ValueError("not an ask_user suspension marker")
    try:
        payload = json.loads(observation[len(PENDING_OBSERVATION_PREFIX):])
    except json.JSONDecodeError as exc:
        raise ValueError("invalid ask_user suspension marker") from exc
    if not isinstance(payload, dict) or not payload.get("id"):
        raise ValueError("invalid ask_user suspension payload")
    return payload


def bind_pending_tool_call(
    workdir: Path,
    question_id: str,
    tool_call_id: str,
) -> dict[str, Any]:
    """Attach the provider tool-call id before the Agent releases its worker."""
    with _LOCK:
        path = _pending_path(workdir)
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("待回答问题未能持久化。") from exc
        if record.get("id") != question_id:
            raise ValueError("待回答问题已经变化，不能绑定旧工具调用。")
        record["tool_call_id"] = tool_call_id
        _atomic_write(path, record)
        return record


def claim_pending_answer(
    workdir: Path,
    question_id: str,
    *,
    answer: str | None = None,
    option_id: str | None = None,
) -> dict[str, Any]:
    """Atomically validate and claim an answer for a suspended question."""
    with _LOCK:
        path = _pending_path(workdir)
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("当前没有可回答的问题。") from exc
        if record.get("id") != question_id:
            raise ValueError("这个问题已经不再等待回答。")
        tool_call_id = str(record.get("tool_call_id") or "")
        if not tool_call_id:
            raise ValueError("问题尚未完成检查点保存，请稍后再试。")

        if option_id is not None:
            option = next(
                (
                    item for item in record.get("options", [])
                    if item.get("id") == option_id
                ),
                None,
            )
            if option is None:
                raise ValueError("所选选项不存在或已经过期。")
            result = {"answer": option["label"], "option_id": option["id"]}
        else:
            custom_answer = (answer or "").strip()
            if not custom_answer or not record.get("allow_custom", True):
                raise ValueError("请输入有效回答，或选择一个可用选项。")
            result = {"answer": custom_answer, "option_id": None}

        if record.get("kind") == "change_confirmation":
            selected = result["option_id"]
            action = selected if selected in {"confirm", "adjust", "cancel"} else "adjust"
            change_request_id = str(record.get("change_request_id") or "")
            if not change_request_id:
                raise ValueError("修改确认卡片缺少对应的修改请求。")
            result["change_action"] = action

        record["result"] = result
        record["answered_at"] = time.time()
        _atomic_write(_answered_path(workdir), record)
        path.unlink(missing_ok=True)
        return record


def restore_claimed_answer(workdir: Path, record: dict[str, Any]) -> None:
    """Restore a claimed question if the continuation worker cannot start."""
    with _LOCK:
        restored = dict(record)
        restored.pop("result", None)
        restored.pop("answered_at", None)
        _atomic_write(_pending_path(workdir), restored)
        _answered_path(workdir).unlink(missing_ok=True)


def complete_claimed_answer(workdir: Path) -> None:
    _answered_path(workdir).unlink(missing_ok=True)


def make_ask_user_tool(
    on_event: Callable[[str, dict], None] | None = None,
    poll_interval: float = 1.0,
) -> Tool:
    del poll_interval  # retained for API compatibility with older builders

    def _ask_user(ctx: ToolContext, args: dict) -> str:
        kind = str(args.get("kind") or "question")
        question = str(args["question"])
        options = [
            {
                "id": str(option.get("id") or f"option-{index + 1}"),
                "label": str(option.get("label") or f"Option {index + 1}"),
                "description": str(option.get("description") or ""),
            }
            for index, option in enumerate(args.get("options") or [])
        ]
        if kind == "change_confirmation" and not options:
            options = [
                {"id": "confirm", "label": "确认重算", "description": "按上述影响范围开始修改"},
                {"id": "adjust", "label": "调整要求", "description": "补充或缩小本次修改范围"},
                {"id": "cancel", "label": "取消", "description": "保留当前稳定版本"},
            ]
        allow_custom = bool(args.get("allow_custom", kind != "change_confirmation"))
        qid = uuid.uuid4().hex[:12]
        impacts = [
            {
                "target": str(item.get("target") or ""),
                "change": str(item.get("change") or ""),
                "reason": str(item.get("reason") or ""),
            }
            for item in (args.get("impacts") or [])
            if isinstance(item, dict)
        ]
        budget = args.get("budget") if isinstance(args.get("budget"), dict) else None
        title = str(args.get("title") or ("模型修改请求" if kind == "change_confirmation" else "需要你的确认"))
        summary = str(args.get("summary") or "")
        change_request_id = None
        if kind == "change_confirmation":
            request = register_change_request(
                ctx.workdir,
                title=title,
                summary=summary,
                impacts=impacts,
                budget=budget,
            )
            change_request_id = request["id"]
            budget = request.get("budget")
            attach_change_question(ctx.workdir, change_request_id, qid)

        record = {
            "id": qid,
            "kind": kind,
            "title": title,
            "summary": summary,
            "question": question,
            "impacts": impacts,
            "budget": budget,
            "options": options,
            "allow_custom": allow_custom,
            "change_request_id": change_request_id,
            "asked_at": time.time(),
            "tool_call_id": None,
        }
        with _LOCK:
            _atomic_write(_pending_path(ctx.workdir), record)
        if on_event is not None:
            on_event("ask", record)
        return PENDING_OBSERVATION_PREFIX + json.dumps(
            {"id": qid},
            ensure_ascii=False,
        )

    return Tool(
        name="ask_user",
        description=(
            "Ask the user for a real decision and suspend durably until they answer. "
            "For any follow-up that would change model assumptions, parameters, "
            "computation, result files, figures, or paper claims, first use kind "
            "change_confirmation and describe the impact and budget. Use option ids "
            "confirm/adjust/cancel with labels 确认重算/调整要求/取消. Do not create a "
            "separate intent-classification workflow: judge the need from the current "
            "project context. Never combine ask_user with other tool calls in one turn."
        ),
        parameters=_PARAMS,
        handler=_ask_user,
    )
