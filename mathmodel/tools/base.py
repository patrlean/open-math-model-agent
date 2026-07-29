"""Tool infrastructure: a registry the agent loop dispatches tool calls through.

Each Tool carries an OpenAI-style JSON schema (sent to the model) and a handler
`(ctx, args) -> str`. The returned string is the observation the model sees, so
handlers are responsible for keeping output compact (large output goes to files,
not into the context window).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..sandbox.base import Sandbox

TOOL_HEARTBEAT_SECONDS = 30.0
EventCallback = Callable[[str, dict[str, Any]], None]


@dataclass
class ToolContext:
    """Shared state handed to every tool handler for a run."""

    workdir: Path
    sandbox: Sandbox
    scope: str = ""  # prefix for log filenames, so subagents don't clobber parent logs
    run_counter: dict[str, int] = field(default_factory=dict)
    # Guards run_counter: parent + concurrent sub-agents share one ToolContext, so
    # index allocation (log/scope naming) must be atomic or two of them collide.
    _counter_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Shared with every sub-agent's ToolContext (see subagent.py), so setting it
    # once from outside (e.g. a dashboard "stop" button) cancels the whole tree:
    # the lead and every in-flight sub-agent check it between steps.
    stop_event: threading.Event = field(default_factory=threading.Event)
    # Read-only run settings for tools that enforce configuration-driven
    # acceptance criteria (for example target paper length).
    settings: dict[str, Any] = field(default_factory=dict)
    # Agent-owned event sink. Long-running tools use it to write durable
    # heartbeats without adding messages to the model conversation.
    on_event: EventCallback | None = field(default=None, repr=False)
    heartbeat_interval_seconds: float = field(
        default=TOOL_HEARTBEAT_SECONDS,
        repr=False,
    )

    def next_index(self, key: str) -> int:
        with self._counter_lock:
            self.run_counter[key] = self.run_counter.get(key, 0) + 1
            return self.run_counter[key]


Handler = Callable[[ToolContext, dict[str, Any]], str]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema for the arguments object
    handler: Handler

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def dispatch(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"[error] unknown tool '{name}'"
        heartbeat_done = threading.Event()
        heartbeat_thread: threading.Thread | None = None
        started_at = time.monotonic()

        if ctx.on_event is not None and ctx.heartbeat_interval_seconds > 0:
            def emit_heartbeats() -> None:
                while not heartbeat_done.wait(ctx.heartbeat_interval_seconds):
                    try:
                        ctx.on_event("tool_heartbeat", {
                            "name": name,
                            "elapsed_seconds": round(
                                time.monotonic() - started_at,
                                1,
                            ),
                            "scope": ctx.scope,
                        })
                    except Exception:
                        # Telemetry must never interrupt the tool it observes.
                        return

            heartbeat_thread = threading.Thread(
                target=emit_heartbeats,
                name=f"tool-heartbeat-{name}",
                daemon=True,
            )
            heartbeat_thread.start()
        try:
            return tool.handler(ctx, args)
        except Exception as e:  # tools must never crash the loop
            return f"[error] tool '{name}' raised {type(e).__name__}: {e}"
        finally:
            heartbeat_done.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=0.1)


def tail(text: str, max_lines: int = 40, max_chars: int = 4000) -> str:
    """Keep output compact: last N lines, capped at max_chars, with a note."""
    if not text:
        return ""
    lines = text.splitlines()
    clipped = False
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        clipped = True
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[-max_chars:]
        clipped = True
    if clipped:
        out = "...[truncated; full output in logs/]...\n" + out
    return out
