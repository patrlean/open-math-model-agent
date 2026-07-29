"""Structured run logging.

JsonlLogger writes one JSON record per agent event to events.jsonl in the run
workdir -- a detailed, machine-readable trace the dashboard renders. Records
capture the runtime context (working-memory snapshot each turn), the model's
text/thinking, tool calls + arguments, tool observations, compaction, and tokens.

Compose it with a console printer via `compose(...)`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable


class JsonlLogger:
    def __init__(self, path: str | Path, append: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._t0 = time.time()
        if not append:
            # start fresh for a new run -- set append=True when resuming an
            # existing conversation, or this wipes its prior event history.
            self.path.write_text("")

    def __call__(self, kind: str, data: dict) -> None:
        now = time.time()
        rec = {"t": round(now - self._t0, 2), "ts": now, "kind": kind, **data}
        with self.path.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def compose(*loggers: Callable[[str, dict], None]) -> Callable[[str, dict], None]:
    def cb(kind: str, data: dict) -> None:
        for lg in loggers:
            if lg is not None:
                lg(kind, data)
    return cb
