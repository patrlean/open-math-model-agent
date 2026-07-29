"""Checks for chat/modeling routing and canonical problem ingestion."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path

from mathmodel.agent import build as agent_build
from mathmodel.agent.loop import load_agent_state
from mathmodel.agent.prompts import LEGACY_MODELING_USER_SUFFIX
from mathmodel.agent.intent import route_new_message
from mathmodel.dashboard import server
from mathmodel.ingest.ingest import ingest
from mathmodel.providers.base import ChatResponse, Provider


class FakeRouter(Provider):
    def __init__(self, label: str) -> None:
        super().__init__("fake")
        self.label = label

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        return ChatResponse(text=self.label)


class FakeChatProvider(Provider):
    def __init__(self) -> None:
        super().__init__("fake-chat")

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        latest = next(
            (message["content"] for message in reversed(messages) if message["role"] == "user"),
            "",
        )
        return ChatResponse(text=f"闲聊回复：{latest}")


def wait_for_status(workdir: Path, run_id: str, expected: str = "done") -> None:
    deadline = time.time() + 3
    while time.time() < deadline:
        if server._meta(workdir / run_id).get("status") == expected:
            return
        time.sleep(0.02)
    raise AssertionError(
        f"{run_id} did not reach {expected}: {server._meta(workdir / run_id)}"
    )


def main() -> None:
    cfg: dict = {}

    assert route_new_message(cfg, "你好", has_files=False) == "chat"
    assert route_new_message(cfg, "你能做什么？", has_files=False) == "chat"
    assert route_new_message(cfg, "请建立一个人口增长模型", has_files=False) == "modeling"
    assert route_new_message(cfg, "随便一句", has_files=True) == "modeling"
    assert route_new_message(
        cfg,
        "SOCKS 代理是什么？",
        has_files=False,
        provider=FakeRouter("CHAT"),
    ) == "chat"
    assert route_new_message(
        cfg,
        "请分析这个具体任务",
        has_files=False,
        provider=FakeRouter("MODELING"),
    ) == "modeling"

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        ingest([], workdir, problem_text="建立一个人口增长模型。")
        problem = (workdir / "problem.md").read_text()
        assert "Problem entered in chat" in problem
        assert "人口增长模型" in problem

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        original_build_provider = agent_build.build_provider
        try:
            agent_build.build_provider = lambda cfg: FakeChatProvider()
            chat_cfg = {
                "sandbox": "local",
                "sandbox_python": sys.executable,
                "context": {"compact_threshold_tokens": 100_000},
            }
            first = agent_build.build_chat_agent(
                chat_cfg,
                workdir,
                stop_event=threading.Event(),
                resume=False,
            )
            assert first.run("你好") == "闲聊回复：你好"
            assert (workdir / "session_state.json").is_file()
            assert not (workdir / "problem.md").exists()

            resumed = agent_build.build_chat_agent(
                chat_cfg,
                workdir,
                stop_event=threading.Event(),
                resume=True,
            )
            assert resumed.run("你能做什么？") == "闲聊回复：你能做什么？"
            user_messages = [
                message for message in resumed.messages if message.get("role") == "user"
            ]
            assert len(user_messages) == 2
        finally:
            agent_build.build_provider = original_build_provider

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        original_workspace = server.WORKSPACE
        original_cfg = server._CFG
        original_build_provider = agent_build.build_provider
        try:
            server.WORKSPACE = workspace
            server._CFG = {
                "sandbox": "local",
                "sandbox_python": sys.executable,
                "context": {"compact_threshold_tokens": 100_000},
                "verification": {"enabled": False, "max_attempts": 1},
            }
            agent_build.build_provider = lambda cfg: FakeChatProvider()

            run_id, _ = server.launch_task("", "你好", [])
            wait_for_status(workspace, run_id)
            assert server._meta(workspace / run_id)["mode"] == "chat"
            assert not (workspace / run_id / "problem.md").exists()
            assert (workspace / run_id / "session_state.json").is_file()

            server.continue_task(run_id, "你能做什么？", [])
            wait_for_status(workspace, run_id)
            assert server._meta(workspace / run_id)["mode"] == "chat"
            assert not (workspace / run_id / "problem.md").exists()

            server.continue_task(run_id, "请建立一个人口增长模型", [])
            wait_for_status(workspace, run_id)
            assert server._meta(workspace / run_id)["mode"] == "modeling"
            problem = (workspace / run_id / "problem.md").read_text()
            assert "人口增长模型" in problem

            modeling_run_id, _ = server.launch_task(
                "",
                "请建立一个排队模型",
                [],
            )
            wait_for_status(workspace, modeling_run_id)
            modeling_dir = workspace / modeling_run_id
            first_events = [
                json.loads(line)
                for line in (modeling_dir / "events.jsonl").read_text().splitlines()
            ]
            assert [
                event["task"] for event in first_events if event["kind"] == "task"
            ] == ["请建立一个排队模型"]

            first_state = json.loads(
                (modeling_dir / "session_state.json").read_text()
            )
            assert "internal runtime contract" in first_state["messages"][0]["content"]
            assert [
                message["content"]
                for message in first_state["messages"]
                if message.get("role") == "user"
            ][-1] == "请建立一个排队模型"

            server.continue_task(modeling_run_id, "继续", [])
            wait_for_status(workspace, modeling_run_id)
            resumed_events = [
                json.loads(line)
                for line in (modeling_dir / "events.jsonl").read_text().splitlines()
            ]
            assert [
                event["task"] for event in resumed_events if event["kind"] == "task"
            ] == ["请建立一个排队模型", "继续"]
            resumed_state = json.loads(
                (modeling_dir / "session_state.json").read_text()
            )
            assert [
                message["content"]
                for message in resumed_state["messages"]
                if message.get("role") == "user"
            ][-1] == "继续"

            legacy_state_path = modeling_dir / "legacy_state.json"
            legacy_state_path.write_text(json.dumps({
                "messages": [
                    {"role": "system", "content": "system"},
                    {"role": "system", "content": "memory"},
                    {"role": "user", "content": "继续" + LEGACY_MODELING_USER_SUFFIX},
                ],
            }))
            loaded_legacy_state = load_agent_state(legacy_state_path)
            assert loaded_legacy_state is not None
            assert loaded_legacy_state["messages"][-1]["content"] == "继续"

            legacy_event = dict(resumed_events[-1])
            legacy_event.update({
                "kind": "task",
                "task": "继续" + LEGACY_MODELING_USER_SUFFIX,
            })
            with (modeling_dir / "events.jsonl").open("a") as event_file:
                event_file.write(json.dumps(legacy_event, ensure_ascii=False) + "\n")
            visible_detail = server.run_detail(modeling_run_id)
            assert [
                event["task"]
                for event in visible_detail["events"]
                if event["kind"] == "task"
            ][-1] == "继续"
        finally:
            server.WORKSPACE = original_workspace
            server._CFG = original_cfg
            agent_build.build_provider = original_build_provider

    print("intent router checks: passed")


if __name__ == "__main__":
    main()
