"""ask_user tool: pause the lead agent and let a human pick an option (or type a
custom answer), Claude-Code-style elicitation.

The handler blocks its calling thread until an answer arrives via
`submit_answer()`, which the dashboard's HTTP layer calls when the user
responds in the UI. This only works because each run already lives in its own
background thread (see dashboard/server.py) -- blocking here just pauses that
one run's thread, not the whole process.

Lead-only: sub-agents run unattended and must never block the whole delegation
on a human who never saw their context. Only one question may be pending per
run at a time (enforced by keying `_pending` on the run id, i.e. the workdir
name) -- the tool description tells the model not to combine this with other
tool calls in the same turn.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .base import Tool, ToolContext

_lock = threading.Lock()
_pending: dict[str, dict[str, Any]] = {}  # run_id -> one trusted pending response
_RESULT_PREFIX = "[ask_user_result] "

_PARAMS = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "The question to show the user."},
        "options": {
            "type": "array",
            "description": "2-5 concrete choices. Omit for a free-text-only question.",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "description": {"type": "string", "description": "Optional detail shown under the label."},
                },
                "required": ["label"],
            },
        },
        "allow_custom": {
            "type": "boolean",
            "description": "Whether the user may also type a free-text answer instead of "
            "picking an option (default true).",
        },
    },
    "required": ["question"],
}


def _run_id_of(ctx: ToolContext) -> str:
    return ctx.workdir.name


def _pending_path(ctx: ToolContext) -> Path:
    return ctx.workdir / "pending_question.json"


def submit_answer(
    run_id: str,
    question_id: str,
    *,
    answer: str | None = None,
    option_id: str | None = None,
) -> bool:
    """Called by the HTTP layer when the user responds.

    Returns False if there is no matching pending question (already answered,
    the run was stopped, or the id is stale) so the caller can report that.
    """
    with _lock:
        entry = _pending.get(run_id)
        if not entry or entry["id"] != question_id:
            return False
        if option_id is not None:
            option = next(
                (
                    item for item in entry["options"]
                    if item["id"] == option_id
                ),
                None,
            )
            if option is None:
                return False
            result = {
                "answer": option["label"],
                "option_id": option["id"],
            }
        else:
            custom_answer = (answer or "").strip()
            if not custom_answer or not entry["allow_custom"]:
                return False
            result = {
                "answer": custom_answer,
                "option_id": None,
            }
        entry["result"] = result
        entry["event"].set()
        return True


def make_ask_user_tool(
    on_event: Callable[[str, dict], None] | None = None,
    poll_interval: float = 1.0,
) -> Tool:
    def _ask_user(ctx: ToolContext, args: dict) -> str:
        run_id = _run_id_of(ctx)
        question = args["question"]
        options = [
            {
                "id": str(option.get("id") or f"option-{index + 1}"),
                "label": str(option.get("label") or f"Option {index + 1}"),
                "description": str(option.get("description") or ""),
            }
            for index, option in enumerate(args.get("options") or [])
        ]
        allow_custom = bool(args.get("allow_custom", True))
        qid = uuid.uuid4().hex[:12]

        event = threading.Event()
        entry: dict[str, Any] = {
            "id": qid,
            "event": event,
            "result": None,
            "options": options,
            "allow_custom": allow_custom,
        }
        with _lock:
            _pending[run_id] = entry

        record = {
            "id": qid,
            "question": question,
            "options": [
                {
                    "id": option["id"],
                    "label": option["label"],
                    "description": option.get("description", ""),
                }
                for option in options
            ],
            "allow_custom": allow_custom,
            "asked_at": time.time(),
        }
        _pending_path(ctx).write_text(json.dumps(record, ensure_ascii=False, indent=2))
        if on_event is not None:
            on_event("ask", record)

        answered = False
        try:
            while True:
                if event.wait(timeout=poll_interval):
                    answered = True
                    break
                if ctx.stop_event.is_set():
                    break
        finally:
            with _lock:
                if _pending.get(run_id) is entry:
                    _pending.pop(run_id, None)
            _pending_path(ctx).unlink(missing_ok=True)
            if on_event is not None:
                resolution = {"id": qid, "answered": answered}
                if answered and entry["result"]:
                    resolution.update({
                        "selected_option_id": entry["result"]["option_id"],
                    })
                on_event("ask_resolved", resolution)

        if not answered:
            return "[error] run was stopped before the question was answered"
        return _RESULT_PREFIX + json.dumps(entry["result"], ensure_ascii=False)

    return Tool(
        name="ask_user",
        description="Pause and ask the user a question with concrete options (they "
        "may also type a custom answer). Use when a real decision needs the user's "
        "input -- e.g. which of several plausible interpretations of an ambiguous "
        "requirement to use, or a modeling choice with a real tradeoff -- not for "
        "things you can reasonably decide yourself. This call blocks until the user "
        "responds, so never combine it with other tool calls in the same turn.",
        parameters=_PARAMS,
        handler=_ask_user,
    )
