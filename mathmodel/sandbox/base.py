"""Sandbox abstraction layer.

Code the agent writes is executed only through a Sandbox, so the execution
backend (local subprocess for dev / Docker for isolation / cloud later) can be
swapped without touching agent logic.

Every sandbox runs code with `workdir` as the working directory, so generated
code reads inputs from `data/` and writes outputs to `results/`, `figures/`,
etc., and those artifacts land back on the host for the agent to read.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_EXEC_TIMEOUT_SECONDS = 7200


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    artifacts: list[str] = field(default_factory=list)  # workdir-relative paths created/modified
    timed_out: bool = False
    stopped: bool = False  # killed because the run's stop_event fired mid-execution
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.stopped


class Sandbox(ABC):
    def __init__(self, workdir: str | Path) -> None:
        self.workdir = Path(workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)

    def _snapshot(self) -> dict[str, float]:
        """mtime of every file under workdir, to detect artifacts after a run."""
        snap: dict[str, float] = {}
        for p in self.workdir.rglob("*"):
            if p.is_file():
                try:
                    snap[str(p.relative_to(self.workdir))] = p.stat().st_mtime
                except OSError:
                    pass
        return snap

    def _diff_artifacts(self, before: dict[str, float]) -> list[str]:
        after = self._snapshot()
        changed = [
            rel for rel, mt in after.items()
            if rel not in before or mt > before[rel]
        ]
        return sorted(changed)

    @abstractmethod
    def exec_python(
        self,
        code: str,
        timeout: int = DEFAULT_EXEC_TIMEOUT_SECONDS,
        stop_event: threading.Event | None = None,
    ) -> ExecResult:
        """Run `code` as a Python script in the sandbox with workdir as cwd.

        `stop_event`, if given, is polled while the process runs (not just
        before/after) -- so a user-initiated stop can kill an in-progress,
        long-running computation immediately instead of waiting for it to
        finish or hit `timeout` on its own.
        """
        raise NotImplementedError
