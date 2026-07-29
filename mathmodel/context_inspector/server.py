"""Serve the standalone Context Inspector on a separate local port.

Run:
    ./.venv/bin/python -m mathmodel.context_inspector.server --port 8766
"""

from __future__ import annotations

import argparse
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..config import PROJECT_ROOT
from ..contextlog import (
    CONTEXT_LOG_FILENAME,
    context_log_stats,
    read_context_requests,
    request_detail,
    request_summary,
)

WORKSPACE = PROJECT_ROOT / "workspace"
STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "static"
STATIC_INDEX = STATIC_DIR / "context.html"


def _safe_run_dir(run_id: str) -> Path:
    target = (WORKSPACE / run_id).resolve()
    root = WORKSPACE.resolve()
    if target == root or not str(target).startswith(str(root) + "/"):
        raise ValueError("bad run id")
    return target


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def list_context_runs() -> list[dict]:
    if not WORKSPACE.is_dir():
        return []
    runs: list[dict] = []
    for directory in WORKSPACE.iterdir():
        if not directory.is_dir():
            continue
        context_log = directory / CONTEXT_LOG_FILENAME
        # Historical conversations created before context capture cannot offer
        # any inspectable request payload, so keep them out of this dedicated UI.
        if not context_log.is_file():
            continue
        meta = _read_json(directory / "meta.json")
        stats = context_log_stats(context_log)
        runs.append({
            "id": directory.name,
            "name": meta.get("name") or directory.name,
            "status": meta.get("status") or "unknown",
            "created": meta.get("created") or directory.stat().st_mtime,
            "last_activity": meta.get("last_activity"),
            **stats,
        })
    return sorted(
        runs,
        key=lambda item: (
            float(item.get("latest_request_ts") or 0),
            float(item.get("created") or 0),
        ),
        reverse=True,
    )


def list_run_requests(run_id: str) -> list[dict]:
    directory = _safe_run_dir(run_id)
    records = read_context_requests(directory / CONTEXT_LOG_FILENAME)
    return [request_summary(record) for record in reversed(records)]


def get_request(run_id: str, request_id: str) -> dict:
    directory = _safe_run_dir(run_id)
    records = read_context_requests(directory / CONTEXT_LOG_FILENAME)
    record = next(
        (
            item for item in records
            if str(item.get("request_id")) == request_id
        ),
        None,
    )
    if record is None:
        raise FileNotFoundError("request not found")
    return request_detail(record)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _send(
        self,
        code: int,
        body: bytes,
        content_type: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if content_type.startswith("application/json"):
            self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value, code: int = 200) -> None:
        self._send(
            code,
            json.dumps(value, ensure_ascii=False, default=str).encode(),
            "application/json; charset=utf-8",
        )

    def _serve_app(self, request_path: str) -> None:
        relative = request_path.lstrip("/")
        if not relative:
            candidate = STATIC_INDEX
        else:
            candidate = (STATIC_DIR / relative).resolve()
        if not str(candidate).startswith(str(STATIC_DIR.resolve())):
            self._send(404, b"not found", "text/plain")
            return
        if candidate.is_file():
            content_type = (
                mimetypes.guess_type(str(candidate))[0]
                or "application/octet-stream"
            )
            self._send(200, candidate.read_bytes(), content_type)
            return
        if STATIC_INDEX.is_file():
            self._send(
                200,
                STATIC_INDEX.read_bytes(),
                "text/html; charset=utf-8",
            )
            return
        self._send(
            503,
            (
                b"Context Inspector UI is not built. Run npm run build in "
                b"mathmodel/dashboard/frontend."
            ),
            "text/plain; charset=utf-8",
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/runs":
                self._json(list_context_runs())
            elif parsed.path == "/api/requests":
                self._json(list_run_requests(query.get("run_id", [""])[0]))
            elif parsed.path == "/api/request":
                self._json(get_request(
                    query.get("run_id", [""])[0],
                    query.get("request_id", [""])[0],
                ))
            elif parsed.path == "/api/export":
                detail = get_request(
                    query.get("run_id", [""])[0],
                    query.get("request_id", [""])[0],
                )
                body = json.dumps(
                    detail["raw_request"],
                    ensure_ascii=False,
                    indent=2,
                ).encode()
                self._send(
                    200,
                    body,
                    "application/json; charset=utf-8",
                    extra_headers={
                        "Content-Disposition": (
                            f'attachment; filename="context-'
                            f'{detail["sequence"]}.json"'
                        ),
                    },
                )
            elif not parsed.path.startswith("/api/"):
                self._serve_app(parsed.path)
            else:
                self._send(404, b"not found", "text/plain")
        except FileNotFoundError as exc:
            self._json({"error": str(exc)}, 404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"context inspector -> http://127.0.0.1:{args.port}")
    print(f"reading context logs from {WORKSPACE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
