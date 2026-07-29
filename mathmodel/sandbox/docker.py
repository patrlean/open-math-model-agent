"""Docker sandbox (intended default).

Runs each snippet inside a container built from sandbox/Dockerfile, which
preinstalls the scientific stack. The run's workdir is bind-mounted at /work so
artifacts land back on the host. Resource limits (wall-clock timeout, memory,
no network by default) keep runaway code contained.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .base import DEFAULT_EXEC_TIMEOUT_SECONDS, ExecResult, Sandbox

DEFAULT_IMAGE = "mathmodel-sandbox:latest"
# How often to check the timeout/stop_event while the container runs. This
# bounds how long a stop click can be stuck behind a long-running computation.
_POLL_INTERVAL = 0.25


class DockerSandbox(Sandbox):
    def __init__(
        self,
        workdir: str | Path,
        image: str = DEFAULT_IMAGE,
        mem_limit: str = "4g",
        network: str = "none",
    ) -> None:
        super().__init__(workdir)
        self.image = image
        self.mem_limit = mem_limit
        self.network = network

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
        container_name = f"mathmodel-run-{uuid.uuid4().hex[:12]}"
        # Numerical code may read the current paper for consistency checks, but
        # paper mutations must go through write_paper/edit_paragraph so source,
        # PDF, revisions, and acceptance metrics stay synchronized.
        paper_dir = self.workdir / "paper"
        paper_dir.mkdir(exist_ok=True)

        cmd = [
            "docker", "run", "--rm", "--name", container_name,
            "--network", self.network,
            "--memory", self.mem_limit,
            "-v", f"{self.workdir}:/work",
            "-v", f"{paper_dir.resolve()}:/work/paper:ro",
            "-w", "/work",
            "-e", "MPLBACKEND=Agg",
            self.image,
            # `timeout` inside the container enforces the wall clock even if the
            # host-side Popen timeout is missed.
            "timeout", str(timeout), "python", script_name,
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        # Poll instead of one long blocking wait, so a stop request mid-run is
        # noticed within _POLL_INTERVAL rather than only once the container
        # exits or hits `timeout` on its own -- this is what makes the
        # dashboard's stop button responsive even during an expensive computation.
        stdout = stderr = ""
        exit_code = -1
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=_POLL_INTERVAL)
                exit_code = proc.returncode
                # 124 is GNU coreutils `timeout`'s exit code for a killed command.
                if exit_code == 124:
                    timed_out = True
                    stderr += f"\n[sandbox] timed out after {timeout}s"
                break
            except subprocess.TimeoutExpired:
                if stop_event is not None and stop_event.is_set():
                    stopped = True
                    break
                if time.monotonic() - start > timeout + 15:
                    timed_out = True
                    break

        if stopped or timed_out:
            # Stop both the docker client and the named container. This avoids a
            # hung child retaining the output pipes after the host timeout fired.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except (subprocess.SubprocessError, OSError):
                pass
            stdout, stderr = proc.communicate()
            exit_code = -1
            note = "stopped by user" if stopped else f"host-side timeout after {timeout}s"
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
