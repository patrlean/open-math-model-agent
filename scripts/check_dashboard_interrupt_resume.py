"""Regression checks for rapid continue -> stop -> continue transitions."""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

from mathmodel.agent.loop import Agent
from mathmodel.dashboard import server
from mathmodel.providers.base import ChatResponse, Provider, ToolCall
from mathmodel.sandbox.local import LocalSandbox
from mathmodel.tools.base import ToolContext, ToolRegistry


class BlockingProvider(Provider):
    def __init__(self) -> None:
        super().__init__("blocking-test")
        self.started = threading.Event()
        self.release = threading.Event()

    def chat(self, messages, tools=None, **kwargs):
        self.started.set()
        assert self.release.wait(2), "test provider was never released"
        return ChatResponse(text="late response that must be discarded")


class ThinkingToolProvider(Provider):
    def __init__(self) -> None:
        super().__init__("thinking-tool-test")
        self.calls = 0

    def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                text="",
                reasoning_content="Let me inspect the uploaded data first.",
                tool_calls=[ToolCall(
                    id="missing-tool",
                    name="read_file",
                    arguments='{"path":"problem.md"}',
                )],
            )
        return ChatResponse(text="Finished.")


def check_thinking_progress_is_emitted_separately() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        events: list[tuple[str, dict]] = []
        agent = Agent(
            ThinkingToolProvider(),
            ToolRegistry(),
            ToolContext(workdir, LocalSandbox(workdir)),
            "system",
            max_steps=2,
            on_event=lambda kind, data: events.append((kind, data)),
        )
        assert agent.run("solve") == "Finished."
        assistant_event = next(data for kind, data in events if kind == "assistant")
        assert assistant_event["text"] == ""
        assert (
            assistant_event["reasoning_text"]
            == "Let me inspect the uploaded data first."
        )


def check_late_provider_response_is_discarded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        provider = BlockingProvider()
        stop_event = threading.Event()
        events: list[tuple[str, dict]] = []
        state_path = workdir / "session_state.json"
        agent = Agent(
            provider,
            ToolRegistry(),
            ToolContext(workdir, LocalSandbox(workdir), stop_event=stop_event),
            "system",
            on_event=lambda kind, data: events.append((kind, data)),
            state_path=state_path,
        )
        thread = threading.Thread(target=lambda: agent.run("继续"), daemon=True)
        thread.start()
        assert provider.started.wait(1)
        stop_event.set()
        provider.release.set()
        thread.join(2)

        assert not thread.is_alive()
        assert agent.last_stop_reason == "cancelled"
        assert not any(kind == "assistant" for kind, _ in events)
        persisted = json.loads(state_path.read_text())
        assert not any(
            message.get("content") == "late response that must be discarded"
            for message in persisted["messages"]
        )


def check_stop_is_immediately_continuable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        run_id = "rapid-resume"
        workdir = workspace / run_id
        workdir.mkdir()
        (workdir / "meta.json").write_text(json.dumps({
            "name": "rapid resume",
            "task": "original task",
            "created": time.time(),
            "status": "running",
        }))
        (workdir / "session_state.json").write_text(json.dumps({
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "system", "content": "memory"},
            ],
            "run_counter": {},
            "total_usage": {},
            "context_tokens": 0,
        }))

        original_workspace = server.WORKSPACE
        original_start = server._start_agent_thread
        old_event = threading.Event()
        server.WORKSPACE = workspace
        server._STOP_EVENTS[run_id] = old_event
        captured: list[tuple[str, str]] = []

        def fake_start(rid, directory, task, saved, *, resume):
            captured.append((rid, task))
            server._set_status(directory, "running")

        try:
            server._start_agent_thread = fake_start
            started = time.monotonic()
            server.stop_run(run_id)
            assert time.monotonic() - started < 0.5
            assert old_event.is_set()
            assert run_id not in server._STOP_EVENTS
            assert server._run_status(workdir) == "cancelled"

            assert server.continue_task(run_id, "再次继续", []) == (run_id, "rapid resume")
            assert captured == [(run_id, "再次继续")]
        finally:
            server._STOP_EVENTS.pop(run_id, None)
            server._start_agent_thread = original_start
            server.WORKSPACE = original_workspace


def main() -> None:
    check_thinking_progress_is_emitted_separately()
    check_late_provider_response_is_discarded()
    check_stop_is_immediately_continuable()
    print("dashboard interrupt/resume checks: passed")


if __name__ == "__main__":
    main()
