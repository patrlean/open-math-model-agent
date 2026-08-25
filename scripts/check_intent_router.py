"""Checks for unified-Agent lazy ingestion (the retired router's replacement)."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from mathmodel.agent import build as agent_build
from mathmodel.dashboard import server
from mathmodel.providers.base import ChatResponse, Provider, ToolCall


class FakeUnifiedProvider(Provider):
    def __init__(self) -> None:
        super().__init__("fake-unified")
        self.requests = 0

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        del kwargs
        self.requests += 1
        latest_user = next(
            (
                str(message.get("content") or "")
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            "",
        )
        latest = messages[-1]
        if latest.get("role") == "tool" and "generated problem.md" in str(
            latest.get("content") or ""
        ):
            return ChatResponse(text="建模工作区已就绪。")
        attachment_pending = any(
            message.get("role") == "system"
            and "Current-turn attachment metadata" in str(message.get("content") or "")
            for message in messages[-3:]
        )
        if "建立" in latest_user or attachment_pending:
            names = {
                schema.get("function", {}).get("name")
                for schema in (tools or [])
            }
            assert "ingest_problem" in names
            return ChatResponse(
                text=None,
                tool_calls=[ToolCall("ingest-1", "ingest_problem", "{}")],
            )
        return ChatResponse(text=f"闲聊回复：{latest_user}")


def wait_for_status(workspace: Path, run_id: str) -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        if server._meta(workspace / run_id).get("status") == "done":
            return
        time.sleep(0.02)
    raise AssertionError(server._meta(workspace / run_id))


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        original_workspace = server.WORKSPACE
        original_cfg = server._CFG
        original_build_provider = agent_build.build_provider
        provider = FakeUnifiedProvider()
        try:
            server.WORKSPACE = workspace
            server._CFG = {
                "sandbox": "local",
                "sandbox_python": sys.executable,
                "context": {"compact_threshold_tokens": 100_000},
                "verification": {"enabled": False, "max_attempts": 1},
            }
            agent_build.build_provider = lambda cfg: provider

            # Ordinary chat uses the same main Agent but never creates problem.md.
            run_id, _ = server.launch_task("", "你好", [])
            wait_for_status(workspace, run_id)
            run_dir = workspace / run_id
            assert server._meta(run_dir)["mode"] == "conversation"
            assert not (run_dir / "problem.md").exists()
            first_state = json.loads((run_dir / "session_state.json").read_text())
            assert any(
                message.get("role") == "user" and message.get("content") == "你好"
                for message in first_state["messages"]
            )

            # The same conversation can activate modeling without a router call.
            server.continue_task(run_id, "请建立一个人口增长模型", [])
            wait_for_status(workspace, run_id)
            assert server._meta(run_dir)["mode"] == "modeling"
            problem = (run_dir / "problem.md").read_text()
            assert "请建立一个人口增长模型" in problem

            # A file-only turn tells the Agent about attachments through an
            # internal system row while preserving the empty user-authored text.
            csv_b64 = "data:text/csv;base64,eCx5CjEsMgo="
            file_run, _ = server.launch_task(
                "",
                "",
                [{"name": "sample.csv", "b64": csv_b64}],
            )
            wait_for_status(workspace, file_run)
            file_dir = workspace / file_run
            assert (file_dir / "problem.md").is_file()
            assert (file_dir / "data" / "sample.csv").is_file()
            file_state = json.loads((file_dir / "session_state.json").read_text())
            assert any(
                message.get("role") == "user" and message.get("content") == ""
                for message in file_state["messages"]
            )
            assert any(
                message.get("role") == "system"
                and "Current-turn attachment metadata" in message.get("content", "")
                for message in file_state["messages"]
            )
        finally:
            server.WORKSPACE = original_workspace
            server._CFG = original_cfg
            agent_build.build_provider = original_build_provider

    print("unified Agent lazy-ingest checks: passed")


if __name__ == "__main__":
    main()
