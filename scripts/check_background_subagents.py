"""Deterministic checks for first-completed background sub-agent collection."""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from mathmodel.agent.loop import Agent
from mathmodel.providers.base import ChatResponse, Provider
from mathmodel.tools.base import ToolContext, ToolRegistry
from mathmodel.tools.subagent import make_background_subagent_tools


class ControlledProvider(Provider):
    def __init__(self) -> None:
        super().__init__("controlled")
        self.release_slow = threading.Event()

    def chat(self, messages, tools=None, **kwargs):
        task = next(
            message["content"] for message in reversed(messages)
            if message.get("role") == "user"
        )
        if "slow task" in task:
            assert self.release_slow.wait(3), "slow task was never released"
            return ChatResponse(text="slow result")
        time.sleep(0.05)
        return ChatResponse(text="fast result")


class TwoTurnProvider(Provider):
    def __init__(self) -> None:
        super().__init__("two-turn")
        self.calls = 0

    def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        return ChatResponse(text=f"lead response {self.calls}")


def check_first_completed_snapshot() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        provider = ControlledProvider()
        events: list[tuple[str, dict]] = []
        spawn, collect, manager = make_background_subagent_tools(
            provider,
            ToolRegistry,
            "sub-agent system",
            max_steps=2,
            on_event=lambda kind, data: events.append((kind, data)),
        )
        ctx = ToolContext(workdir=Path(tmp), sandbox=None)

        started = time.monotonic()
        first_handle = spawn.handler(ctx, {"task": "fast task"})
        second_handle = spawn.handler(ctx, {"task": "slow task"})
        assert time.monotonic() - started < 0.25, "spawn blocked on sub-agent work"
        assert "SUB-1" in first_handle and "SUB-2" in second_handle

        first_snapshot = collect.handler(ctx, {
            "mode": "first_completed",
            "timeout_seconds": 2,
        })
        assert "SUB-1 — completed" in first_snapshot
        assert "fast result" in first_snapshot
        assert "## Sub-agents still running" in first_snapshot
        assert "SUB-2: slow task" in first_snapshot
        pending = manager.pending_summary()
        assert pending is not None and "SUB-2: slow task" in pending

        provider.release_slow.set()
        final_snapshot = collect.handler(ctx, {
            "mode": "all_completed",
            "timeout_seconds": 2,
        })
        assert "SUB-2 — completed" in final_snapshot
        assert "slow result" in final_snapshot
        assert "## Sub-agents still running\nNone." in final_snapshot
        assert manager.pending_summary() is None

        starts = [data["subagent"] for kind, data in events if kind == "subagent_start"]
        ends = [data["subagent"] for kind, data in events if kind == "subagent_end"]
        assert starts == [1, 2]
        assert sorted(ends) == [1, 2]


def check_lead_cannot_finish_with_uncollected_work() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        provider = TwoTurnProvider()
        pending_checks = 0
        events: list[tuple[str, dict]] = []

        def pending_work() -> str | None:
            nonlocal pending_checks
            pending_checks += 1
            return "SUB-2 is still running: slow task" if pending_checks == 1 else None

        agent = Agent(
            provider,
            ToolRegistry(),
            ToolContext(Path(tmp), sandbox=None),
            "system",
            max_steps=2,
            on_event=lambda kind, data: events.append((kind, data)),
            pending_work=pending_work,
        )
        result = agent.run("coordinate tasks")
        assert provider.calls == 2, "lead treated its first response as final"
        assert result == "lead response 2"
        assert any(kind == "background_pending" for kind, _ in events)
        done_events = [data for kind, data in events if kind == "done"]
        assert len(done_events) == 1 and done_events[0]["text"] == "lead response 2"
        assert any(
            message.get("role") == "user"
            and "SUB-2 is still running" in message.get("content", "")
            for message in agent.messages
        )


def main() -> None:
    check_first_completed_snapshot()
    check_lead_cannot_finish_with_uncollected_work()
    print("background sub-agent checks: passed")


if __name__ == "__main__":
    main()
