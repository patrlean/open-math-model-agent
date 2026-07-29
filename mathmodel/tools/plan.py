"""Structured todo plan + decision log -- the agent's externalized working memory.

The plan is a list of tasks in plan.json (the source of truth). Updating progress
is deliberately low-friction: `set_task_status(id, "done", result=...)` flips one
task, so the agent never has to rewrite the whole plan and rarely forgets. A
markdown mirror (plan.md) is rendered for humans / the dashboard.

- plan_write      : create/replace the task list (once, up front, and to restructure).
- set_task_status : flip one task's status (+ optional key result). Call it the
                    moment a sub-task's state changes.
- log_decision    : append what was tried/decided and why, so dead-ends survive
                    context compaction and are not re-attempted.
"""

from __future__ import annotations

import json
import os
import time
import threading
import uuid
from pathlib import Path

from .base import Tool, ToolContext

STATUSES = ("pending", "in_progress", "done", "blocked")
_ICON = {"pending": "☐", "in_progress": "◐", "done": "☑", "blocked": "⊘"}

# A lead agent may emit several set_task_status calls in one turn. The agent
# dispatches those calls in parallel, so plan state needs its own per-workdir
# critical section rather than relying on the caller to serialize writes.
_PLAN_LOCKS: dict[Path, threading.RLock] = {}
_PLAN_LOCKS_GUARD = threading.Lock()


def _plan_lock(workdir: Path) -> threading.RLock:
    key = workdir.resolve()
    with _PLAN_LOCKS_GUARD:
        lock = _PLAN_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PLAN_LOCKS[key] = lock
        return lock


def plan_path(workdir: Path) -> Path:
    return workdir / "plan.json"


def _load_tasks(workdir: Path) -> list[dict]:
    """Read plan tasks while the caller holds the per-workdir plan lock."""
    p = plan_path(workdir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data.get("tasks", []) if isinstance(data, dict) else []
    except json.JSONDecodeError:
        return []


def load_tasks(workdir: Path) -> list[dict]:
    """Return a consistent task snapshot, even while another call saves a plan."""
    with _plan_lock(workdir):
        return _load_tasks(workdir)


def render_markdown(tasks: list[dict]) -> str:
    lines = ["# Plan"]
    for t in tasks:
        icon = _ICON.get(t.get("status", "pending"), "☐")
        line = f"- {icon} [{t.get('id','?')}] {t.get('title','')}"
        if t.get("result"):
            line += f" — {t['result']}"
        elif t.get("note"):
            line += f"  ({t['note']})"
        lines.append(line)
    return "\n".join(lines) + "\n"


def render_compact(tasks: list[dict]) -> str:
    if not tasks:
        return "(no plan yet — create one with plan_write)"
    out = []
    for t in tasks:
        icon = _ICON.get(t.get("status", "pending"), "☐")
        seg = f"{icon} {t.get('id','?')}  {t.get('title','')}"
        if t.get("result"):
            seg += f"  — {t['result']}"
        out.append(seg)
    return "\n".join(out)


def _atomic_write(path: Path, content: str) -> None:
    """Replace a file in one operation so readers never see a partial JSON file."""
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(content)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


def _save(workdir: Path, tasks: list[dict]) -> None:
    """Save JSON source of truth and Markdown mirror while the plan lock is held."""
    _atomic_write(plan_path(workdir), json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2))
    _atomic_write(workdir / "plan.md", render_markdown(tasks))  # human/dashboard mirror


def _plan_write(ctx: ToolContext, args: dict) -> str:
    tasks = args["tasks"]
    norm = []
    for t in tasks:
        st = t.get("status", "pending")
        if st not in STATUSES:
            st = "pending"
        norm.append({
            "id": str(t["id"]), "title": t.get("title", ""),
            "status": st, "note": t.get("note", ""), "result": t.get("result", ""),
        })
    with _plan_lock(ctx.workdir):
        _save(ctx.workdir, norm)
    return f"plan set: {len(norm)} tasks ({', '.join(t['id'] for t in norm)})."


def _set_task_status(ctx: ToolContext, args: dict) -> str:
    tid = str(args["id"])
    status = args["status"]
    if status not in STATUSES:
        return f"[error] status must be one of {STATUSES}"
    with _plan_lock(ctx.workdir):
        tasks = _load_tasks(ctx.workdir)
        for t in tasks:
            if t.get("id") == tid:
                t["status"] = status
                if "result" in args and args["result"]:
                    t["result"] = args["result"]
                _save(ctx.workdir, tasks)
                return f"task {tid} -> {status}."
        return f"[error] no task id '{tid}'. Known ids: {[t.get('id') for t in tasks]}"


def _log_decision(ctx: ToolContext, args: dict) -> str:
    what = args["what"].strip()
    why = args.get("why", "").strip()
    stamp = time.strftime("%H:%M:%S")
    block = f"- [{stamp}] {what}"
    if why:
        block += f"\n  - why: {why}"
    path = ctx.workdir / "decisions.md"
    prefix = "" if path.exists() else "# Decision Log\n\n"
    with path.open("a") as f:
        f.write(prefix + block + "\n")
    return "decision logged."


plan_write_tool = Tool(
    name="plan_write",
    description="Create or replace the todo plan as a list of tasks. Call once up "
    "front (one task per sub-problem) and again only to restructure. Each task: "
    "{id, title, status?, note?}.",
    parameters={
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "short stable id, e.g. 'q2'"},
                        "title": {"type": "string"},
                        "status": {"type": "string", "enum": list(STATUSES)},
                        "note": {"type": "string", "description": "optional method/plan note"},
                    },
                    "required": ["id", "title"],
                },
            }
        },
        "required": ["tasks"],
    },
    handler=_plan_write,
)

set_task_status_tool = Tool(
    name="set_task_status",
    description="Flip ONE task's status (and optionally record its key result). Call "
    "the moment a task changes state — e.g. set_task_status('q2','done', "
    "result='max shielding 4.83s'). This is the low-friction way to keep the plan live.",
    parameters={
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "status": {"type": "string", "enum": list(STATUSES)},
            "result": {"type": "string", "description": "optional short key result"},
        },
        "required": ["id", "status"],
    },
    handler=_set_task_status,
)

log_decision_tool = Tool(
    name="log_decision",
    description="Append a decision/finding to decisions.md so it survives context "
    "compaction (e.g. 'ruled out exact MILP: >2h on full instance; using LP relaxation').",
    parameters={
        "type": "object",
        "properties": {
            "what": {"type": "string", "description": "What was tried/decided."},
            "why": {"type": "string", "description": "Reasoning / evidence."},
        },
        "required": ["what"],
    },
    handler=_log_decision,
)
