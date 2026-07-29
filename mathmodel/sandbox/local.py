"""Local subprocess sandbox (development fallback).

Runs code with a plain Python interpreter in a subprocess. This is NOT strongly
isolated (no cgroups, shares the host filesystem outside workdir). It exists so
the full pipeline can run without Docker during development; DockerSandbox is the
intended default for real use. See sandbox/docker.py.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .base import DEFAULT_EXEC_TIMEOUT_SECONDS, ExecResult, Sandbox

# How often to check the timeout/stop_event while the subprocess runs. This
# bounds how long a stop click can be stuck behind a long-running computation.
_POLL_INTERVAL = 0.25


class LocalSandbox(Sandbox):
    def __init__(self, workdir: str | Path, python: str | None = None) -> None:
        super().__init__(workdir)
        # Default to the interpreter running the agent; override via config for a
        # venv that has the scientific stack installed.
        self.python = python or sys.executable

    def exec_python(
        self,
        code: str,
        timeout: int = DEFAULT_EXEC_TIMEOUT_SECONDS,
        stop_event: threading.Event | None = None,
    ) -> ExecResult:
        # Unique per call: concurrent sub-agents share this sandbox, so a fixed
        # script name would let one run's code overwrite another's mid-flight.
        script_name = f"_run_{uuid.uuid4().hex[:8]}.py"
        script = self.workdir / script_name
        script.write_text(code)
        before = self._snapshot()
        start = time.monotonic()
        timed_out = False
        stopped = False
        proc = subprocess.Popen(
            [self.python, script_name],
            cwd=self.workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env={**os.environ, "MPLBACKEND": "Agg"},  # headless plotting
        )
        # Poll instead of one long blocking wait, so a stop request mid-run is
        # noticed within _POLL_INTERVAL rather than only once the process ends
        # or hits `timeout` on its own -- this is what makes the dashboard's
        # stop button responsive even during an expensive computation.
        stdout = stderr = ""
        exit_code = -1
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=_POLL_INTERVAL)
                exit_code = proc.returncode
                break
            except subprocess.TimeoutExpired:
                if stop_event is not None and stop_event.is_set():
                    stopped = True
                    break
                if time.monotonic() - start > timeout:
                    timed_out = True
                    break

        if stopped or timed_out:
            # A user snippet can spawn descendants that inherit stdout/stderr.
            # Killing only the direct interpreter leaves those pipes open and can
            # wedge communicate() forever, so terminate its complete process group.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
            note = "stopped by user" if stopped else f"timed out after {timeout}s"
            stderr = (stderr or "") + f"\n[sandbox] {note}"
        duration = time.monotonic() - start

        # Best-effort cleanup; unique name means we only ever remove our own script.
        try:
            script.unlink()
        except OSError:
            pass
        artifacts = [a for a in self._diff_artifacts(before)
                     if not a.startswith("_run")]
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            artifacts=artifacts,
            timed_out=timed_out,
            stopped=stopped,
            duration_s=round(duration, 3),
        )
