"""Serve the read-only Experimental Inspector on a standalone local port.

Run:
    ./.venv/bin/python -m mathmodel.experimental_inspector.server --port 8767

The server never imports or controls the experiment supervisor. It only reads
durable files below ``experiments/`` and serves the prebuilt React UI.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from ..config import PROJECT_ROOT
from ..contextlog import (
    CONTEXT_LOG_FILENAME,
    context_log_stats,
    read_context_request,
    read_context_request_summaries,
    request_detail,
)


EXPERIMENTS_ROOT = Path(
    os.environ.get("MATHMODEL_EXPERIMENTS_ROOT", PROJECT_ROOT / "experiments")
).expanduser().resolve()
STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "static"
STATIC_INDEX = STATIC_DIR / "experiment.html"
MAX_TEXT_BYTES = 120_000
MAX_LOG_BYTES = 100_000
TERMINAL_CASE_STATUSES = {"completed", "failed", "stopped"}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _read_text(path: Path, limit: int = MAX_TEXT_BYTES) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) <= limit:
        return data.decode("utf-8", errors="replace")
    return (
        data[:limit].decode("utf-8", errors="replace")
        + f"\n\n[truncated after {limit:,} bytes]"
    )


def _tail_text(path: Path, limit: int = MAX_LOG_BYTES) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > limit and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def _parse_timestamp(value: Any, fallback: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            pass
    return fallback


def _safe_directory(root: Path, name: str, label: str) -> Path:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"bad {label}")
    target = (root / name).resolve()
    if target.parent != root.resolve() or not target.is_dir():
        raise FileNotFoundError(f"{label} not found")
    return target


def _experiment_dir(experiment_id: str) -> Path:
    return _safe_directory(EXPERIMENTS_ROOT, experiment_id, "experiment")


def _case_dir(experiment_dir: Path, case_slug: str) -> Path:
    return _safe_directory(experiment_dir / "cases", case_slug, "case")


def _workspace(experiment_id: str, case_slug: str) -> Path:
    workspace = _case_dir(_experiment_dir(experiment_id), case_slug) / "workspace"
    if not workspace.is_dir():
        raise FileNotFoundError("case workspace not found")
    return workspace.resolve()


def _latest_mtime(paths: list[Path]) -> float | None:
    values: list[float] = []
    for path in paths:
        try:
            values.append(path.stat().st_mtime)
        except OSError:
            pass
    return max(values) if values else None


def _process_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_group_alive(process_group_id: Any) -> bool:
    if (
        not isinstance(process_group_id, int)
        or isinstance(process_group_id, bool)
        or process_group_id <= 0
    ):
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _effective_experiment_status(manifest: dict[str, Any]) -> str:
    recorded = str(manifest.get("status") or "unknown")
    if recorded != "running":
        return recorded

    supervisor_pid = manifest.get("supervisor_pid")
    process_group_id = manifest.get("process_group_id")
    if not isinstance(supervisor_pid, int) and not isinstance(process_group_id, int):
        # Older manifests have no process identity, so their durable status remains
        # the only trustworthy signal.
        return recorded
    if _process_alive(supervisor_pid):
        return "running"
    if _process_group_alive(process_group_id):
        return "orphaned"
    return "killed"


def _effective_case_status(status: dict[str, Any], experiment_status: str) -> str:
    recorded = str(status.get("status") or "unknown")
    if recorded in TERMINAL_CASE_STATUSES:
        return recorded
    if experiment_status == "killed":
        return "killed"
    if recorded == "running" and isinstance(status.get("pid"), int):
        if not _process_alive(status["pid"]):
            return "killed"
    return recorded


def _derived_case_status(
    experiment_dir: Path,
    status: dict[str, Any],
) -> dict[str, Any]:
    manifest = _read_json(experiment_dir / "manifest.json")
    effective = _effective_case_status(status, _effective_experiment_status(manifest))
    recorded = str(status.get("status") or "unknown")
    if effective == recorded:
        return status
    return {
        **status,
        "status": effective,
        "recorded_status": recorded,
        "status_source": "process_detection",
    }


def _case_summary(
    experiment_dir: Path,
    record: dict[str, Any],
    experiment_status: str,
) -> dict[str, Any]:
    slug = str(record.get("slug") or record.get("name") or "")
    directory = experiment_dir / "cases" / slug
    status = _read_json(directory / "status.json")
    status_for_detection = {
        **status,
        "status": status.get("status") or record.get("status") or "unknown",
    }
    workspace = directory / "workspace"
    context_path = workspace / CONTEXT_LOG_FILENAME
    context_stats = context_log_stats(context_path) if context_path.is_file() else {
        "request_count": 0,
        "latest_request_ts": None,
        "latest_model": None,
    }
    return {
        "name": record.get("name") or slug,
        "slug": slug,
        "benchmark_case": record.get("benchmark_case") or record.get("name") or slug,
        "repetition": record.get("repetition"),
        "repetitions": record.get("repetitions"),
        "status": _effective_case_status(status_for_detection, experiment_status),
        "started_at": _parse_timestamp(status.get("started_at")) or None,
        "finished_at": _parse_timestamp(status.get("finished_at")) or None,
        "duration_seconds": status.get("duration_seconds"),
        "stop_reason": status.get("stop_reason"),
        "error": status.get("error"),
        "artifact_count": status.get("artifact_count", 0),
        "last_activity": _latest_mtime([
            directory / "status.json",
            directory / "console.log",
            workspace / "events.jsonl",
            context_path,
        ]),
        **context_stats,
    }


def list_experiments() -> list[dict[str, Any]]:
    if not EXPERIMENTS_ROOT.is_dir():
        return []
    experiments: list[dict[str, Any]] = []
    for directory in EXPERIMENTS_ROOT.iterdir():
        manifest_path = directory / "manifest.json"
        if not directory.is_dir() or not manifest_path.is_file():
            continue
        manifest = _read_json(manifest_path)
        experiment_status = _effective_experiment_status(manifest)
        cases = [
            _case_summary(directory, record, experiment_status)
            for record in manifest.get("cases", [])
            if isinstance(record, dict)
        ]
        submitted = _parse_timestamp(
            manifest.get("submitted_at"),
            directory.stat().st_mtime,
        )
        experiments.append({
            "id": manifest.get("id") or directory.name,
            "label": manifest.get("label") or manifest.get("id") or directory.name,
            "status": experiment_status,
            "submitted_at": submitted,
            "started_at": _parse_timestamp(manifest.get("started_at")) or None,
            "finished_at": _parse_timestamp(manifest.get("finished_at")) or None,
            "source_sha256": manifest.get("source_sha256"),
            "git": manifest.get("git") or {},
            "settings": manifest.get("settings") or {},
            "cases": cases,
            "last_activity": max(
                [submitted, *[float(case.get("last_activity") or 0) for case in cases]]
            ),
        })
    return sorted(experiments, key=lambda item: item["submitted_at"], reverse=True)


def get_experiment(experiment_id: str) -> dict[str, Any]:
    directory = _experiment_dir(experiment_id)
    manifest = _read_json(directory / "manifest.json")
    experiment_status = _effective_experiment_status(manifest)
    records = [record for record in manifest.get("cases", []) if isinstance(record, dict)]
    return {
        "id": manifest.get("id") or directory.name,
        "label": manifest.get("label") or manifest.get("id") or directory.name,
        "status": experiment_status,
        "submitted_at": _parse_timestamp(manifest.get("submitted_at"), directory.stat().st_mtime),
        "started_at": _parse_timestamp(manifest.get("started_at")) or None,
        "finished_at": _parse_timestamp(manifest.get("finished_at")) or None,
        "source_sha256": manifest.get("source_sha256"),
        "git": manifest.get("git") or {},
        "settings": manifest.get("settings") or {},
        "benchmark_source": manifest.get("benchmark_source"),
        "config_source": manifest.get("config_source"),
        "supervisor_pid": manifest.get("supervisor_pid"),
        "cases": [
            _case_summary(directory, record, experiment_status)
            for record in records
        ],
    }


def _read_events(path: Path, after: int = 0) -> tuple[list[dict[str, Any]], int]:
    if after < 0:
        after = 0
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            if after > size:
                after = 0
            handle.seek(after)
            data = handle.read()
    except OSError:
        return [], 0
    if not data:
        return [], after
    complete_size = len(data)
    if not data.endswith(b"\n"):
        last_newline = data.rfind(b"\n")
        if last_newline < 0:
            return [], after
        complete_size = last_newline + 1
        data = data[:complete_size]
    events: list[dict[str, Any]] = []
    for raw_line in data.splitlines():
        try:
            value = json.loads(raw_line)
            if isinstance(value, dict):
                events.append(value)
        except json.JSONDecodeError:
            continue
    return events, after + complete_size


def _artifact_kind(relative: str) -> str:
    suffix = Path(relative).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg"}:
        return "image"
    if suffix in {".xlsx", ".xls", ".csv"}:
        return "data"
    if suffix in {".md", ".txt", ".tex", ".json"}:
        return "text"
    return "file"


def _artifacts(workspace: Path, experiment_id: str, case_slug: str) -> list[dict[str, Any]]:
    candidates: list[Path] = []
    for relative in ("paper", "results", "figures", "verification"):
        directory = workspace / relative
        if directory.is_dir():
            candidates.extend(path for path in directory.rglob("*") if path.is_file())
    candidates.extend(path for path in workspace.glob("*.xlsx") if path.is_file())
    for name in ("final_summary.md", "plan.md", "decisions.md"):
        path = workspace / name
        if path.is_file():
            candidates.append(path)
    artifacts: list[dict[str, Any]] = []
    for path in sorted(set(candidates)):
        relative = str(path.relative_to(workspace))
        artifacts.append({
            "path": relative,
            "name": path.name,
            "kind": _artifact_kind(relative),
            "bytes": path.stat().st_size,
            "modified_at": path.stat().st_mtime,
            "url": (
                "/api/file?experiment_id=" + quote(experiment_id)
                + "&case=" + quote(case_slug)
                + "&path=" + quote(relative)
            ),
        })
    return artifacts


def _running_usage(workspace: Path, status: dict[str, Any]) -> dict[str, Any]:
    if isinstance(status.get("usage"), dict):
        return status["usage"]
    state = _read_json(workspace / "session_state.json")
    usage = state.get("total_usage")
    return usage if isinstance(usage, dict) else {}


def get_case(experiment_id: str, case_slug: str, events_after: int = 0) -> dict[str, Any]:
    experiment_dir = _experiment_dir(experiment_id)
    case_dir = _case_dir(experiment_dir, case_slug)
    workspace = case_dir / "workspace"
    if not workspace.is_dir():
        status = _derived_case_status(
            experiment_dir,
            _read_json(case_dir / "status.json"),
        )
        return {
            "experiment_id": experiment_id,
            "slug": case_slug,
            "status": status,
            "workspace_ready": False,
            "events": [],
            "events_cursor": 0,
            "task": _read_text(case_dir / "task.txt"),
            "plan": "",
            "decisions": "",
            "problem": "",
            "results": {},
            "artifacts": [],
            "console_log": _tail_text(case_dir / "console.log"),
            "usage": {},
            "context": {"request_count": 0},
        }
    workspace = workspace.resolve()
    events, cursor = _read_events(workspace / "events.jsonl", events_after)
    status = _derived_case_status(
        experiment_dir,
        _read_json(case_dir / "status.json"),
    )
    results: dict[str, str] = {}
    results_dir = workspace / "results"
    if results_dir.is_dir():
        for path in sorted(results_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in {".md", ".txt", ".json", ".csv"}:
                results[path.name] = _read_text(path, 30_000)
    context_path = workspace / CONTEXT_LOG_FILENAME
    context = context_log_stats(context_path) if context_path.is_file() else {"request_count": 0}
    return {
        "experiment_id": experiment_id,
        "slug": case_slug,
        "status": status,
        "workspace_ready": True,
        "events": events,
        "events_cursor": cursor,
        "task": _read_text(case_dir / "task.txt"),
        "plan": _read_text(workspace / "plan.md"),
        "decisions": _read_text(workspace / "decisions.md"),
        "problem": _read_text(workspace / "problem.md"),
        "results": results,
        "artifacts": _artifacts(workspace, experiment_id, case_slug),
        "console_log": _tail_text(case_dir / "console.log"),
        "usage": _running_usage(workspace, status),
        "context": context,
    }


def list_context_requests(experiment_id: str, case_slug: str) -> list[dict[str, Any]]:
    path = _workspace(experiment_id, case_slug) / CONTEXT_LOG_FILENAME
    return list(reversed(read_context_request_summaries(path)))


def list_agent_contexts(experiment_id: str, case_slug: str) -> list[dict[str, Any]]:
    """Group lightweight Context summaries by the Agent that made them."""
    requests = list_context_requests(experiment_id, case_slug)
    grouped: dict[str, dict[str, Any]] = {}
    for request in requests:
        role = str(request.get("agent_role") or "Unclassified")
        scope = str(request.get("agent_scope") or "")
        key = f"{role}::{scope}"
        group = grouped.setdefault(key, {
            "key": key,
            "agent_role": role,
            "agent_scope": scope,
            "request_count": 0,
            "completed_count": 0,
            "total_tokens": 0,
            "estimated_input_tokens": 0,
            "first_ts": None,
            "latest_ts": None,
            "latest_step": None,
            "phases": set(),
            "requests": [],
        })
        timestamp = request.get("ts")
        if isinstance(timestamp, (int, float)):
            group["first_ts"] = min(group["first_ts"] or timestamp, timestamp)
            group["latest_ts"] = max(group["latest_ts"] or timestamp, timestamp)
        step = request.get("step")
        if isinstance(step, int):
            group["latest_step"] = max(group["latest_step"] or step, step)
        usage = request.get("usage") or {}
        total_tokens = usage.get("total_tokens")
        if isinstance(total_tokens, (int, float)):
            group["total_tokens"] += int(total_tokens)
        estimated = request.get("estimated_input_tokens")
        if isinstance(estimated, (int, float)):
            group["estimated_input_tokens"] += int(estimated)
        phase = request.get("phase")
        if phase:
            group["phases"].add(str(phase))
        group["request_count"] += 1
        group["completed_count"] += int(request.get("status") == "completed")
        group["requests"].append(request)

    def sort_key(group: dict[str, Any]) -> tuple[int, int, str]:
        role = str(group["agent_role"])
        if role == "Main Agent":
            return (0, 0, role)
        if role.startswith("Subagent "):
            suffix = role.removeprefix("Subagent ")
            return (1, int(suffix) if suffix.isdigit() else 9999, role)
        return (2, 0, role.casefold())

    result: list[dict[str, Any]] = []
    for group in sorted(grouped.values(), key=sort_key):
        group["phases"] = sorted(group["phases"])
        result.append(group)
    return result


def get_context_request(
    experiment_id: str,
    case_slug: str,
    request_id: str,
) -> dict[str, Any]:
    path = _workspace(experiment_id, case_slug) / CONTEXT_LOG_FILENAME
    record = read_context_request(path, request_id)
    if record is None:
        raise FileNotFoundError("context request not found")
    return request_detail(record)


def resolve_artifact(experiment_id: str, case_slug: str, relative_path: str) -> Path:
    workspace = _workspace(experiment_id, case_slug)
    if not relative_path:
        raise ValueError("missing file path")
    target = (workspace / relative_path).resolve()
    if target == workspace or not str(target).startswith(str(workspace) + os.sep):
        raise ValueError("bad file path")
    if not target.is_file():
        raise FileNotFoundError("file not found")
    return target


class ClientDisconnected(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: Any) -> None:
        pass

    def _send(
        self,
        code: int,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ClientDisconnected from exc

    def _json(self, value: Any, code: int = 200) -> None:
        self._send(
            code,
            json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _serve_app(self, request_path: str) -> None:
        relative = request_path.lstrip("/")
        candidate = STATIC_INDEX if not relative else (STATIC_DIR / relative).resolve()
        if not str(candidate).startswith(str(STATIC_DIR.resolve())):
            self._send(404, b"not found", "text/plain")
            return
        if candidate.is_file():
            self._send(
                200,
                candidate.read_bytes(),
                mimetypes.guess_type(str(candidate))[0] or "application/octet-stream",
            )
            return
        if STATIC_INDEX.is_file():
            self._send(200, STATIC_INDEX.read_bytes(), "text/html; charset=utf-8")
            return
        self._send(503, b"Experimental Inspector UI is not built.", "text/plain")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        experiment_id = query.get("experiment_id", [""])[0]
        case_slug = query.get("case", [""])[0]
        try:
            if parsed.path == "/api/experiments":
                self._json(list_experiments())
            elif parsed.path == "/api/experiment":
                self._json(get_experiment(experiment_id))
            elif parsed.path == "/api/case":
                after = int(query.get("events_after", ["0"])[0] or 0)
                self._json(get_case(experiment_id, case_slug, after))
            elif parsed.path == "/api/context/requests":
                self._json(list_context_requests(experiment_id, case_slug))
            elif parsed.path == "/api/context/agents":
                self._json(list_agent_contexts(experiment_id, case_slug))
            elif parsed.path == "/api/context/request":
                self._json(get_context_request(
                    experiment_id,
                    case_slug,
                    query.get("request_id", [""])[0],
                ))
            elif parsed.path == "/api/file":
                path = resolve_artifact(
                    experiment_id,
                    case_slug,
                    query.get("path", [""])[0],
                )
                content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
                disposition = "inline" if content_type.startswith(("image/", "text/")) or content_type == "application/pdf" else "attachment"
                self._send(
                    200,
                    path.read_bytes(),
                    content_type,
                    {"Content-Disposition": f'{disposition}; filename="{path.name}"'},
                )
            elif not parsed.path.startswith("/api/"):
                self._serve_app(parsed.path)
            else:
                self._send(404, b"not found", "text/plain")
        except FileNotFoundError as exc:
            self._json({"error": str(exc)}, 404)
        except (ValueError, TypeError) as exc:
            self._json({"error": str(exc)}, 400)
        except ClientDisconnected:
            pass
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument(
        "--experiments-root",
        help="override the experiments directory (defaults to PROJECT_ROOT/experiments)",
    )
    args = parser.parse_args()
    global EXPERIMENTS_ROOT
    if args.experiments_root:
        EXPERIMENTS_ROOT = Path(args.experiments_root).expanduser().resolve()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"experimental inspector -> http://127.0.0.1:{args.port}")
    print(f"reading experiments from {EXPERIMENTS_ROOT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
