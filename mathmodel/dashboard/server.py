"""Dashboard server (Python stdlib only).

Serves a ChatGPT-style single-page app that live-views AND launches agent runs.
Runs live under workspace/<id>/ with events.jsonl (detailed trace), plan.json
(todos), problem.md, results/, figures/, and meta.json (session name + task).

Endpoints:
  GET  /                      -> the SPA
  GET  /api/runs              -> list of sessions (id, name, status, created)
  GET  /api/run?id=           -> full detail for one run (events, plan, results, ...)
  DELETE /api/run?id=         -> permanently remove one non-running conversation
  GET  /api/file?id=&path=    -> raw file (figures / xlsx / pdf)
  GET  /api/provider-settings -> current browser-safe model provider settings
  POST /api/tasks             -> launch a new run {name, task, files:[{name,b64}]}
  POST /api/provider-settings -> update the provider used by subsequent turns

Launching runs the agent in a background thread in this process, writing
events.jsonl as it goes (the SPA polls it).

Run:  ./.venv/bin/python -m mathmodel.dashboard.server [--port 8765]
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

# tectonic / docker must be findable from agent runs launched in-process.
os.environ["PATH"] = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin:/Library/TeX/texbin"

from ..config import PROJECT_ROOT, load_config  # noqa: E402
from ..agent.prompts import strip_legacy_modeling_user_suffix  # noqa: E402
from ..contextlog import CONTEXT_LOG_FILENAME, ContextRecorder  # noqa: E402
from ..provider_settings import (  # noqa: E402
    provider_settings_payload,
    save_provider_settings,
)
from ..user_provider_settings import (  # noqa: E402
    runtime_override as user_provider_runtime_override,
    save_account_settings as save_user_provider_settings,
    settings_payload as user_provider_settings_payload,
)
from ..project_state import (  # noqa: E402
    ensure_project,
    mark_active_revision_completed,
    project_budget_settings,
    project_view,
    resolve_change_request,
    update_active_revision_status,
    update_project_budget_limit,
)
from ..sandbox.docker import (  # noqa: E402
    ContainerCleanupResult,
    cleanup_managed_containers,
)

WORKSPACE = PROJECT_ROOT / "workspace"
# Production assets are built by the Vite app in ``frontend/``.  Keeping the
# SPA and the API on one local origin makes it possible to use the dashboard
# without a second development server after ``pnpm build``.
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_INDEX = STATIC_DIR / "index.html"
_CFG = load_config()

# Long-running tools emit a durable heartbeat every 30 seconds. Five minutes
# without any event now means neither the agent nor its active tools are making
# observable progress, while a healthy multi-hour run_code call stays running.
RUN_STALE_SECONDS = int(os.environ.get("MATHMODEL_RUN_STALE_SECONDS", "300"))
_META_LOCK = threading.RLock()
# One cancellation flag per in-flight run. The mapping is also the worker's
# lease: once stop_run removes its exact event, that old worker may finish its
# provider call in the background, but it may no longer emit events, persist
# conversation state, or overwrite the status of a newer continuation.
_STOP_LOCK = threading.RLock()
_STOP_EVENTS: dict[str, threading.Event] = {}
_VERIFICATION_ATTEMPTS_MIN = 1
_VERIFICATION_ATTEMPTS_MAX = 10
_MAIN_AGENT_STEPS_DEFAULT = 200
_MAIN_AGENT_STEPS_MIN = 10
_MAIN_AGENT_STEPS_MAX = 300
_MAIN_AGENT_STEPS_EXTENSION = 40
_SUBAGENT_STEPS_DEFAULT = 60
_SUBAGENT_STEPS_MIN = 5
_SUBAGENT_STEPS_MAX = 300
_VERIFIER_STEPS_MIN = 4
_VERIFIER_STEPS_MAX = 512
_PROVIDER_SETTINGS_LOCK = threading.RLock()
_REQUEST_SCOPE = threading.local()
_AUTH_PROFILE_URL = os.environ.get("MATHMODEL_AUTH_PROFILE_URL", "").strip()
_PUBLIC_DEPLOYMENT = os.environ.get("MATHMODEL_PUBLIC_DEPLOYMENT", "").lower() in {
    "1", "true", "yes", "on",
}


class RunStateError(ValueError):
    """Raised when a requested conversation transition is not allowed."""


class AuthenticationError(ValueError):
    """Raised when a public dashboard request has no valid account token."""


# --------------------------------------------------------------------------- io
def _read(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


def _workspace() -> Path:
    """Return the conversation root visible to the current request.

    The upstream project is intentionally local-first. Public deployments opt
    into account-backed request scoping, which keeps every user's run list and
    artifacts in a separate directory while preserving the original local
    single-user layout by default.
    """
    if not _AUTH_PROFILE_URL:
        return WORKSPACE
    user_id = str(getattr(_REQUEST_SCOPE, "user_id", "")).strip()
    if not user_id:
        raise AuthenticationError("authentication required")
    safe_user_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", user_id).strip("._")
    if not safe_user_id:
        raise AuthenticationError("invalid authenticated user")
    return WORKSPACE / ".users" / safe_user_id


def _run_dir(run_id: str) -> Path:
    workspace = _workspace().resolve()
    d = (workspace / run_id).resolve()
    if not run_id or d == workspace or workspace not in d.parents:
        raise ValueError("bad run id")
    return d


def _provider_settings_payload() -> dict:
    if _PUBLIC_DEPLOYMENT and _AUTH_PROFILE_URL:
        return user_provider_settings_payload(_CFG, _workspace())
    return provider_settings_payload(_CFG)


def _meta(d: Path) -> dict:
    p = d / "meta.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _write_meta(d: Path, meta: dict) -> None:
    """Atomically persist run metadata read by the polling dashboard."""
    with _META_LOCK:
        target = d / "meta.json"
        temporary = d / ".meta.json.tmp"
        temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        os.replace(temporary, target)


def _default_verification_attempts() -> int:
    configured = int(_CFG.get("verification", {}).get("max_attempts", 3))
    return max(
        _VERIFICATION_ATTEMPTS_MIN,
        min(_VERIFICATION_ATTEMPTS_MAX, configured),
    )


def _verification_settings(d: Path) -> dict:
    meta = _meta(d)
    default = _default_verification_attempts()
    raw = meta.get("verification_max_attempts")
    is_custom = raw is not None
    try:
        value = int(raw) if is_custom else default
    except (TypeError, ValueError):
        value = default
        is_custom = False
    value = max(_VERIFICATION_ATTEMPTS_MIN, min(_VERIFICATION_ATTEMPTS_MAX, value))
    return {
        "max_attempts": value,
        "default_max_attempts": default,
        "min_attempts": _VERIFICATION_ATTEMPTS_MIN,
        "max_allowed_attempts": _VERIFICATION_ATTEMPTS_MAX,
        "is_custom": is_custom,
    }


def _agent_settings(d: Path) -> dict:
    meta = _meta(d)
    raw = meta.get("main_agent_max_steps")
    is_custom = raw is not None
    try:
        value = int(raw) if is_custom else _MAIN_AGENT_STEPS_DEFAULT
    except (TypeError, ValueError):
        value = _MAIN_AGENT_STEPS_DEFAULT
        is_custom = False
    value = max(_MAIN_AGENT_STEPS_MIN, min(_MAIN_AGENT_STEPS_MAX, value))
    return {
        "max_steps": value,
        "default_max_steps": _MAIN_AGENT_STEPS_DEFAULT,
        "min_steps": _MAIN_AGENT_STEPS_MIN,
        "max_allowed_steps": _MAIN_AGENT_STEPS_MAX,
        "is_custom": is_custom,
    }


def _default_verifier_steps() -> int:
    configured = int(_CFG.get("verification", {}).get("max_steps", 80))
    return max(_VERIFIER_STEPS_MIN, min(_VERIFIER_STEPS_MAX, configured))


def _verifier_settings(d: Path) -> dict:
    meta = _meta(d)
    default = _default_verifier_steps()
    raw = meta.get("verification_agent_max_steps")
    is_custom = raw is not None
    try:
        value = int(raw) if is_custom else default
    except (TypeError, ValueError):
        value = default
        is_custom = False
    value = max(_VERIFIER_STEPS_MIN, min(_VERIFIER_STEPS_MAX, value))
    return {
        "max_steps": value,
        "default_max_steps": default,
        "min_steps": _VERIFIER_STEPS_MIN,
        "max_allowed_steps": _VERIFIER_STEPS_MAX,
        "is_custom": is_custom,
    }


def _subagent_settings(d: Path) -> dict:
    meta = _meta(d)
    raw = meta.get("subagent_max_steps")
    is_custom = raw is not None
    try:
        value = int(raw) if is_custom else _SUBAGENT_STEPS_DEFAULT
    except (TypeError, ValueError):
        value = _SUBAGENT_STEPS_DEFAULT
        is_custom = False
    value = max(_SUBAGENT_STEPS_MIN, min(_SUBAGENT_STEPS_MAX, value))
    return {
        "max_steps": value,
        "default_max_steps": _SUBAGENT_STEPS_DEFAULT,
        "min_steps": _SUBAGENT_STEPS_MIN,
        "max_allowed_steps": _SUBAGENT_STEPS_MAX,
        "is_custom": is_custom,
    }


def _last_activity(d: Path, meta: dict | None = None) -> float:
    """Return the last durable sign of progress for a run."""
    meta = meta or _meta(d)
    candidates = [float(meta.get("created", 0) or 0), float(meta.get("last_activity", 0) or 0)]
    events = d / "events.jsonl"
    if events.exists():
        candidates.append(events.stat().st_mtime)
    return max(candidates)


def _touch_run(workdir: Path) -> None:
    """Record a heartbeat whenever the agent emits an event."""
    with _META_LOCK:
        meta = _meta(workdir)
        if meta.get("status") != "running":
            return
        meta["last_activity"] = time.time()
        _write_meta(workdir, meta)


def _run_status(d: Path) -> str:
    """The authoritative status is meta.json's "status" field -- worker() (see
    _start_agent_thread) sets it precisely and synchronously at every real
    transition (running -> done/cancelled/stopped/error), using Agent.
    last_stop_reason rather than guessing from events.jsonl. (An earlier
    version inferred status by scanning events.jsonl's tail for a terminal
    marker; that broke as soon as a conversation could be continued, because a
    stale marker from a PRIOR turn -- or simply the gap before a new turn's
    thread has written anything yet -- would be misread as the current
    status. Trusting meta.json avoids that whole class of bug.)
    """
    if (d / "pending_question.json").exists():
        # Waiting on a human is not staleness -- never let the idle-timeout
        # below mark a paused-for-input run as an error.
        return "waiting_input"
    ev = d / "events.jsonl"
    m = _meta(d)
    status = m.get("status", "running" if ev.exists() else "unknown")
    if status == "running":
        idle_seconds = time.time() - _last_activity(d, m)
        if idle_seconds > RUN_STALE_SECONDS:
            # An in-process worker lease is stronger evidence than event-file
            # freshness. Long model requests now emit provider heartbeats too,
            # but this guard also closes the race where status polling happens
            # just before the next heartbeat is durably appended.
            stale_without_worker = False
            with _STOP_LOCK:
                if d.name in _STOP_EVENTS:
                    return "running"
                # Re-read under the lifecycle lock in case a worker started or
                # emitted an event between the first check and lock acquisition.
                current = _meta(d)
                if current.get("status") != "running":
                    return str(current.get("status") or "unknown")
                if time.time() - _last_activity(d, current) <= RUN_STALE_SECONDS:
                    return "running"
                reason = (
                    f"超过 {RUN_STALE_SECONDS // 60} 分钟没有新的运行记录，"
                    "且没有活动的运行线程，已自动标记为异常。"
                    "请重新开始本次会话。"
                )
                _set_status(d, "error", failure_reason=reason)
                stale_without_worker = True
            if stale_without_worker:
                cleanup_managed_containers(d)
                return "error"
    return status


def _last_lead_stop_reason(d: Path) -> str | None:
    """Recover the latest lead-run boundary for conversations created before
    ``meta.stop_reason`` was persisted. Sub-agent max-step events must never
    make the main conversation eligible for one-click continuation.
    """
    events_path = d / "events.jsonl"
    if not events_path.is_file():
        return None
    for line in reversed(events_path.read_text(errors="replace").splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("subagent") is not None:
            continue
        kind = str(event.get("kind") or "")
        if kind in {"max_steps", "cancelled", "done", "task"}:
            return kind
    return None


def list_runs() -> list[dict]:
    workspace = _workspace()
    if not workspace.is_dir():
        return []
    runs = []
    for d in workspace.iterdir():
        if not d.is_dir():
            continue
        status = _run_status(d)
        m = _meta(d)
        runs.append({
            "id": d.name,
            "name": _resolved_run_name(d, m),
            "task": m.get("task", ""),
            "created": m.get("created", d.stat().st_mtime),
            "status": status,
            "has_pdf": (d / "paper" / "main.pdf").exists(),
            "failure_reason": m.get("failure_reason"),
            "last_activity": _last_activity(d, m),
        })
    runs.sort(key=lambda r: r["created"], reverse=True)
    return runs


def _enrich_legacy_assistant_reasoning(d: Path, events: list[dict]) -> None:
    """Backfill thinking-mode progress text for events written by older builds."""
    state_path = d / "session_state.json"
    if not state_path.is_file():
        return
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return

    assistant_messages = [
        message for message in state.get("messages") or []
        if message.get("role") == "assistant"
    ]
    cursor = 0
    for event in events:
        if event.get("kind") != "assistant":
            continue
        expected_tools = [
            str(call[0]) for call in event.get("tool_calls") or []
            if isinstance(call, (list, tuple)) and call
        ]
        while cursor < len(assistant_messages):
            message = assistant_messages[cursor]
            cursor += 1
            actual_tools = [
                str((call.get("function") or {}).get("name", ""))
                for call in message.get("tool_calls") or []
            ]
            if actual_tools != expected_tools:
                continue
            if (
                not str(event.get("text") or "").strip()
                and not str(event.get("reasoning_text") or "").strip()
            ):
                reasoning = message.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning.strip():
                    event["reasoning_text"] = reasoning
            break


_USAGE_INTEGER_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_input_tokens",
    "uncached_input_tokens",
    "unclassified_input_tokens",
    "priced_tokens",
    "unpriced_tokens",
)


def _empty_usage_summary() -> dict:
    return {
        **{field: 0 for field in _USAGE_INTEGER_FIELDS},
        "estimated_cost_cny": 0.0,
    }


def _add_usage_summary(
    total: dict,
    payload: dict | None,
    *,
    legacy_total_tokens: int = 0,
    legacy_prompt_tokens: int = 0,
) -> None:
    """Add one independently billed Agent stream without inventing cache data."""
    if not isinstance(payload, dict):
        total["total_tokens"] += max(0, int(legacy_total_tokens or 0))
        total["prompt_tokens"] += max(0, int(legacy_prompt_tokens or 0))
        total["unclassified_input_tokens"] += max(
            0, int(legacy_prompt_tokens or 0)
        )
        total["unpriced_tokens"] += max(0, int(legacy_total_tokens or 0))
        return

    has_cache_fields = any(
        key in payload
        for key in (
            "cached_input_tokens",
            "uncached_input_tokens",
            "unclassified_input_tokens",
        )
    )
    has_pricing_fields = any(
        key in payload
        for key in ("priced_tokens", "unpriced_tokens", "estimated_cost_cny")
    )
    prompt_tokens = max(0, int(payload.get("prompt_tokens", 0) or 0))
    total_tokens = max(
        0,
        int(payload.get("total_tokens", legacy_total_tokens) or 0),
    )
    for field in _USAGE_INTEGER_FIELDS:
        if field == "unclassified_input_tokens" and not has_cache_fields:
            value = prompt_tokens
        elif field == "unpriced_tokens" and not has_pricing_fields:
            value = total_tokens
        else:
            value = payload.get(field, 0)
        total[field] += max(0, int(value or 0))
    total["estimated_cost_cny"] += max(
        0.0, float(payload.get("estimated_cost_cny", 0) or 0)
    )


def _conversation_usage(
    d: Path,
    events: list[dict],
    verification_usage: dict[int, dict],
) -> dict:
    """Combine lead, delegated, and verifier usage into one conversation bill."""
    total = _empty_usage_summary()
    state_path = d / "session_state.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    lead_usage = state.get("total_usage")
    _add_usage_summary(total, lead_usage)
    external_models: dict[tuple[str, str, str], dict] = {}

    for event in events:
        if event.get("kind") == "external_model_usage":
            key = (
                str(event.get("tool") or "external_model"),
                str(event.get("provider") or "unknown"),
                str(event.get("model") or "unknown"),
            )
            entry = external_models.setdefault(
                key,
                {
                    "tool": key[0],
                    "provider": key[1],
                    "model": key[2],
                    **_empty_usage_summary(),
                },
            )
            # This is a breakdown only. The same usage is already persisted in
            # the lead Agent state and therefore must not be added twice.
            _add_usage_summary(entry, event.get("usage"))
            continue
        if event.get("kind") == "routing_usage":
            _add_usage_summary(total, event.get("usage"))
            continue
        if event.get("kind") != "subagent_end":
            continue
        _add_usage_summary(
            total,
            event.get("usage"),
            legacy_total_tokens=int(event.get("tokens", 0) or 0),
        )

    for usage in verification_usage.values():
        _add_usage_summary(
            total,
            usage.get("usage") if isinstance(usage, dict) else None,
            legacy_total_tokens=(
                int(usage.get("reported_total_tokens", 0) or 0)
                if isinstance(usage, dict) else 0
            ),
        )

    total["estimated_cost_cny"] = round(total["estimated_cost_cny"], 6)
    total["cache_breakdown_complete"] = (
        total["unclassified_input_tokens"] == 0
    )
    total["pricing_complete"] = total["unpriced_tokens"] == 0
    total["currency"] = "CNY"
    pricing = _CFG.get("pricing", {})
    rates_by_model = pricing.get("deepseek_cny_per_million", {})
    active_rates = (
        rates_by_model.get(str(_CFG.get("model", "")).lower(), {})
        if isinstance(rates_by_model, dict) else {}
    )
    total["rates_per_million"] = copy.deepcopy(active_rates)
    total["pricing_policy"] = {
        "rates_by_model": copy.deepcopy(rates_by_model),
        "peak": copy.deepcopy(pricing.get("deepseek_peak", {})),
    }
    total["external_model_usage"] = [
        {
            **entry,
            "estimated_cost_cny": round(entry["estimated_cost_cny"], 6),
            "cache_breakdown_complete": entry["unclassified_input_tokens"] == 0,
            "pricing_complete": entry["unpriced_tokens"] == 0,
        }
        for entry in external_models.values()
    ]
    return total


def run_detail(run_id: str) -> dict:
    d = _run_dir(run_id)
    if not d.is_dir():
        raise ValueError("run not found")
    events = []
    ev = d / "events.jsonl"
    if ev.exists():
        for line in ev.read_text(errors="replace").splitlines():
            line = line.strip()
            if line:
                try:
                    event = json.loads(line)
                    if event.get("kind") == "task" and isinstance(event.get("task"), str):
                        event["task"] = strip_legacy_modeling_user_suffix(
                            event["task"]
                        )
                    events.append(event)
                except json.JSONDecodeError:
                    pass
    _enrich_legacy_assistant_reasoning(d, events)
    # Older event streams did not include the final verifier usage even though
    # it was persisted in each verification report. Enrich both historical and
    # current runs at read time so the dashboard can show per-round totals.
    verification_usage: dict[int, dict] = {}
    historical_triage_tokens: dict[int, int] = {}
    for event in events:
        if (
            event.get("kind") == "verification_progress"
            and event.get("role") == "lead-triage"
            and isinstance(event.get("total_tokens"), int)
        ):
            try:
                attempt = int(event.get("attempt", 0))
            except (TypeError, ValueError):
                continue
            historical_triage_tokens[attempt] = max(
                historical_triage_tokens.get(attempt, 0),
                event["total_tokens"],
            )
    verification_dir = d / "verification"
    if verification_dir.is_dir():
        for report_path in verification_dir.glob("report_attempt_*.json"):
            try:
                report_payload = json.loads(report_path.read_text())
                attempt = int(report_payload.get("attempt", 0))
                usage = report_payload.get("verification_usage")
                if attempt > 0 and isinstance(usage, dict):
                    usage = dict(usage)
                    if "triage_tokens" not in usage:
                        triage_tokens = historical_triage_tokens.get(attempt, 0)
                        usage["triage_tokens"] = triage_tokens
                        usage["reported_total_tokens"] = (
                            int(usage.get("reported_total_tokens", 0))
                            + triage_tokens
                        )
                    verification_usage[attempt] = usage
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    for event in events:
        if (
            event.get("kind") in {"verification_result", "verification_failed"}
            and not isinstance(event.get("verification_usage"), dict)
        ):
            try:
                attempt = int(event.get("attempt", 0))
            except (TypeError, ValueError):
                continue
            if attempt in verification_usage:
                event["verification_usage"] = verification_usage[attempt]
    results = {}
    rdir = d / "results"
    if rdir.is_dir():
        for p in sorted(rdir.glob("*")):
            if p.is_file():
                results[p.name] = _read(p)[:20000]
    plan_tasks = []
    pj = d / "plan.json"
    if pj.exists():
        try:
            plan_tasks = json.loads(pj.read_text()).get("tasks", [])
        except (json.JSONDecodeError, AttributeError):
            plan_tasks = []
    status = _run_status(d)
    m = _meta(d)
    project_was_bootstrapped = not (d / "project.json").is_file()
    project = project_view(
        d,
        title=_resolved_run_name(d, m),
        created_at=m.get("created"),
    )
    if project_was_bootstrapped:
        if status == "done":
            verified = False
            try:
                verified = json.loads(
                    (d / "verification" / "report.json").read_text()
                ).get("verdict") == "PASS"
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
            mark_active_revision_completed(d, verified=verified)
        else:
            update_active_revision_status(d, status)
        project = project_view(d, title=_resolved_run_name(d, m))
    elif status == "done":
        # Older builds reopened the accepted current revision as `running` on
        # every conversational follow-up.  If that revision had already
        # completed, repair the display/state on read without inventing a new
        # historical revision or snapshot.
        active_revision = project.get("active_revision") or {}
        if (
            active_revision.get("id") == project.get("current_revision_id")
            and active_revision.get("completed_at") is not None
            and active_revision.get("status") in {"running", "waiting_input"}
        ):
            verified = False
            try:
                verified = json.loads(
                    (d / "verification" / "report.json").read_text()
                ).get("verdict") == "PASS"
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
            mark_active_revision_completed(d, verified=verified)
            project = project_view(d, title=_resolved_run_name(d, m))
    project["deliverable_revisions"] = _revision_deliverables(d, project)
    pending_question = None
    pq = d / "pending_question.json"
    if pq.exists():
        try:
            pending_question = json.loads(pq.read_text())
        except json.JSONDecodeError:
            pending_question = None
    paper = _paper_delivery(d)
    usage = _conversation_usage(d, events, verification_usage)
    return {
        "id": run_id,
        "name": _resolved_run_name(d, m),
        "task": m.get("task", ""),
        "files": [
            str(name)
            for name in (m.get("files") or [])
            if isinstance(name, str)
        ],
        "created": m.get("created"),
        "status": status,
        "stop_reason": m.get("stop_reason") or (
            _last_lead_stop_reason(d) if status == "stopped" else None
        ),
        "plan": _read(d / "plan.md"),
        "plan_tasks": plan_tasks,
        "problem": _read(d / "problem.md"),
        "decisions": _read(d / "decisions.md"),
        "results": results,
        "figures": [p.name for p in sorted((d / "figures").glob("*"))] if (d / "figures").is_dir() else [],
        "source_files": _source_deliverables(d),
        "outputs": [p.name for p in sorted(d.glob("*.xlsx"))],
        "paper": paper,
        "events": events,
        "run_log": _read(d / "run.log")[-20000:],
        "has_pdf": (d / "paper" / "main.pdf").exists(),
        "failure_reason": m.get("failure_reason"),
        "last_activity": _last_activity(d, m),
        "retry_of": m.get("retry_of"),
        "pending_question": pending_question,
        "verification_settings": _verification_settings(d),
        "agent_settings": _agent_settings(d),
        "subagent_settings": _subagent_settings(d),
        "verifier_settings": _verifier_settings(d),
        "usage": usage,
        "project": project,
        "project_budget_settings": project_budget_settings(d),
    }


# ------------------------------------------------------------------- launching
def _safe_name(s: str) -> str:
    keep = "".join(c if (c.isalnum() or c in "-_") else "_" for c in s)
    return keep.strip("_")[:40]


_TITLE_LEAD_IN = re.compile(
    r"^(?:你好[，,。!！\s]*)?"
    r"(?:请(?:你)?|你|麻烦(?:你)?|我想(?:让你)?|能不能|可以)?"
    r"(?:帮我|帮助我|帮忙)?"
    r"(?:看一下|看看)?"
    r"[：:，,\s]*",
)
_TITLE_CREATION_VERB = re.compile(
    r"^(?:做|建立|构建|设计|开发|创建)(?:一个|一套|一份)?\s*",
)


def _generated_run_name(task: str, files: list[dict]) -> str:
    """Derive a compact ChatGPT-style topic title from the first user request."""
    lines = [
        re.sub(r"^\s*(?:#{1,6}|\d+[.、)])\s*", "", line).strip()
        for line in task.splitlines()
        if line.strip()
    ]
    title = lines[0] if lines else ""
    title = _TITLE_LEAD_IN.sub("", title).strip()
    title = _TITLE_CREATION_VERB.sub("", title).strip()
    title = re.split(r"[，,。！？!?；;]", title, maxsplit=1)[0].strip("：:，,。 ")

    generic = {"", "这个问题", "这个任务", "建模问题", "继续", "开始"}
    source_files = [
        item for item in files
        if Path(item.get("name", "")).suffix.lower()
        in {".pdf", ".doc", ".docx", ".txt", ".md"}
    ]
    if title in generic and files:
        title = Path((source_files or files)[0].get("name", "新建建模任务")).stem
    if not title and files:
        title = Path((source_files or files)[0].get("name", "新建建模任务")).stem
    title = title or "新建建模任务"
    limit = 24 if re.search(r"[\u3400-\u9fff]", title) else 48
    if len(title) > limit:
        title = title[:limit].rstrip(" ，,：:.-_") + "…"
    return title


_PROBLEM_TITLE_PATTERNS = (
    re.compile(
        r"(?mi)^\s*(?:problem\s+[A-Z]|[A-ZＡ-Ｚ]\s*题)\s*[:：]?\s*"
        r"([^\n]{2,80})\s*$"
    ),
    re.compile(r"(?m)^\s*#{1,6}\s+([^\n]{2,80})\s*$"),
)
_PROBLEM_TITLE_BOILERPLATE = {
    "problem materials",
    "user-provided problem",
    "材料",
    "问题材料",
}


def _problem_run_name(problem: str) -> str:
    """Extract a strong topic title from normalized problem material."""
    for pattern in _PROBLEM_TITLE_PATTERNS:
        for match in pattern.finditer(problem):
            candidate = match.group(1).strip(" `#：:，,。")
            lowered = candidate.lower()
            if (
                lowered in _PROBLEM_TITLE_BOILERPLATE
                or lowered.startswith(("from `", "data from `"))
                or candidate.startswith(("来自 `", "数据来自 `"))
            ):
                continue
            return _generated_run_name(candidate, [])
    return ""


def _legacy_name_base(name: str) -> str:
    base = re.sub(r"(?:\s*·\s*重试)+\s*$", "", name).strip()
    return re.sub(r"\s*·\s*\d{1,2}:\d{2}\s*$", "", base).strip()


def _resolved_run_name(d: Path, meta: dict | None = None) -> str:
    """Return the content-derived title for automatic and legacy file runs."""
    meta = meta or _meta(d)
    current = str(meta.get("name") or d.name).strip()
    current_base = _legacy_name_base(current)
    candidate = _problem_run_name(_read(d / "problem.md"))
    if not candidate:
        return current_base or current

    upload_stems = {
        Path(str(name)).stem
        for name in meta.get("files", [])
        if name
    }
    source_stems = {
        Path(name).stem
        for name in re.findall(
            r"(?m)^##\s+(?:From|Data from)\s+`([^`]+)`",
            _read(d / "problem.md"),
        )
    }
    generic = {
        "", "新建会话", "新建建模任务", "这个问题", "这个任务",
        "建模问题", "继续", "开始",
    }
    legacy_retry = "· 重试" in current
    file_based = current_base in upload_stems or current_base in source_stems
    problem_code = bool(re.fullmatch(r"[A-ZＡ-Ｚ]\s*题", current_base))
    if (
        meta.get("name_auto_generated") is True
        or current_base in generic
        or legacy_retry
        or file_based
        or problem_code
    ):
        return candidate
    return current_base or current


def _refresh_generated_run_name(workdir: Path) -> None:
    """Persist a better title once uploaded problem material is available."""
    with _META_LOCK:
        meta = _meta(workdir)
        if meta.get("name_auto_generated") is not True:
            return
        candidate = _problem_run_name(_read(workdir / "problem.md"))
        if candidate:
            meta["name"] = candidate
            _write_meta(workdir, meta)


def _paper_title(tex_path: Path) -> str:
    if not tex_path.is_file():
        return "建模论文"
    match = re.search(r"\\title\{([^{}]+)\}", tex_path.read_text(errors="replace"))
    return match.group(1).strip() if match else "建模论文"


def _delivery_filename(title: str, suffix: str, generated_at: float) -> str:
    safe_title = _safe_name(title) or "建模论文"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(generated_at))
    return f"{safe_title}_{stamp}{suffix}"


def _paper_delivery(workdir: Path) -> dict[str, str]:
    paper_dir = workdir / "paper"
    pdf = paper_dir / "main.pdf"
    tex = paper_dir / "main.tex"
    if not pdf.is_file() and not tex.is_file():
        return {}

    generated_at = pdf.stat().st_mtime if pdf.is_file() else tex.stat().st_mtime
    title = _paper_title(tex)
    delivery_path = paper_dir / "delivery.json"
    if delivery_path.is_file():
        try:
            delivery = json.loads(delivery_path.read_text())
            title = str(delivery.get("title") or title)
            generated_at = float(delivery.get("generated_at") or generated_at)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    result: dict[str, str] = {}
    if pdf.is_file():
        result["pdf"] = "paper/main.pdf"
        result["pdf_name"] = _delivery_filename(title, ".pdf", generated_at)
    if tex.is_file():
        result["tex"] = "paper/main.tex"
        result["tex_name"] = _delivery_filename(title, ".tex", generated_at)
    return result


_SOURCE_DELIVERY_SUFFIXES = {
    ".c", ".cc", ".cpp", ".h", ".hpp", ".ipynb", ".jl", ".js",
    ".jsx", ".m", ".py", ".r", ".rs", ".sh", ".sql", ".ts", ".tsx",
}


def _source_deliverables(workdir: Path) -> list[dict[str, object]]:
    """List downloadable source files without embedding code in the run API."""
    candidates: set[Path] = set()
    source_dir = workdir / "src"
    if source_dir.is_dir():
        candidates.update(path for path in source_dir.rglob("*") if path.is_file())
    candidates.update(
        path for path in workdir.iterdir()
        if path.is_file() and path.suffix.lower() in _SOURCE_DELIVERY_SUFFIXES
    )
    delivered: list[dict[str, object]] = []
    for path in sorted(candidates):
        relative = path.relative_to(workdir)
        if (
            path.suffix.lower() not in _SOURCE_DELIVERY_SUFFIXES
            or any(part.startswith(".") or part == "__pycache__" for part in relative.parts)
        ):
            continue
        delivered.append({
            "path": relative.as_posix(),
            "name": relative.relative_to("src").as_posix()
            if relative.parts[0] == "src" else relative.as_posix(),
            "size": path.stat().st_size,
        })
    return delivered[:200]


def _revision_deliverables(
    workdir: Path,
    project: dict[str, object],
) -> list[dict[str, object]]:
    """Resolve paper and code downloads for each durable project revision."""
    current_id = project.get("current_revision_id")
    active_id = project.get("active_revision_id")
    deliveries: list[dict[str, object]] = []
    for revision in project.get("revisions", []):
        if not isinstance(revision, dict):
            continue
        revision_id = str(revision.get("id") or "")
        snapshot_path = revision.get("snapshot_path")
        artifact_root: Path | None = None
        prefix = ""
        if isinstance(snapshot_path, str) and snapshot_path:
            candidate = (workdir / snapshot_path).resolve()
            if str(candidate).startswith(str(workdir.resolve())) and candidate.is_dir():
                artifact_root = candidate
                prefix = f"{Path(snapshot_path).as_posix().rstrip('/')}/"
        elif revision_id == active_id or (
            revision_id == current_id and active_id == current_id
        ):
            artifact_root = workdir

        paper: dict[str, str] = {}
        source_files: list[dict[str, object]] = []
        if artifact_root is not None:
            paper = _paper_delivery(artifact_root)
            for key in ("pdf", "tex"):
                if key in paper:
                    paper[key] = prefix + paper[key]
            source_files = _source_deliverables(artifact_root)
            if prefix:
                for source_file in source_files:
                    source_file["path"] = prefix + str(source_file["path"])

        deliveries.append({
            "revision_id": revision_id,
            "number": int(revision.get("number") or 0),
            "title": str(revision.get("title") or ""),
            "summary": str(revision.get("summary") or ""),
            "status": str(revision.get("status") or ""),
            "is_current": revision_id == current_id,
            "is_active": revision_id == active_id,
            "paper": paper,
            "source_files": source_files,
        })
    return deliveries


def _new_run_dir(label: str) -> tuple[str, Path]:
    """Allocate a unique run directory without overwriting a fast double-click."""
    workspace = _workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    stem = time.strftime("%Y%m%d-%H%M%S") + "_" + _safe_name(label)
    run_id = stem
    n = 2
    while (workspace / run_id).exists():
        run_id = f"{stem}_{n}"
        n += 1
    return run_id, workspace / run_id


def _unsubmitted_draft() -> dict | None:
    """Return the newest empty conversation draft, if there is one.

    Runs may proceed in parallel. The only protected state is an unsent draft:
    a second browser tab or a quick double click must not create more blank
    conversations while that draft still needs a modeling request.
    """
    drafts = [run for run in list_runs() if run["status"] == "draft"]
    return drafts[0] if drafts else None


def _ensure_new_run_allowed() -> None:
    if _unsubmitted_draft():
        raise RunStateError("请先在当前的新会话中发送建模请求，或删除该空白会话。")


def create_draft() -> tuple[str, str]:
    """Create a visible, not-yet-running workspace entry for a new conversation."""
    with _META_LOCK:
        _ensure_new_run_allowed()
        display_name = "新建项目"
        run_id, workdir = _new_run_dir("draft")
        workdir.mkdir(parents=True)
        _write_meta(workdir, {
            "name": display_name, "task": "", "created": time.time(),
            "status": "draft", "files": [],
        })
        ensure_project(workdir, title=display_name)
    return run_id, display_name


def launch_task(
    name: str,
    task: str,
    files: list[dict],
    draft_id: str | None = None,
    retry_of: str | None = None,
    verification_max_attempts: int | None = None,
    main_agent_max_steps: int | None = None,
    subagent_max_steps: int | None = None,
    verification_agent_max_steps: int | None = None,
) -> tuple[str, str]:
    """Create a run workdir, save inputs, and run the unified Agent in a thread."""
    display_name = name.strip() or _generated_run_name(task, files)
    if draft_id:
        run_id = draft_id
        workdir = _run_dir(run_id)
        existing_meta = _meta(workdir)
        if not workdir.is_dir() or existing_meta.get("status") != "draft":
            raise ValueError("draft is unavailable")
        created = existing_meta.get("created", time.time())
    else:
        with _META_LOCK:
            run_id, workdir = _new_run_dir(display_name)
            workdir.mkdir(parents=True)
            created = time.time()

    uploads = workdir / "_uploads"
    uploads.mkdir(exist_ok=True)
    saved = []
    for f in files or []:
        fn = _safe_name(Path(f.get("name", "file")).stem) + Path(f.get("name", "")).suffix
        try:
            data = base64.b64decode(f["b64"].split(",")[-1])
        except Exception:
            continue
        (uploads / fn).write_bytes(data)
        saved.append(uploads / fn)

    _write_meta(workdir, {
        "name": display_name, "task": task, "created": created,
        "status": "running", "files": [p.name for p in saved],
        "mode": "conversation",
        "name_auto_generated": not bool(name.strip()),
        "last_activity": time.time(),
        **(
            {"verification_max_attempts": existing_meta["verification_max_attempts"]}
            if draft_id and "verification_max_attempts" in existing_meta else {}
        ),
        **(
            {"main_agent_max_steps": existing_meta["main_agent_max_steps"]}
            if draft_id and "main_agent_max_steps" in existing_meta else {}
        ),
        **(
            {"subagent_max_steps": existing_meta["subagent_max_steps"]}
            if draft_id and "subagent_max_steps" in existing_meta else {}
        ),
        **(
            {"verification_agent_max_steps": existing_meta["verification_agent_max_steps"]}
            if draft_id and "verification_agent_max_steps" in existing_meta else {}
        ),
        **(
            {"verification_max_attempts": verification_max_attempts}
            if verification_max_attempts is not None else {}
        ),
        **(
            {"main_agent_max_steps": main_agent_max_steps}
            if main_agent_max_steps is not None else {}
        ),
        **(
            {"subagent_max_steps": subagent_max_steps}
            if subagent_max_steps is not None else {}
        ),
        **(
            {"verification_agent_max_steps": verification_agent_max_steps}
            if verification_agent_max_steps is not None else {}
        ),
        **({"retry_of": retry_of} if retry_of else {}),
    })
    ensure_project(workdir, title=display_name, created_at=created)

    _start_agent_thread(run_id, workdir, task, saved, resume=False)
    return run_id, display_name


def _start_agent_thread(
    run_id: str,
    workdir: Path,
    task: str,
    saved: list[Path],
    *,
    resume: bool,
    max_steps_override: int | None = None,
    internal_instruction: bool = False,
    pending_answer: dict | None = None,
) -> None:
    """Register a stop_event and start the background thread that drives one
    agent.run() call (fresh or resumed) in `workdir`. Shared by launch_task
    (resume=False: always starts an empty conversation) and continue_task
    (resume=True: picks up from session_state.json -- see agent/build.py).
    """
    from ..agent.build import build_agent
    from ..runlog import JsonlLogger

    # Authentication is request-thread local. Resolve the account's encrypted
    # provider choice before starting the background worker, then keep it only
    # in this run's in-memory config copy.
    provider_override = (
        user_provider_runtime_override(_workspace())
        if _PUBLIC_DEPLOYMENT and _AUTH_PROFILE_URL else {}
    )

    # Register before the thread starts so a stop request arriving in the gap
    # between launch and worker startup can still invalidate this generation.
    # For a continuation, validate the status again while holding the same lock
    # used by stop_run: two rapid "continue" requests cannot both win.
    stop_event = threading.Event()
    with _STOP_LOCK:
        if run_id in _STOP_EVENTS:
            raise RunStateError("这次会话已经在继续处理中，请先停止当前会话。")
        resume_statuses = (
            _CONTINUABLE_STATUSES | {"waiting_input"}
            if pending_answer is not None
            else _CONTINUABLE_STATUSES
        )
        if resume and _run_status(workdir) not in resume_statuses:
            raise RunStateError("只有已完成、已停止、已中断或异常的会话才能继续。")
        _STOP_EVENTS[run_id] = stop_event
        _set_status(
            workdir,
            "running",
            sync_project=pending_answer is None,
        )

    def is_current_worker() -> bool:
        return _STOP_EVENTS.get(run_id) is stop_event

    def finish_if_current(
        status: str,
        failure_reason: str | None = None,
        *,
        stop_reason: str | None = None,
    ) -> None:
        """Publish a terminal state and retire all work owned by this generation."""
        with _STOP_LOCK:
            if not is_current_worker():
                return
            # All main/sub-agent sandboxes for this generation share this
            # cancellation event. Set it before dropping the worker lease so
            # an abnormal lead-agent exit cannot leave a run_code poller alive.
            stop_event.set()
            _set_status(
                workdir,
                status,
                failure_reason=failure_reason,
                stop_reason=stop_reason,
            )
            should_complete_revision = (
                not resume
                or (internal_instruction and pending_answer is None)
                or (
                    pending_answer is not None
                    and (pending_answer.get("result") or {}).get("change_action")
                    == "confirm"
                )
            )
            if status == "done" and should_complete_revision:
                verified = False
                report_path = workdir / "verification" / "report.json"
                try:
                    verified = (
                        json.loads(report_path.read_text()).get("verdict") == "PASS"
                    )
                except (OSError, json.JSONDecodeError, AttributeError):
                    pass
                mark_active_revision_completed(workdir, verified=verified)
            _STOP_EVENTS.pop(run_id, None)
        # The stop event normally lets each DockerSandbox remove itself within
        # one poll interval. This labeled sweep is the deterministic fallback
        # for a sandbox thread that also failed before reaching its own cleanup.
        cleanup_managed_containers(workdir)

    def worker():
        try:
            if stop_event.is_set():
                return
            # Resuming an existing conversation must append to its event
            # history, not truncate it (a fresh launch always starts clean).
            logger = JsonlLogger(workdir / "events.jsonl", append=resume)

            def on_event(kind: str, data: dict) -> None:
                # Keep the lease check and append atomic with stop_run. Once
                # stopped, a late provider response from this worker is silent.
                with _STOP_LOCK:
                    if not is_current_worker():
                        return
                    logger(kind, data)
                    _touch_run(workdir)

            # Context payloads are intentionally stored outside events.jsonl so
            # the modeling dashboard never receives them. A separate local
            # Context Inspector process reads this append-only log.
            runtime_cfg = copy.deepcopy(_CFG)
            runtime_cfg.update(provider_override)
            runtime_cfg["_context_request_observer"] = ContextRecorder(
                workdir / CONTEXT_LOG_FILENAME,
                run_id,
            )
            meta = _meta(workdir)
            if stop_event.is_set():
                return

            prior_mode = str(meta.get("mode") or "")
            modeling_was_active = (
                prior_mode == "modeling" or (workdir / "problem.md").is_file()
            )
            if modeling_was_active and prior_mode != "modeling":
                with _META_LOCK:
                    current = _meta(workdir)
                    current["mode"] = "modeling"
                    _write_meta(workdir, current)

            def on_ingested(report) -> None:
                # The Agent's tool call, rather than a separate classifier, is
                # the durable transition into the modeling lifecycle.
                with _META_LOCK:
                    current = _meta(workdir)
                    current["mode"] = "modeling"
                    _write_meta(workdir, current)
                _refresh_generated_run_name(workdir)

            run_cfg = runtime_cfg
            run_cfg.setdefault("verification", {})["max_steps"] = (
                _verifier_settings(workdir)["max_steps"]
            )
            agent = build_agent(
                run_cfg,
                workdir,
                max_steps=(
                    max_steps_override
                    if max_steps_override is not None
                    else _agent_settings(workdir)["max_steps"]
                ),
                sub_max_steps=_subagent_settings(workdir)["max_steps"],
                on_event=on_event,
                stop_event=stop_event,
                resume=resume,
                state_lock=_STOP_LOCK,
                state_write_allowed=is_current_worker,
                verification_attempt_limit=(
                    lambda: _verification_settings(workdir)["max_attempts"]
                ),
                pending_problem_text=task,
                pending_upload_paths=tuple(saved),
                on_ingested=on_ingested,
            )
            if pending_answer is not None:
                result = dict(pending_answer.get("result") or {})
                change_request_id = pending_answer.get("change_request_id")
                if change_request_id:
                    resolved = resolve_change_request(
                        workdir,
                        str(change_request_id),
                        action=str(result.get("change_action") or "adjust"),
                        answer=str(result.get("answer") or ""),
                        selected_option_id=result.get("option_id"),
                        usage_baseline_cny=agent.total_usage.estimated_cost_cny,
                    )
                    result["revision_id"] = resolved.get("revision_id")
                    if result["revision_id"]:
                        update_active_revision_status(workdir, "running")
                observation = "[ask_user_result] " + json.dumps(
                    result,
                    ensure_ascii=False,
                )
                agent.resume_tool_result(
                    str(pending_answer["tool_call_id"]),
                    observation,
                )
                on_event(
                    "ask_resolved",
                    {
                        "id": pending_answer["id"],
                        "answered": True,
                        "selected_option_id": result.get("option_id"),
                        "change_action": result.get("change_action"),
                        "revision_id": result.get("revision_id"),
                    },
                )
                from ..tools.ask_user import complete_claimed_answer
                complete_claimed_answer(workdir)
            # Keep dashboard/runtime instructions in the Agent's system prompt.
            # The persisted user message remains exactly what the user typed.
            full = None if pending_answer is not None else task
            attachment_instruction = None
            if saved:
                attachment_names = ", ".join(path.name for path in saved)
                attachment_instruction = (
                    "[Current-turn attachment metadata — this is not user-authored "
                    "text]\n"
                    f"The user attached: {attachment_names}. The raw uploads are "
                    "held by the backend. If the request requires inspecting or "
                    "using them, call ingest_problem; do not claim to have read "
                    "them before that tool succeeds."
                )
            agent.run(
                full,
                verify_on_completion=(
                    modeling_was_active and internal_instruction
                ),
                internal_instruction=internal_instruction,
                turn_system_instruction=attachment_instruction,
            )
            # Precise, not inferred: max_steps must show as "stopped", not "done".
            terminal_status = {
                "done": "done", "cancelled": "cancelled", "max_steps": "stopped",
                "verification_failed": "stopped",
                "budget_exhausted": "stopped",
                "waiting_input": "waiting_input",
            }.get(agent.last_stop_reason, "done")
            finish_if_current(
                terminal_status,
                stop_reason=agent.last_stop_reason,
            )
        except Exception as exc:
            with _STOP_LOCK:
                owns_run = is_current_worker()
                if owns_run:
                    (workdir / "error.log").write_text(traceback.format_exc())
            if owns_run:
                finish_if_current(
                    "error",
                    failure_reason=f"运行异常：{type(exc).__name__}: {exc}",
                )
        finally:
            with _STOP_LOCK:
                if is_current_worker():
                    _STOP_EVENTS.pop(run_id, None)

    try:
        threading.Thread(target=worker, daemon=True).start()
    except Exception:
        with _STOP_LOCK:
            if is_current_worker():
                _STOP_EVENTS.pop(run_id, None)
                _set_status(workdir, "error", failure_reason="会话线程启动失败。")
        raise


# Statuses from which a conversation can be continued with a follow-up message
# rather than restarted from the original prompt (see continue_task).
_CONTINUABLE_STATUSES = {"done", "error", "stopped", "cancelled"}


def continue_task(run_id: str, task: str, files: list[dict]) -> tuple[str, str]:
    """Send a follow-up message to an already-finished conversation.

    Unlike retry_task (which restarts the original prompt in a brand-new run),
    this resumes the SAME workdir's persisted conversation state (see
    agent/build.py's session_state.json) and appends the new message to it --
    the agent sees the full prior history, not just the new task text.
    """
    from ..agent.build import session_state_path

    workdir = _run_dir(run_id)
    status = _run_status(workdir)
    if status not in _CONTINUABLE_STATUSES:
        raise RunStateError("只有已完成、已停止、已中断或异常的会话才能继续。")
    if not session_state_path(workdir).is_file():
        raise RunStateError("这次会话还没有可续接的历史记录（可能是较早版本产生的会话，缺少续接所需的状态文件）。")
    if not task.strip() and not files:
        raise RunStateError("请输入要发送的内容。")

    meta = _meta(workdir)
    uploads = workdir / "_uploads"
    uploads.mkdir(exist_ok=True)
    saved = []
    for f in files or []:
        fn = _safe_name(Path(f.get("name", "file")).stem) + Path(f.get("name", "")).suffix
        target = uploads / fn
        if target.exists():
            # Don't clobber a same-named upload from an earlier turn.
            target = uploads / f"{Path(fn).stem}_{int(time.time())}{Path(fn).suffix}"
        try:
            data = base64.b64decode(f["b64"].split(",")[-1])
        except Exception:
            continue
        target.write_bytes(data)
        saved.append(target)

    _start_agent_thread(run_id, workdir, task, saved, resume=True)
    return run_id, meta.get("name") or run_id


def continue_after_max_steps(run_id: str) -> dict:
    """Extend a max-step-stopped modeling run by exactly 40 steps and resume it.

    The persisted conversation limit grows by 40 so Settings reflects the new
    ceiling. This continuation itself receives only those 40 additional steps,
    rather than accidentally starting a fresh full-size step budget.
    """
    from ..agent.build import session_state_path

    workdir = _run_dir(run_id)
    if not workdir.is_dir():
        raise ValueError("run not found")

    with _STOP_LOCK:
        if _run_status(workdir) != "stopped":
            raise RunStateError("只有达到最大步数而暂停的会话才能直接继续。")
        meta = _meta(workdir)
        stop_reason = meta.get("stop_reason") or _last_lead_stop_reason(workdir)
        if stop_reason != "max_steps":
            raise RunStateError("这次会话并非因达到最大步数而暂停，请在输入框中继续。")
        if str(meta.get("mode") or "modeling") != "modeling":
            raise RunStateError("只有建模会话支持增加主 Agent 步数后直接继续。")
        if not session_state_path(workdir).is_file():
            raise RunStateError("这次会话缺少可续接的历史状态。")

        current = _agent_settings(workdir)["max_steps"]
        extended = current + _MAIN_AGENT_STEPS_EXTENSION
        if extended > _MAIN_AGENT_STEPS_MAX:
            raise RunStateError(
                f"主 Agent 最大步数不能超过 {_MAIN_AGENT_STEPS_MAX}；"
                "请先在设置中调整当前配置。"
            )

        had_custom_value = "main_agent_max_steps" in meta
        prior_custom_value = meta.get("main_agent_max_steps")
        with _META_LOCK:
            meta = _meta(workdir)
            meta["main_agent_max_steps"] = extended
            _write_meta(workdir, meta)

        instruction = (
            "[Orchestrator continuation — this is not user input]\n"
            "The previous run reached its main-Agent step limit before the "
            f"task was complete. Continue from the persisted state with the {_MAIN_AGENT_STEPS_EXTENSION} "
            "newly granted steps. Do not restart completed work; finish the "
            "remaining plan, verification, and delivery work."
        )
        try:
            _start_agent_thread(
                run_id,
                workdir,
                instruction,
                [],
                resume=True,
                max_steps_override=_MAIN_AGENT_STEPS_EXTENSION,
                internal_instruction=True,
            )
        except Exception:
            # Keep the setting change and the new worker generation atomic.
            with _META_LOCK:
                rollback = _meta(workdir)
                if had_custom_value:
                    rollback["main_agent_max_steps"] = prior_custom_value
                else:
                    rollback.pop("main_agent_max_steps", None)
                _write_meta(workdir, rollback)
            raise

    return {
        "id": run_id,
        "name": meta.get("name") or run_id,
        "added_steps": _MAIN_AGENT_STEPS_EXTENSION,
        "agent_settings": _agent_settings(workdir),
    }


def stop_run(run_id: str) -> None:
    """Immediately invalidate the current generation and make it continuable.

    Provider SDK calls are synchronous and cannot always be forcefully aborted.
    The underlying call may therefore return later, but removing its lease here
    guarantees that the late response is discarded instead of reaching the
    event log, session state, or run status. A new continuation can start as
    soon as this function returns.
    """
    d = _run_dir(run_id)
    missing_worker_reason: str | None = None
    with _STOP_LOCK:
        status = _run_status(d)
        if status not in {"running", "waiting_input"}:
            raise RunStateError("会话当前不是运行状态，无法停止。")
        ev = _STOP_EVENTS.get(run_id)
        if ev is None and status == "waiting_input":
            # Durable questions intentionally have no live worker: the Agent
            # checkpointed its open tool call and released the thread.
            (d / "pending_question.json").unlink(missing_ok=True)
            (d / "answered_question.json").unlink(missing_ok=True)
            _set_status(d, "cancelled", stop_reason="cancelled")
        elif ev is None:
            # A running status without a lease means the process lost the
            # worker (normally because the service was restarted).
            reason = "会话线程已不存在（可能是服务重启导致），已自动标记为异常。请重新开始本次会话。"
            (d / "pending_question.json").unlink(missing_ok=True)
            _set_status(d, "error", failure_reason=reason)
            missing_worker_reason = reason
        else:
            ev.set()
            # Removing this exact event is the generation cut-off. Old worker
            # callbacks and state saves now fail their lease checks immediately.
            if _STOP_EVENTS.get(run_id) is ev:
                _STOP_EVENTS.pop(run_id, None)
            (d / "pending_question.json").unlink(missing_ok=True)
            _set_status(d, "cancelled", stop_reason="cancelled")
    # Do not merely rely on the sandbox poller: if that worker thread also
    # failed, the durable ownership labels still let us terminate its exact
    # containers without touching another conversation.
    cleanup_managed_containers(d)
    if missing_worker_reason:
        raise RunStateError(missing_worker_reason)


def answer_question(
    run_id: str,
    *,
    answer: str | None = None,
    option_id: str | None = None,
) -> None:
    """Resolve a durable question and resume its exact open tool call."""
    from ..agent.build import session_state_path
    from ..tools.ask_user import claim_pending_answer, restore_claimed_answer

    d = _run_dir(run_id)
    with _STOP_LOCK:
        if _run_status(d) != "waiting_input":
            raise RunStateError("当前没有等待回答的问题（可能已经回答或会话已停止）。")
        if not session_state_path(d).is_file():
            raise RunStateError("待回答问题缺少可恢复的 Agent 检查点。")
        pq = d / "pending_question.json"
        try:
            question_id = json.loads(pq.read_text())["id"]
        except (OSError, json.JSONDecodeError, KeyError):
            raise RunStateError("无法读取待回答的问题。")
        try:
            claimed = claim_pending_answer(
                d,
                question_id,
                answer=answer,
                option_id=option_id,
            )
        except ValueError as exc:
            raise RunStateError(str(exc)) from exc
        try:
            _start_agent_thread(
                run_id,
                d,
                "",
                [],
                resume=True,
                internal_instruction=True,
                pending_answer=claimed,
            )
        except Exception:
            restore_claimed_answer(d, claimed)
            _set_status(d, "waiting_input")
            raise


def update_verification_prompt(run_id: str, prompt: str | None) -> dict:
    """Reject per-conversation prompt changes; verifier policy is fixed."""
    workdir = _run_dir(run_id)
    if not workdir.is_dir():
        raise ValueError("run not found")
    del prompt
    raise ValueError("验证 Agent Prompt 已由系统锁定，不能按会话修改。")


def update_verification_settings(
    run_id: str,
    max_attempts: int | None,
) -> dict:
    """Persist a per-conversation verification retry limit."""
    workdir = _run_dir(run_id)
    if not workdir.is_dir():
        raise ValueError("run not found")
    with _META_LOCK:
        meta = _meta(workdir)
        if max_attempts is None:
            meta.pop("verification_max_attempts", None)
        else:
            try:
                value = int(max_attempts)
            except (TypeError, ValueError) as exc:
                raise ValueError("最大验证轮数必须是整数。") from exc
            if not _VERIFICATION_ATTEMPTS_MIN <= value <= _VERIFICATION_ATTEMPTS_MAX:
                raise ValueError(
                    f"最大验证轮数必须在 {_VERIFICATION_ATTEMPTS_MIN} 到 "
                    f"{_VERIFICATION_ATTEMPTS_MAX} 之间。"
                )
            meta["verification_max_attempts"] = value
        _write_meta(workdir, meta)
    return _verification_settings(workdir)


def update_agent_settings(
    run_id: str,
    max_steps: int | None,
) -> dict:
    """Persist a per-conversation main Agent step budget."""
    workdir = _run_dir(run_id)
    if not workdir.is_dir():
        raise ValueError("run not found")
    with _META_LOCK:
        meta = _meta(workdir)
        if max_steps is None:
            meta.pop("main_agent_max_steps", None)
        else:
            try:
                value = int(max_steps)
            except (TypeError, ValueError) as exc:
                raise ValueError("主 Agent 最大步数必须是整数。") from exc
            if not _MAIN_AGENT_STEPS_MIN <= value <= _MAIN_AGENT_STEPS_MAX:
                raise ValueError(
                    f"主 Agent 最大步数必须在 {_MAIN_AGENT_STEPS_MIN} 到 "
                    f"{_MAIN_AGENT_STEPS_MAX} 之间。"
                )
            meta["main_agent_max_steps"] = value
        _write_meta(workdir, meta)
    return _agent_settings(workdir)


def update_verifier_settings(
    run_id: str,
    max_steps: int | None,
) -> dict:
    """Persist a per-conversation verification Agent step budget."""
    workdir = _run_dir(run_id)
    if not workdir.is_dir():
        raise ValueError("run not found")
    with _META_LOCK:
        meta = _meta(workdir)
        if max_steps is None:
            meta.pop("verification_agent_max_steps", None)
        else:
            try:
                value = int(max_steps)
            except (TypeError, ValueError) as exc:
                raise ValueError("验证 Agent 最大步数必须是整数。") from exc
            if not _VERIFIER_STEPS_MIN <= value <= _VERIFIER_STEPS_MAX:
                raise ValueError(
                    f"验证 Agent 最大步数必须在 {_VERIFIER_STEPS_MIN} 到 "
                    f"{_VERIFIER_STEPS_MAX} 之间。"
                )
            meta["verification_agent_max_steps"] = value
        _write_meta(workdir, meta)
    return _verifier_settings(workdir)


def update_subagent_settings(
    run_id: str,
    max_steps: int | None,
) -> dict:
    """Persist a per-conversation collaboration subagent step budget."""
    workdir = _run_dir(run_id)
    if not workdir.is_dir():
        raise ValueError("run not found")
    with _META_LOCK:
        meta = _meta(workdir)
        if max_steps is None:
            meta.pop("subagent_max_steps", None)
        else:
            try:
                value = int(max_steps)
            except (TypeError, ValueError) as exc:
                raise ValueError("协作 Agent 最大步数必须是整数。") from exc
            if not _SUBAGENT_STEPS_MIN <= value <= _SUBAGENT_STEPS_MAX:
                raise ValueError(
                    f"协作 Agent 最大步数必须在 {_SUBAGENT_STEPS_MIN} 到 "
                    f"{_SUBAGENT_STEPS_MAX} 之间。"
                )
            meta["subagent_max_steps"] = value
        _write_meta(workdir, meta)
    return _subagent_settings(workdir)


def update_project_budget_settings(run_id: str, value: object) -> dict:
    """Persist the authoritative per-revision cost cap for a project."""
    workdir = _run_dir(run_id)
    if not workdir.is_dir():
        raise ValueError("run not found")
    return update_project_budget_limit(workdir, value)  # type: ignore[arg-type]


def update_provider_settings(body: dict) -> dict:
    """Persist the local global provider or this public account's provider."""
    with _PROVIDER_SETTINGS_LOCK:
        if _PUBLIC_DEPLOYMENT and _AUTH_PROFILE_URL:
            return save_user_provider_settings(body, _CFG, _workspace())
        return save_provider_settings(
            provider=str(body.get("provider", "")),
            model=str(body.get("model", "")),
            base_url=str(body.get("base_url", "")),
            api_key=(
                str(body["api_key"])
                if body.get("api_key") is not None else None
            ),
            reasoning_effort=(
                str(body["reasoning_effort"])
                if body.get("reasoning_effort") is not None else None
            ),
            current_cfg=_CFG,
        )


