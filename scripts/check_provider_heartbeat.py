"""Deterministic check for model-request heartbeats (no network).

Run with: ./.venv/bin/python -m scripts.check_provider_heartbeat
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from mathmodel.agent.loop import Agent
from mathmodel.providers.base import ChatResponse, Provider, Usage
from mathmodel.tools.base import ToolContext, ToolRegistry


class SlowProvider(Provider):
    def __init__(self) -> None:
        super().__init__(model="slow-test-model")

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        del messages, tools, kwargs
        time.sleep(0.08)
        return ChatResponse(text="done", usage=Usage(1, 1, 2))


def main() -> None:
    events: list[tuple[str, dict]] = []
    with tempfile.TemporaryDirectory() as tmp:
        ctx = ToolContext(
            workdir=Path(tmp),
            sandbox=None,  # unused by this test
            heartbeat_interval_seconds=0.02,
        )
        agent = Agent(
            SlowProvider(),
            ToolRegistry(),
            ctx,
            "SYS",
            max_steps=1,
            on_event=lambda kind, data: events.append((kind, data)),
        )
        assert agent.run("test") == "done"

    heartbeats = [data for kind, data in events if kind == "provider_heartbeat"]
    assert len(heartbeats) >= 2, heartbeats
    assert all(item["provider"] == "SlowProvider" for item in heartbeats)
    assert all(item["model"] == "slow-test-model" for item in heartbeats)
    assert all(item["request_phase"] == "agent_step" for item in heartbeats)
    assert all(item["step"] == 1 for item in heartbeats)
    assert heartbeats[-1]["elapsed_seconds"] >= heartbeats[0]["elapsed_seconds"]
    print(f"provider heartbeat checks: passed ({len(heartbeats)} heartbeats)")


if __name__ == "__main__":
    main()
