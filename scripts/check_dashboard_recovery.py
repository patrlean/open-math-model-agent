"""Smoke checks for stale-run recovery and timeout cleanup.

Run with: ./.venv/bin/python -m scripts.check_dashboard_recovery
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from pathlib import Path

from mathmodel.dashboard import server
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
    check_timeout_cleans_descendants()
    print("dashboard recovery checks: passed")


if __name__ == "__main__":
    main()