def _set_status(
    workdir: Path,
    status: str,
    failure_reason: str | None = None,
    *,
    stop_reason: str | None = None,
    sync_project: bool = True,
) -> None:
    with _META_LOCK:
        m = _meta(workdir)
        m["status"] = status
        if status == "running":
            m["last_activity"] = time.time()
            m.pop("stop_reason", None)
        elif stop_reason:
            m["stop_reason"] = stop_reason
        if failure_reason:
            m["failure_reason"] = failure_reason
            m["failed_at"] = time.time()
        elif status != "error":
            m.pop("failure_reason", None)
            m.pop("failed_at", None)
        _write_meta(workdir, m)
    if sync_project:
        update_active_revision_status(workdir, status)


def retry_task(run_id: str) -> tuple[str, str]:
    """Launch a clean run using the original prompt and uploaded materials."""
    source = _run_dir(run_id)
    source_status = _run_status(source)
    if source_status not in {"error", "stopped", "cancelled"}:
        raise ValueError("only stopped, cancelled, or failed runs can be retried")

    meta = _meta(source)
    uploads = source / "_uploads"
    files = []
    if uploads.is_dir():
        for path in sorted(uploads.iterdir()):
            if path.is_file():
                files.append({
                    "name": path.name,
                    "b64": base64.b64encode(path.read_bytes()).decode("ascii"),
                })
    retry_name = _resolved_run_name(source, meta)
    new_run_id, name = launch_task(
        retry_name,
        meta.get("task", ""),
        files,
        retry_of=run_id,
        verification_max_attempts=meta.get("verification_max_attempts"),
        main_agent_max_steps=meta.get("main_agent_max_steps"),
        subagent_max_steps=meta.get("subagent_max_steps"),
        verification_agent_max_steps=meta.get("verification_agent_max_steps"),
    )
    return new_run_id, name


