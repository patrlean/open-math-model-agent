"""Smoke checks for dashboard conversation creation and deletion rules.

Run with: ./.venv/bin/python -m scripts.check_dashboard_conversations
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from mathmodel.dashboard import server


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        original_workspace = server.WORKSPACE
        try:
            server.WORKSPACE = workspace

            draft_id, draft_name = server.create_draft()
            assert draft_name == "新建项目"
            draft_detail = server.run_detail(draft_id)
            assert draft_detail["project"]["current_revision_id"] == "rev_0001"
            try:
                server.create_draft()
            except server.RunStateError as exc:
                assert "发送" in str(exc)
            else:
                raise AssertionError("a second empty draft was allowed")

            try:
                server.delete_run(draft_id)
            except Exception as exc:  # pragma: no cover - makes the failure clearer in a smoke run
                raise AssertionError("an empty draft should be deletable") from exc
            assert not (workspace / draft_id).exists()

            completed_id, _ = server.create_draft()
            server._set_status(workspace / completed_id, "done")
            server.delete_run(completed_id)
            assert not (workspace / completed_id).exists()

            running_id, _ = server.create_draft()
            server._set_status(workspace / running_id, "running")
            next_draft_id, _ = server.create_draft()
            assert (workspace / next_draft_id).is_dir(), "a running task should not block a new draft"
            try:
                server.delete_run(running_id)
            except server.RunStateError as exc:
                assert "进行中" in str(exc)
            else:
                raise AssertionError("a running conversation was deleted")

            generated = server._generated_run_name(
                "请帮我建立无人机烟幕投放策略优化模型，并给出验证结果。",
                [],
            )
            assert generated == "无人机烟幕投放策略优化模型"
            assert "·" not in generated and ":" not in generated

            file_generated = server._generated_run_name(
                "",
                [{"name": "result3.xlsx"}, {"name": "A题.pdf"}],
            )
            assert file_generated == "A题", "problem sources should take priority over output templates"

            legacy_dir = workspace / "legacy-file-title"
            legacy_dir.mkdir()
            (legacy_dir / "problem.md").write_text(
                "# Problem Materials\n\n"
                "## From `A题.pdf` (PDF)\n\n"
                "[page 1]\n"
                "A 题  烟幕干扰弹的投放策略\n"
            )
            (legacy_dir / "meta.json").write_text(json.dumps({
                "name": "result3 · 13:18 · 重试",
                "task": "",
                "files": ["result3.xlsx", "A题.pdf"],
            }, ensure_ascii=False))
            assert server._resolved_run_name(legacy_dir) == "烟幕干扰弹的投放策略"

            verifier_settings = server.update_verifier_settings("legacy-file-title", 196)
            assert verifier_settings["max_steps"] == 196
            assert verifier_settings["is_custom"] is True
            reset_verifier_settings = server.update_verifier_settings("legacy-file-title", None)
            assert reset_verifier_settings["max_steps"] == server._default_verifier_steps()
            assert reset_verifier_settings["is_custom"] is False

            agent_settings = server.update_agent_settings("legacy-file-title", 240)
            assert agent_settings["max_steps"] == 240
            assert agent_settings["is_custom"] is True
            reset_agent_settings = server.update_agent_settings("legacy-file-title", None)
            assert reset_agent_settings["max_steps"] == 200
            assert reset_agent_settings["default_max_steps"] == 200
            assert reset_agent_settings["is_custom"] is False

            subagent_settings = server.update_subagent_settings("legacy-file-title", 72)
            assert subagent_settings["max_steps"] == 72
            assert subagent_settings["is_custom"] is True
            detail = server.run_detail("legacy-file-title")
            assert detail["subagent_settings"]["max_steps"] == 72
            reset_subagent_settings = server.update_subagent_settings(
                "legacy-file-title",
                None,
            )
            assert reset_subagent_settings["max_steps"] == 60
            assert reset_subagent_settings["is_custom"] is False

            paper_dir = workspace / "paper-name" / "paper"
            paper_dir.mkdir(parents=True)
            (paper_dir / "main.tex").write_text(
                r"\title{无人机烟幕投放策略优化}\begin{document}\end{document}"
            )
            (paper_dir / "main.pdf").write_bytes(b"%PDF-1.7\n" + b"x" * 1200)
            (paper_dir / "delivery.json").write_text(json.dumps({
                "title": "无人机烟幕投放策略优化",
                "generated_at": 1784995200,
            }, ensure_ascii=False))
            delivery = server._paper_delivery(paper_dir.parent)
            assert delivery["pdf_name"].startswith("无人机烟幕投放策略优化_")
            assert delivery["pdf_name"].endswith(".pdf")
        finally:
            server.WORKSPACE = original_workspace

    print("dashboard conversation checks: passed")


if __name__ == "__main__":
    main()
