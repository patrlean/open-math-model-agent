"""Smoke checks for stale-run recovery and timeout cleanup.

Run with: ./.venv/bin/python -m scripts.check_dashboard_recovery
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from mathmodel.dashboard import server
from mathmodel.sandbox.docker import ContainerCleanupResult
from mathmodel.sandbox.local import LocalSandbox


def check_stale_run_recovery() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        run = workspace / "stale-run"
        run.mkdir()
        now = time.time()
        (run / "meta.json").write_text(json.dumps({
            "name": "stale run",
            "task": "solve this",
            "created": now - 900,
            "last_activity": now - 900,
            "status": "running",
        }))
        events = run / "events.jsonl"
        events.write_text(json.dumps({"kind": "assistant", "ts": now - 900}) + "\n")
        os.utime(events, (now - 900, now - 900))
        uploads = run / "_uploads"
        uploads.mkdir()
        (uploads / "source.txt").write_text("retry input")

        original_workspace = server.WORKSPACE
        original_launch = server.launch_task
        try:
            server.WORKSPACE = workspace
            assert server._run_status(run) == "error"
            recovered = json.loads((run / "meta.json").read_text())
            assert recovered["failure_reason"].startswith("超过")

            captured: dict = {}

            def fake_launch(
                name,
                task,
                files,
                draft_id=None,
                retry_of=None,
                **settings,
            ):
                del draft_id, settings
                captured.update(name=name, task=task, files=files, retry_of=retry_of)
                return "fresh-run", "fresh run"

            server.launch_task = fake_launch
            assert server.retry_task("stale-run") == ("fresh-run", "fresh run")
            assert captured["retry_of"] == "stale-run"
            assert captured["task"] == "solve this"
            assert base64.b64decode(captured["files"][0]["b64"]) == b"retry input"
        finally:
            server.WORKSPACE = original_workspace
            server.launch_task = original_launch


def check_live_worker_is_not_marked_stale() -> None:
    """A slow API call may be quiet, but its worker lease is still authoritative."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        run = workspace / "slow-live-run"
        run.mkdir()
        now = time.time()
        (run / "meta.json").write_text(json.dumps({
            "name": "slow live run",
            "task": "solve this",
            "created": now - 900,
            "last_activity": now - 900,
            "status": "running",
        }))
        events = run / "events.jsonl"
        events.write_text(json.dumps({"kind": "context", "ts": now - 900}) + "\n")
        os.utime(events, (now - 900, now - 900))

        original_workspace = server.WORKSPACE
        try:
            server.WORKSPACE = workspace
            with server._STOP_LOCK:
                server._STOP_EVENTS[run.name] = threading.Event()
            assert server._run_status(run) == "running"
            assert json.loads((run / "meta.json").read_text())["status"] == "running"
        finally:
            with server._STOP_LOCK:
                server._STOP_EVENTS.pop(run.name, None)
            server.WORKSPACE = original_workspace


def check_startup_reclaims_only_orphaned_run_containers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        running = workspace / "running-run"
        waiting = workspace / "waiting-run"
        done = workspace / "done-run"
        for run, status in (
            (running, "running"),
            (waiting, "waiting_input"),
            (done, "done"),
        ):
            run.mkdir()
            (run / "meta.json").write_text(json.dumps({
                "name": run.name,
                "created": time.time(),
                "status": status,
            }))
        (waiting / "pending_question.json").write_text(json.dumps({
            "id": "question-1",
            "tool_call_id": "call-1",
        }))
        (waiting / "session_state.json").write_text(json.dumps({
            "messages": [{
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "ask_user", "arguments": "{}"},
                }],
            }],
        }))

        cleanup_calls: list[Path] = []

        def fake_cleanup(path: Path) -> ContainerCleanupResult:
            # Cleanup must happen while the durable status still identifies the
            # conversation as orphaned, before startup rewrites it to error.
            assert json.loads((path / "meta.json").read_text())["status"] in {
                "running", "waiting_input"
            }
            cleanup_calls.append(path)
            container_id = f"container-for-{path.name}"
            return ContainerCleanupResult(
                matched=(container_id,),
                removed=(container_id,),
            )

        original_workspace = server.WORKSPACE
        try:
            server.WORKSPACE = workspace
            server._reconcile_orphaned_runs(fake_cleanup)
        finally:
            server.WORKSPACE = original_workspace

        assert cleanup_calls == [running, waiting]
        for run in (running, waiting):
            meta = json.loads((run / "meta.json").read_text())
            assert meta["orphan_container_cleanup"]["removed"] == [
                f"container-for-{run.name}"
            ]
        assert json.loads((running / "meta.json").read_text())["status"] == "error"
        assert json.loads((waiting / "meta.json").read_text())["status"] == "waiting_input"
        assert json.loads((done / "meta.json").read_text())["status"] == "done"
        assert (waiting / "pending_question.json").exists()


def check_timeout_cleans_descendants() -> None:
    code = """import subprocess, sys, time
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
time.sleep(30)
"""
    with tempfile.TemporaryDirectory() as tmp:
        started = time.monotonic()
        result = LocalSandbox(tmp).exec_python(code, timeout=1)
        elapsed = time.monotonic() - started
    assert result.timed_out
    assert result.exit_code == -1
    assert elapsed < 5, f"timeout cleanup took {elapsed:.1f}s"


def main() -> None:
    check_stale_run_recovery()
    check_live_worker_is_not_marked_stale()
    check_startup_reclaims_only_orphaned_run_containers()
    check_timeout_cleans_descendants()
    print("dashboard recovery checks: passed")


if __name__ == "__main__":
    main()