def delete_run(run_id: str) -> None:
    """Permanently remove one completed or empty conversation workspace."""
    workdir = _run_dir(run_id)
    workspace_root = _workspace().resolve()
    if workdir == workspace_root or not workdir.is_dir():
        raise ValueError("run not found")
    with _META_LOCK:
        status = _run_status(workdir)
        if status in {"running", "waiting_input"}:
            raise RunStateError("会话正在进行中，暂时不能删除。")
        shutil.rmtree(workdir)


# ----------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj) -> None:
        self._send(200, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

    def _authenticate_api_request(self) -> None:
        """Validate the existing site JWT and bind this request to one user."""
        if not _AUTH_PROFILE_URL:
            return
        authorization = self.headers.get("Authorization", "").strip()
        if not authorization.lower().startswith("bearer "):
            raise AuthenticationError("authentication required")
        profile_request = Request(
            _AUTH_PROFILE_URL,
            headers={"Authorization": authorization, "Accept": "application/json"},
        )
        try:
            with urlopen(profile_request, timeout=10) as response:
                payload = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise AuthenticationError("invalid or expired account token") from exc
        user = payload.get("data") if isinstance(payload, dict) else None
        user_id = user.get("id") if isinstance(user, dict) else None
        if user_id is None:
            # Some existing account-validation endpoints intentionally return
            # only balance metadata. Their successful 2xx response has already
            # verified the JWT and user; recover the signed subject only after
            # that validation instead of trusting an unverified bearer token.
            try:
                token = authorization.split(None, 1)[1]
                encoded_payload = token.split(".")[1]
                encoded_payload += "=" * (-len(encoded_payload) % 4)
                claims = json.loads(base64.urlsafe_b64decode(encoded_payload))
                user_id = claims.get("sub")
            except (IndexError, TypeError, ValueError, json.JSONDecodeError):
                user_id = None
        if user_id is None:
            raise AuthenticationError("invalid account profile")
        _REQUEST_SCOPE.user_id = str(user_id)

    def _clear_request_scope(self) -> None:
        if hasattr(_REQUEST_SCOPE, "user_id"):
            del _REQUEST_SCOPE.user_id

    def _serve_spa(self, request_path: str) -> None:
        """Serve a Vite build asset, falling back to the SPA entry point."""
        relative = request_path.lstrip("/") or "index.html"
        candidate = (STATIC_DIR / relative).resolve()
        if not str(candidate).startswith(str(STATIC_DIR.resolve())):
            self._send(404, b"not found", "text/plain")
            return
        if candidate.is_file():
            ctype = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
            self._send(200, candidate.read_bytes(), ctype)
            return
        if STATIC_INDEX.is_file():
            self._send(200, STATIC_INDEX.read_bytes(), "text/html; charset=utf-8")
            return
        self._send(
            503,
            b"Dashboard UI is not built. Run: cd mathmodel/dashboard/frontend && pnpm install && pnpm build",
            "text/plain; charset=utf-8",
        )

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/health":
                self._json({"ok": True})
                return
            if u.path.startswith("/api/"):
                self._authenticate_api_request()
            if u.path == "/api/runs":
                self._json(list_runs())
            elif u.path == "/api/provider-settings":
                with _PROVIDER_SETTINGS_LOCK:
                    self._json(_provider_settings_payload())
            elif u.path == "/api/run":
                self._json(run_detail(q.get("id", [""])[0]))
            elif u.path == "/api/file":
                d = _run_dir(q.get("id", [""])[0])
                fp = (d / q.get("path", [""])[0]).resolve()
                if not str(fp).startswith(str(d)) or not fp.is_file():
                    self._send(404, b"not found", "text/plain"); return
                ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
                self._send(200, fp.read_bytes(), ctype)
            elif not u.path.startswith("/api/"):
                self._serve_spa(u.path)
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as e:
            code = 401 if isinstance(e, AuthenticationError) else 500
            self._send(code, json.dumps({"error": str(e)}).encode(), "application/json")
        finally:
            self._clear_request_scope()

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path.startswith("/api/"):
                self._authenticate_api_request()
            if u.path == "/api/drafts":
                run_id, name = create_draft()
                self._json({"id": run_id, "name": name})
            elif u.path == "/api/provider-settings":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                self._json(update_provider_settings(body))
            elif u.path == "/api/tasks":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                task = (body.get("task") or "").strip()
                files = body.get("files") or []
                if not task and not files:
                    self._send(400, b'{"error":"empty task"}', "application/json"); return
                run_id, name = launch_task(body.get("name", ""), task, files, body.get("run_id"))
                self._json({"id": run_id, "name": name})
            elif u.path == "/api/continue":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                run_id, name = continue_task(
                    str(body.get("id", "")), (body.get("task") or "").strip(), body.get("files") or [],
                )
                self._json({"id": run_id, "name": name})
            elif u.path == "/api/continue-after-max-steps":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                self._json(continue_after_max_steps(str(body.get("id", ""))))
            elif u.path == "/api/retry":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                run_id, name = retry_task(str(body.get("id", "")))
                self._json({"id": run_id, "name": name})
            elif u.path == "/api/stop":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                stop_run(str(body.get("id", "")))
                self._json({"ok": True})
            elif u.path == "/api/answer":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                answer_question(
                    str(body.get("id", "")),
                    answer=(
                        str(body["answer"])
                        if body.get("answer") is not None else None
                    ),
                    option_id=(
                        str(body["option_id"])
                        if body.get("option_id") is not None else None
                    ),
                )
                self._json({"ok": True})
            elif u.path == "/api/verification-prompt":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                run_id = str(body.get("id", ""))
                prompt = None if body.get("reset") else str(body.get("prompt", ""))
                self._json(update_verification_prompt(run_id, prompt))
            elif u.path == "/api/verification-settings":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                run_id = str(body.get("id", ""))
                max_attempts = None if body.get("reset") else body.get("max_attempts")
                self._json(update_verification_settings(run_id, max_attempts))
            elif u.path == "/api/agent-settings":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                run_id = str(body.get("id", ""))
                max_steps = None if body.get("reset") else body.get("max_steps")
                self._json(update_agent_settings(run_id, max_steps))
            elif u.path == "/api/verifier-settings":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                run_id = str(body.get("id", ""))
                max_steps = None if body.get("reset") else body.get("max_steps")
                self._json(update_verifier_settings(run_id, max_steps))
            elif u.path == "/api/subagent-settings":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                run_id = str(body.get("id", ""))
                max_steps = None if body.get("reset") else body.get("max_steps")
                self._json(update_subagent_settings(run_id, max_steps))
            elif u.path == "/api/project-budget-settings":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                self._json(update_project_budget_settings(
                    str(body.get("id", "")),
                    body.get("revision_budget_limit_cny"),
                ))
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as e:
            code = 401 if isinstance(e, AuthenticationError) else 409 if isinstance(e, RunStateError) else 400 if isinstance(e, ValueError) else 500
            self._send(code, json.dumps({"error": str(e)}).encode(), "application/json")
        finally:
            self._clear_request_scope()

    def do_DELETE(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path.startswith("/api/"):
                self._authenticate_api_request()
            if u.path == "/api/run":
                delete_run(q.get("id", [""])[0])
                self._json({"ok": True})
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as e:
            code = 401 if isinstance(e, AuthenticationError) else 409 if isinstance(e, RunStateError) else 500
            self._send(code, json.dumps({"error": str(e)}).encode(), "application/json")
        finally:
            self._clear_request_scope()


def _reconcile_orphaned_runs(
    cleanup_containers=cleanup_managed_containers,
) -> None:
    """Reconcile lost workers while preserving durable human checkpoints.

    A running worker cannot survive a process restart and becomes an error. A
    waiting_input run is different: it intentionally has no worker and remains
    answerable when both its question and open tool call exist on disk.
    """
    reason = "服务重启导致本次会话中断，未能继续运行。请重新开始本次会话。"
    roots = [WORKSPACE]
    users_root = WORKSPACE / ".users"
    if users_root.is_dir():
        roots.extend(d for d in users_root.iterdir() if d.is_dir())
    run_dirs = [
        d
        for root in roots
        if root.is_dir()
        for d in root.iterdir()
        if d.is_dir() and d.name != ".users"
    ]
    for d in run_dirs:
        status = _meta(d).get("status")
        if status in {"running", "waiting_input"}:
            try:
                cleanup = cleanup_containers(d)
            except Exception as exc:
                # Docker cleanup is best-effort and must never prevent the
                # dashboard itself from starting or the zombie status from
                # being reconciled.
                cleanup = ContainerCleanupResult(error=str(exc))
            resumable_question = (
                status == "waiting_input" and _durable_question_is_resumable(d)
            )
            if not resumable_question:
                (d / "pending_question.json").unlink(missing_ok=True)
                _set_status(d, "error", failure_reason=reason)
            with _META_LOCK:
                meta = _meta(d)
                meta["orphan_container_cleanup"] = {
                    "attempted_at": time.time(),
                    "matched": list(cleanup.matched),
                    "removed": list(cleanup.removed),
                    "error": cleanup.error,
                }
                _write_meta(d, meta)


def _durable_question_is_resumable(workdir: Path) -> bool:
    try:
        question = json.loads((workdir / "pending_question.json").read_text())
        tool_call_id = str(question["tool_call_id"])
        if not question.get("id") or not tool_call_id:
            return False
        state = json.loads((workdir / "session_state.json").read_text())
        open_ids: set[str] = set()
        for message in state.get("messages", []):
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    call_id = str(call.get("id") or "")
                    if call_id:
                        open_ids.add(call_id)
            elif message.get("role") == "tool":
                open_ids.discard(str(message.get("tool_call_id") or ""))
        return tool_call_id in open_ids
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    _reconcile_orphaned_runs()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"mathmodel dashboard -> http://{args.host}:{args.port}")
    print(f"serving + launching runs in {WORKSPACE}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
