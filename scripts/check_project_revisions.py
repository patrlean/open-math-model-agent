"""Regression checks for durable change confirmation and revision promotion."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from mathmodel.agent.loop import Agent
from mathmodel.dashboard.server import _revision_deliverables
from mathmodel.project_state import (
    ensure_project,
    mark_active_revision_completed,
    project_budget_settings,
    project_view,
    register_change_request,
    resolve_change_request,
    revision_change_confirmation_required,
    update_active_revision_status,
    update_project_budget_limit,
)
from mathmodel.providers.base import ChatResponse, Provider, ToolCall, Usage
from mathmodel.tools.ask_user import (
    claim_pending_answer,
    complete_claimed_answer,
    make_ask_user_tool,
)
from mathmodel.tools.base import Tool, ToolContext, ToolRegistry
from mathmodel.tools.ingest_problem import (
    make_ingest_problem_tool,
    make_promote_materials_tool,
)


class AskProvider(Provider):
    def __init__(self) -> None:
        super().__init__("test-model")
        self.calls = 0

    def chat(self, messages, tools=None, **kwargs):
        del tools, kwargs
        self.calls += 1
        if self.calls == 1:
            return ChatResponse(
                text="我先确认本次修订范围。",
                tool_calls=[ToolCall(
                    id="call_change_1",
                    name="ask_user",
                    arguments=json.dumps({
                        "kind": "change_confirmation",
                        "title": "修改风速并重新计算",
                        "summary": "将来流风速改为 12m/s，并同步更新结果与论文。",
                        "question": "是否按这个范围开始重算？",
                        "impacts": [
                            {
                                "target": "模型参数",
                                "change": "风速 10m/s → 12m/s",
                                "reason": "用户提出新的实验条件",
                            },
                            {
                                "target": "结果与论文",
                                "change": "重算并同步更新表格、图和结论",
                            },
                        ],
                        "budget": {
                            "currency": "CNY",
                            "max_additional_cost": 3,
                        },
                        "options": [
                            {"id": "confirm", "label": "确认重算"},
                            {"id": "adjust", "label": "调整要求"},
                            {"id": "cancel", "label": "取消"},
                        ],
                        "allow_custom": False,
                    }, ensure_ascii=False),
                )],
                usage=Usage(total_tokens=10),
            )
        assert any(
            message.get("role") == "tool"
            and message.get("tool_call_id") == "call_change_1"
            and "确认重算" in str(message.get("content"))
            for message in messages
        ), "resumed provider request did not contain the durable tool result"
        return ChatResponse(text="已按确认范围继续修订。", usage=Usage(total_tokens=5))


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        workdir = Path(temporary) / "project-a"
        workdir.mkdir()
        (workdir / "results").mkdir()
        (workdir / "results" / "q1.json").write_text('{"value": 1.385}')
        (workdir / "paper").mkdir()
        (workdir / "paper" / "main.pdf").write_bytes(b"%PDF stable-v1")
        (workdir / "src").mkdir()
        (workdir / "src" / "solver.py").write_text("ROUND = 1\n")
        ensure_project(workdir, title="烟幕干扰弹的投放策略")
        assert project_budget_settings(workdir)["revision_budget_limit_cny"] == 40
        update_project_budget_limit(workdir, 3)
        state_path = workdir / "session_state.json"
        events: list[tuple[str, dict]] = []
        provider = AskProvider()
        registry = ToolRegistry()
        registry.register(make_ask_user_tool(
            on_event=lambda kind, data: events.append((kind, data)),
        ))
        agent = Agent(
            provider,
            registry,
            ToolContext(workdir, sandbox=None),
            "system",
            max_steps=2,
            state_path=state_path,
            on_event=lambda kind, data: events.append((kind, data)),
        )
        result = agent.run("把风速改成 12m/s")
        assert result == "[waiting for user input]"
        assert agent.last_stop_reason == "waiting_input"
        pending = json.loads((workdir / "pending_question.json").read_text())
        assert pending["tool_call_id"] == "call_change_1"
        assert pending["kind"] == "change_confirmation"
        assert pending["budget"]["max_additional_cost"] == 3
        assert pending["budget"]["limit_source"] == "project_setting"

        # Simulate a full service restart: reconstruct the Agent exclusively
        # from its file checkpoint, without any in-memory Event or callback.
        checkpoint = json.loads(state_path.read_text())
        claimed = claim_pending_answer(
            workdir,
            pending["id"],
            option_id="confirm",
        )
        resolved = resolve_change_request(
            workdir,
            claimed["change_request_id"],
            action=claimed["result"]["change_action"],
            answer=claimed["result"]["answer"],
            selected_option_id=claimed["result"]["option_id"],
        )
        view = project_view(workdir)
        assert view["current_revision_id"] == "rev_0001"
        assert view["active_revision_id"] == "rev_0002"
        assert resolved["revision_id"] == "rev_0002"
        snapshot = workdir / "revisions" / "rev_0001" / "snapshot"
        assert (snapshot / "results" / "q1.json").read_text() == '{"value": 1.385}'
        assert (snapshot / "paper" / "main.pdf").read_bytes() == b"%PDF stable-v1"
        assert (snapshot / "src" / "solver.py").read_text() == "ROUND = 1\n"

        # The delivery API must expose the stable snapshot as V1 while the
        # mutable workspace is labeled as V2.
        (workdir / "paper" / "main.pdf").write_bytes(b"%PDF working-v2")
        (workdir / "src" / "solver.py").write_text("ROUND = 2\n")
        deliveries = _revision_deliverables(workdir, project_view(workdir))
        assert [item["revision_id"] for item in deliveries] == ["rev_0002", "rev_0001"]
        assert deliveries[0]["paper"]["pdf"] == "paper/main.pdf"
        assert deliveries[0]["source_files"][0]["path"] == "src/solver.py"
        assert deliveries[1]["paper"]["pdf"] == (
            "revisions/rev_0001/snapshot/paper/main.pdf"
        )
        assert deliveries[1]["source_files"][0]["path"] == (
            "revisions/rev_0001/snapshot/src/solver.py"
        )

        resumed = Agent(
            provider,
            registry,
            ToolContext(workdir, sandbox=None),
            "system",
            max_steps=1,
            initial_messages=checkpoint["messages"],
            state_path=state_path,
        )
        resumed.resume_tool_result(
            claimed["tool_call_id"],
            "[ask_user_result] " + json.dumps(claimed["result"], ensure_ascii=False),
        )
        complete_claimed_answer(workdir)
        assert resumed.run(None) == "已按确认范围继续修订。"
        calls_before_budget_stop = provider.calls
        resumed.total_usage.estimated_cost_cny = 3.0
        assert resumed.run(None) == "[stopped: revision cost budget exhausted]"
        assert provider.calls == calls_before_budget_stop

        # Completion alone does not replace the stable revision. Independent
        # verification is the promotion boundary.
        mark_active_revision_completed(workdir, verified=False)
        assert project_view(workdir)["current_revision_id"] == "rev_0001"
        mark_active_revision_completed(workdir, verified=True)
        final = project_view(workdir)
        assert final["current_revision_id"] == "rev_0002"
        assert final["current_revision"]["status"] == "verified"

        # A later conversational turn must not reopen the stable V2 in place.
        update_active_revision_status(workdir, "running")
        preserved = project_view(workdir)
        assert preserved["current_revision"]["status"] == "verified"
        assert revision_change_confirmation_required(workdir)
        mutation_calls: list[dict] = []
        registry.register(Tool(
            name="write_paper",
            description="test artifact mutation",
            parameters={"type": "object", "properties": {}},
            handler=lambda _ctx, args: mutation_calls.append(args) or "written",
        ))
        blocked = resumed._run_one_tool(ToolCall(
            id="call_blocked_write",
            name="write_paper",
            arguments="{}",
        ))
        assert blocked.startswith("[revision confirmation required]")
        assert not mutation_calls

    with tempfile.TemporaryDirectory() as temporary:
        workdir = Path(temporary) / "material-project"
        workdir.mkdir()
        (workdir / "problem.md").write_text("# Problem Materials\n\nOriginal problem.\n")
        supplement = workdir / "wind-note.txt"
        supplement.write_text("Use wind speed 12 m/s in the revised experiment.")
        ensure_project(workdir, title="Material staging")
        ctx = ToolContext(workdir, sandbox=None)
        stage_tool = make_ingest_problem_tool(
            problem_text="Please incorporate the attached experiment note.",
            upload_paths=[supplement],
        )
        staged_result = stage_tool.handler(ctx, {})
        staging_id = next(
            token.rstrip(";") for token in staged_result.split()
            if token.startswith("mat_")
        )
        assert (workdir / "problem.md").read_text().endswith("Original problem.\n")
        assert (
            workdir / ".pending_materials" / staging_id / "problem.md"
        ).is_file()

        promote_tool = make_promote_materials_tool()
        rejected = promote_tool.handler(ctx, {"staging_id": staging_id})
        assert rejected.startswith("[error] no confirmed draft revision")
        request = register_change_request(
            workdir,
            title="Adopt new wind experiment",
            summary="Promote staged experiment material and recompute.",
            impacts=[{"target": "model", "change": "wind speed to 12 m/s"}],
            budget={"currency": "CNY", "max_additional_cost": 2},
        )
        resolve_change_request(
            workdir,
            request["id"],
            action="confirm",
            answer="确认重算",
            selected_option_id="confirm",
        )
        promoted = promote_tool.handler(ctx, {"staging_id": staging_id})
        assert promoted.startswith(f"Promoted {staging_id} into rev_0002")
        canonical = (workdir / "problem.md").read_text()
        assert "Supplemental materials promoted in rev_0002" in canonical
        assert "wind speed 12 m/s" in canonical
        assert "already promoted" in promote_tool.handler(
            ctx,
            {"staging_id": staging_id},
        )

    print("project revision checks: passed")


if __name__ == "__main__":
    main()
