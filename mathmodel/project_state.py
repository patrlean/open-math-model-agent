"""Durable project, revision, and change-request state.

The dashboard historically treated one workspace directory as one chat run.
The product model is now a project: follow-up requests may create revisions,
while the last accepted revision remains the stable delivery until a newer one
finishes verification.  This module deliberately stays file-backed so the
local app and a future database-backed service share the same state machine.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

PROJECT_STATE_FILENAME = "project.json"
PROJECT_SCHEMA_VERSION = 1
DEFAULT_REVISION_BUDGET_LIMIT_CNY = 40.0
LEGACY_DEFAULT_REVISION_BUDGET_LIMIT_CNY = 10.0
MIN_REVISION_BUDGET_LIMIT_CNY = 0.1
MAX_REVISION_BUDGET_LIMIT_CNY = 10_000.0

_LOCK = threading.RLock()
_SNAPSHOT_DIRECTORIES = (
    "data",
    "assets",
    "src",
    "results",
    "figures",
    "paper",
    "verification",
)
_SNAPSHOT_FILES = (
    "problem.md",
    "decisions.md",
    "plan.md",
    "plan.json",
    ".paper-profile.json",
)


class ProjectStateError(ValueError):
    """Raised when a project transition is invalid or stale."""


def _now() -> float:
    return time.time()


def _state_path(workdir: Path) -> Path:
    return workdir / PROJECT_STATE_FILENAME


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    os.replace(temporary, path)


def _initial_revision(created_at: float) -> dict[str, Any]:
    return {
        "id": "rev_0001",
        "number": 1,
        "parent_revision_id": None,
        "trigger_type": "initial",
        "title": "初始求解",
        "summary": "项目的初始模型与交付结果",
        "status": "draft",
        "created_at": created_at,
        "updated_at": created_at,
        "completed_at": None,
        "change_request_id": None,
    }


def _new_state(workdir: Path, *, title: str, created_at: float) -> dict[str, Any]:
    revision = _initial_revision(created_at)
    return {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "id": workdir.name,
        "title": title or "未命名项目",
        "created_at": created_at,
        "updated_at": created_at,
        # The initial revision is both active and current because there is no
        # prior accepted delivery to preserve yet.
        "current_revision_id": revision["id"],
        "active_revision_id": revision["id"],
        "next_revision_number": 2,
        "settings": {
            "revision_budget_limit_cny": DEFAULT_REVISION_BUDGET_LIMIT_CNY,
            "revision_budget_limit_custom": False,
        },
        "revisions": [revision],
        "change_requests": [],
    }


def _read_unlocked(workdir: Path) -> dict[str, Any] | None:
    path = _state_path(workdir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectStateError("项目版本记录损坏，无法继续修改。") from exc
    if not isinstance(payload, dict):
        raise ProjectStateError("项目版本记录格式无效。")
    return payload


def ensure_project(
    workdir: Path,
    *,
    title: str = "",
    created_at: float | None = None,
) -> dict[str, Any]:
    """Return the durable project state, bootstrapping legacy workspaces."""
    workdir.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        state = _read_unlocked(workdir)
        if state is None:
            timestamp = float(created_at or _now())
            state = _new_state(workdir, title=title, created_at=timestamp)
            _atomic_write(_state_path(workdir), state)
        elif title and state.get("title") != title:
            state["title"] = title
            state["updated_at"] = _now()
            _atomic_write(_state_path(workdir), state)
        if not isinstance(state.get("settings"), dict):
            state["settings"] = {}
        if "revision_budget_limit_cny" not in state["settings"]:
            state["settings"]["revision_budget_limit_cny"] = (
                DEFAULT_REVISION_BUDGET_LIMIT_CNY
            )
            state["settings"]["revision_budget_limit_custom"] = False
            state["updated_at"] = _now()
            _atomic_write(_state_path(workdir), state)
        elif "revision_budget_limit_custom" not in state["settings"]:
            current_limit = float(state["settings"]["revision_budget_limit_cny"])
            is_legacy_default = current_limit == LEGACY_DEFAULT_REVISION_BUDGET_LIMIT_CNY
            state["settings"]["revision_budget_limit_custom"] = not is_legacy_default
            if is_legacy_default:
                state["settings"]["revision_budget_limit_cny"] = (
                    DEFAULT_REVISION_BUDGET_LIMIT_CNY
                )
            state["updated_at"] = _now()
            _atomic_write(_state_path(workdir), state)
        return copy.deepcopy(state)


def project_view(
    workdir: Path,
    *,
    title: str = "",
    created_at: float | None = None,
) -> dict[str, Any]:
    """Return a UI-safe project snapshot with resolved revision pointers."""
    state = ensure_project(workdir, title=title, created_at=created_at)
    revisions = {
        revision.get("id"): revision
        for revision in state.get("revisions", [])
        if isinstance(revision, dict)
    }
    state["current_revision"] = revisions.get(state.get("current_revision_id"))
    state["active_revision"] = revisions.get(state.get("active_revision_id"))
    state["revisions"] = sorted(
        revisions.values(),
        key=lambda revision: int(revision.get("number", 0)),
        reverse=True,
    )
    state["change_requests"] = sorted(
        [
            item for item in state.get("change_requests", [])
            if isinstance(item, dict)
        ],
        key=lambda item: float(item.get("created_at", 0)),
        reverse=True,
    )
    return state


def register_change_request(
    workdir: Path,
    *,
    title: str,
    summary: str,
    impacts: list[dict[str, Any]],
    budget: dict[str, Any] | None,
    user_request: str = "",
) -> dict[str, Any]:
    """Persist a proposed change before any expensive mutation begins."""
    with _LOCK:
        state = ensure_project(workdir)
        timestamp = _now()
        configured_cap = float(
            (state.get("settings") or {}).get(
                "revision_budget_limit_cny",
                DEFAULT_REVISION_BUDGET_LIMIT_CNY,
            )
        )
        normalized_budget = copy.deepcopy(budget) if budget else {}
        if (
            "estimated_additional_cost" not in normalized_budget
            and normalized_budget.get("max_additional_cost") is not None
        ):
            normalized_budget["estimated_additional_cost"] = normalized_budget[
                "max_additional_cost"
            ]
        normalized_budget.update({
            "currency": "CNY",
            "max_additional_cost": configured_cap,
            "limit_source": "project_setting",
        })
        request = {
            "id": f"chg_{uuid.uuid4().hex[:12]}",
            "base_revision_id": state["current_revision_id"],
            "revision_id": None,
            "status": "pending",
            "title": title or "模型修改请求",
            "summary": summary,
            "impacts": copy.deepcopy(impacts),
            "budget": normalized_budget,
            "user_request": user_request,
            "question_id": None,
            "selected_option_id": None,
            "answer": None,
            "created_at": timestamp,
            "updated_at": timestamp,
            "resolved_at": None,
        }
        state.setdefault("change_requests", []).append(request)
        state["updated_at"] = timestamp
        _atomic_write(_state_path(workdir), state)
        return copy.deepcopy(request)


def project_budget_settings(workdir: Path) -> dict[str, Any]:
    state = ensure_project(workdir)
    value = float(
        (state.get("settings") or {}).get(
            "revision_budget_limit_cny",
            DEFAULT_REVISION_BUDGET_LIMIT_CNY,
        )
    )
    return {
        "revision_budget_limit_cny": value,
        "default_revision_budget_limit_cny": DEFAULT_REVISION_BUDGET_LIMIT_CNY,
        "min_revision_budget_limit_cny": MIN_REVISION_BUDGET_LIMIT_CNY,
        "max_revision_budget_limit_cny": MAX_REVISION_BUDGET_LIMIT_CNY,
        "currency": "CNY",
    }


def update_project_budget_limit(workdir: Path, value: float) -> dict[str, Any]:
    try:
        normalized = round(float(value), 2)
    except (TypeError, ValueError) as exc:
        raise ProjectStateError("追加费用上限必须是有效数字。") from exc
    if not (
        MIN_REVISION_BUDGET_LIMIT_CNY
        <= normalized
        <= MAX_REVISION_BUDGET_LIMIT_CNY
    ):
        raise ProjectStateError(
            "追加费用上限必须在 "
            f"¥{MIN_REVISION_BUDGET_LIMIT_CNY:.2f}–"
            f"¥{MAX_REVISION_BUDGET_LIMIT_CNY:.2f} 之间。"
        )
    with _LOCK:
        state = ensure_project(workdir)
        state.setdefault("settings", {})[
            "revision_budget_limit_cny"
        ] = normalized
        state["settings"]["revision_budget_limit_custom"] = (
            normalized != DEFAULT_REVISION_BUDGET_LIMIT_CNY
        )
        state["updated_at"] = _now()
        active = next(
            (
                revision for revision in state.get("revisions", [])
                if revision.get("id") == state.get("active_revision_id")
            ),
            None,
        )
        if active and active.get("status") in {
            "draft", "running", "waiting_input", "stopped"
        } and isinstance(active.get("budget"), dict):
            active["budget"]["max_additional_cost"] = normalized
            active["budget"]["limit_source"] = "project_setting"
            active["updated_at"] = state["updated_at"]
        _atomic_write(_state_path(workdir), state)
    return project_budget_settings(workdir)


def attach_change_question(
    workdir: Path,
    change_request_id: str,
    question_id: str,
) -> None:
    with _LOCK:
        state = ensure_project(workdir)
        request = _find_change_request(state, change_request_id)
        if request["status"] != "pending":
            raise ProjectStateError("修改请求已经处理，不能再次绑定确认卡片。")
        request["question_id"] = question_id
        request["updated_at"] = _now()
        state["updated_at"] = request["updated_at"]
        _atomic_write(_state_path(workdir), state)


def resolve_change_request(
    workdir: Path,
    change_request_id: str,
    *,
    action: str,
    answer: str,
    selected_option_id: str | None,
    usage_baseline_cny: float | None = None,
) -> dict[str, Any]:
    """Resolve a change card and create a draft revision only on confirmation.

    Repeating the same answer is idempotent; a conflicting second answer is
    rejected.  This protects the HTTP endpoint from double-clicks and retries.
    """
    if action not in {"confirm", "adjust", "cancel"}:
        raise ProjectStateError("未知的修改确认操作。")
    with _LOCK:
        state = ensure_project(workdir)
        request = _find_change_request(state, change_request_id)
        prior_action = request.get("resolution_action")
        if request.get("resolved_at") is not None:
            if prior_action == action:
                return copy.deepcopy(request)
            raise ProjectStateError("这个修改请求已经由另一个操作处理。")

        timestamp = _now()
        request["resolution_action"] = action
        request["selected_option_id"] = selected_option_id
        request["answer"] = answer
        request["resolved_at"] = timestamp
        request["updated_at"] = timestamp

        if action == "confirm":
            _snapshot_revision_unlocked(
                workdir,
                state,
                str(request["base_revision_id"]),
            )
            number = int(state.get("next_revision_number", 2))
            revision_id = f"rev_{number:04d}"
            revision = {
                "id": revision_id,
                "number": number,
                "parent_revision_id": request["base_revision_id"],
                "trigger_type": "change",
                "title": request["title"],
                "summary": request["summary"],
                "status": "draft",
                "created_at": timestamp,
                "updated_at": timestamp,
                "completed_at": None,
                "change_request_id": request["id"],
                "budget": copy.deepcopy(request.get("budget")),
                "usage_baseline_cny": max(0.0, float(usage_baseline_cny or 0.0)),
            }
            state.setdefault("revisions", []).append(revision)
            state["next_revision_number"] = number + 1
            state["active_revision_id"] = revision_id
            request["status"] = "confirmed"
            request["revision_id"] = revision_id
        elif action == "adjust":
            request["status"] = "adjusted"
        else:
            request["status"] = "cancelled"

        state["updated_at"] = timestamp
        _atomic_write(_state_path(workdir), state)
        return copy.deepcopy(request)


def update_active_revision_status(workdir: Path, run_status: str) -> None:
    """Mirror execution status without promoting an unverified revision."""
    mapped = {
        "running": "running",
        "waiting_input": "waiting_input",
        "stopped": "stopped",
        "cancelled": "cancelled",
        "error": "failed",
    }.get(run_status)
    if mapped is None:
        return
    with _LOCK:
        state = _read_unlocked(workdir)
        if state is None:
            return
        active_id = state.get("active_revision_id")
        revision = next(
            (
                item for item in state.get("revisions", [])
                if item.get("id") == active_id
            ),
            None,
        )
        if revision is None:
            return
        # A normal conversational follow-up reuses the same run, but it must
        # not reopen the accepted delivery as a mutable revision.  Artifact
        # changes first create a draft revision through change_confirmation;
        # until then the stable revision remains completed/verified.
        if (
            active_id == state.get("current_revision_id")
            and revision.get("status") in {"completed", "verified"}
        ):
            return
        revision["status"] = mapped
        revision["updated_at"] = _now()
        state["updated_at"] = revision["updated_at"]
        _atomic_write(_state_path(workdir), state)


def revision_change_confirmation_required(workdir: Path) -> bool:
    """Whether artifact mutation would overwrite the accepted delivery.

    The Agent still decides from the conversation whether a follow-up is a
    genuine revision.  This is only the persistence guard: once the active
    revision is also the completed/verified current revision, mutating tools
    must wait until ``ask_user(change_confirmation)`` creates a new draft.
    """
    with _LOCK:
        state = _read_unlocked(workdir)
        if state is None:
            return False
        active_id = state.get("active_revision_id")
        if not active_id or active_id != state.get("current_revision_id"):
            return False
        revision = next(
            (
                item for item in state.get("revisions", [])
                if item.get("id") == active_id
            ),
            None,
        )
        return bool(
            revision
            and revision.get("status") in {"completed", "verified"}
        )


def mark_active_revision_completed(workdir: Path, *, verified: bool) -> None:
    """Complete the active revision, promoting it only after verification."""
    with _LOCK:
        state = _read_unlocked(workdir)
        if state is None:
            return
        active_id = state.get("active_revision_id")
        revision = next(
            (
                item for item in state.get("revisions", [])
                if item.get("id") == active_id
            ),
            None,
        )
        if revision is None:
            return
        timestamp = _now()
        revision["status"] = "verified" if verified else "completed"
        revision["updated_at"] = timestamp
        revision["completed_at"] = timestamp
        if verified or revision.get("parent_revision_id") is None:
            state["current_revision_id"] = revision["id"]
        state["updated_at"] = timestamp
        _atomic_write(_state_path(workdir), state)


def _find_change_request(
    state: dict[str, Any],
    change_request_id: str,
) -> dict[str, Any]:
    request = next(
        (
            item for item in state.get("change_requests", [])
            if item.get("id") == change_request_id
        ),
        None,
    )
    if request is None:
        raise ProjectStateError("修改请求不存在或已经过期。")
    return request


def _snapshot_revision_unlocked(
    workdir: Path,
    state: dict[str, Any],
    revision_id: str,
) -> None:
    """Freeze the accepted artifact set before a new revision can mutate it."""
    revision = next(
        (
            item for item in state.get("revisions", [])
            if item.get("id") == revision_id
        ),
        None,
    )
    if revision is None:
        raise ProjectStateError("当前稳定版本不存在，无法建立安全快照。")
    snapshot_root = workdir / "revisions" / revision_id / "snapshot"
    manifest_path = snapshot_root.parent / "manifest.json"
    if manifest_path.is_file():
        revision["snapshot_path"] = snapshot_root.relative_to(workdir).as_posix()
        return

    snapshot_root.mkdir(parents=True, exist_ok=True)
    candidates: list[Path] = []
    for name in _SNAPSHOT_FILES:
        source = workdir / name
        if source.is_file():
            candidates.append(source)
    candidates.extend(
        path
        for path in sorted(workdir.glob("*.xlsx"))
        if path.is_file()
    )
    for directory in _SNAPSHOT_DIRECTORIES:
        source_root = workdir / directory
        if source_root.is_dir():
            candidates.extend(
                path for path in sorted(source_root.rglob("*"))
                if path.is_file()
            )

    artifacts: list[dict[str, Any]] = []
    try:
        for source in candidates:
            relative = source.relative_to(workdir)
            target = snapshot_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            artifacts.append({
                "path": relative.as_posix(),
                "size_bytes": target.stat().st_size,
                "sha256": digest,
            })
        manifest = {
            "schema_version": 1,
            "revision_id": revision_id,
            "created_at": _now(),
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
        }
        _atomic_write(manifest_path, manifest)
    except Exception as exc:
        shutil.rmtree(snapshot_root.parent, ignore_errors=True)
        raise ProjectStateError("旧版本快照失败，本次修改尚未开始。") from exc

    revision["snapshot_path"] = snapshot_root.relative_to(workdir).as_posix()
    revision["snapshot_artifact_count"] = len(artifacts)
    revision["updated_at"] = _now()
