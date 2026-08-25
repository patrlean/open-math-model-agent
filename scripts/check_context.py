"""Deterministic self-test for the Task 4 context-management mechanics (no network):
  - plan_write / set_task_status / log_decision write files
  - lead working memory re-surfaces the todo plan + decisions + results index
  - verifier working memory excludes plan and decision-log content
  - compaction collapses old turns while keeping tool_call/tool pairing intact

Run:  ./.venv/bin/python -m scripts.check_context
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from mathmodel.agent.loop import Agent
from mathmodel.providers.base import ChatResponse, Provider, Usage
from mathmodel.tools.base import ToolContext, ToolRegistry
from mathmodel.tools.plan import log_decision_tool, plan_write_tool, set_task_status_tool


class FakeProvider(Provider):
    """Returns a canned summary; records that it was called."""

    def __init__(self) -> None:
        super().__init__(model="fake")
        self.calls = 0

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        self.calls += 1
        return ChatResponse(text="SUMMARY: attempted A, ruled out B (too slow), kept C.",
                            tool_calls=[], usage=Usage(10, 10, 20))


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        ctx = ToolContext(workdir=wd, sandbox=None)  # sandbox unused here

        # 1) todo plan + decision tools write files
        plan_write_tool.handler(ctx, {"tasks": [
            {"id": "q1", "title": "read data"},
            {"id": "q2", "title": "fit model"},
        ]})
        set_task_status_tool.handler(ctx, {"id": "q1", "status": "done", "result": "loaded 50 rows"})
        log_decision_tool.handler(ctx, {"what": "ruled out exact MILP", "why": ">2h on full instance"})
        # Keep enough subsequent entries to push the first decision beyond the
        # old 20-line tail. Working memory must preserve the complete log.
        for index in range(25):
            log_decision_tool.handler(ctx, {
                "what": f"later decision {index}",
                "why": f"evidence {index}",
            })
        assert (wd / "plan.json").exists() and (wd / "plan.md").exists() and (wd / "decisions.md").exists()
        (wd / "results").mkdir()
        (wd / "results" / "fit.json").write_text(json.dumps({"a": 1}))
        print("[1] plan.json / plan.md / decisions.md / results written OK")

        # 2) working memory surfaces them (done task + result + open task)
        agent = Agent(FakeProvider(), ToolRegistry(), ctx, "SYS",
                      compact_threshold_tokens=1, keep_tail_messages=6)
        agent._refresh_working_memory()
        wm = agent.messages[1]["content"]
        assert "fit model" in wm and "loaded 50 rows" in wm and "☑" in wm \
            and "ruled out exact MILP" in wm and "later decision 24" in wm \
            and "results/fit.json" in wm
        print("[2] working memory re-surfaces todo + complete decisions + results OK")

        # 3) Verification must judge artifacts independently: its pinned system
        # memory keeps artifact indexes but omits the author's plan and rationale.
        verifier = Agent(
            FakeProvider(),
            ToolRegistry(),
            ctx,
            "VERIFIER SYS",
            include_planning_memory=False,
        )
        verifier._refresh_working_memory()
        verifier_wm = verifier.messages[1]["content"]
        assert "## Plan" not in verifier_wm
        assert "fit model" not in verifier_wm
        assert "## Decisions" not in verifier_wm
        assert "ruled out exact MILP" not in verifier_wm
        assert "results/fit.json" in verifier_wm
        print("[3] verifier working memory excludes plan + decisions, keeps artifact indexes OK")

        # 4) compaction: build a long history with tool pairing, force compaction
        agent.messages = agent.messages[:2] + [{"role": "user", "content": "task"}]
        for i in range(8):
            agent.messages.append({"role": "assistant", "content": None,
                                   "tool_calls": [{"id": f"c{i}", "type": "function",
                                                   "function": {"name": "run_code", "arguments": "{}"}}]})
            agent.messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"obs {i}"})
        before = len(agent.messages)
        agent.context_tokens = 5  # current context size exceeds threshold=1
        agent._maybe_compact()
        after = len(agent.messages)

        assert after < before, "compaction did not shrink history"
        assert agent.messages[0]["role"] == "system" and agent.messages[1]["role"] == "system"
        assert "SUMMARY" in agent.messages[2]["content"], "summary not inserted"
        # tail must start at an assistant (never an orphan tool) and stay paired
        tail = agent.messages[3:]
        assert tail[0]["role"] == "assistant", "tail must start at assistant (paired)"
        # every tool message has a preceding assistant with matching id
        open_ids = set()
        for m in agent.messages:
            if m["role"] == "assistant":
                open_ids = {c["id"] for c in m.get("tool_calls", [])}
            elif m["role"] == "tool":
                assert m["tool_call_id"] in open_ids, "orphan tool message after compaction"
        print(f"[4] compaction {before}->{after} msgs, tool pairing intact, summary called={FakeProvider is not None}")

    print("\nOK: context-management mechanics pass.")


if __name__ == "__main__":
    main()
