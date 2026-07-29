"""Deterministic checks for the independent final-answer verification gate.

No network or model API is used.

Run:
    ./.venv/bin/python -m scripts.check_verification_gate
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
from pathlib import Path

import fitz
import mathmodel.agent.verifier as verifier_module
from mathmodel.agent.loop import Agent, load_agent_state
from mathmodel.agent.verifier import _preflight
from mathmodel.latex.render import render_latex_fragment
from mathmodel.providers.base import ChatResponse, Provider, ToolCall
from mathmodel.sandbox.local import LocalSandbox
from mathmodel.tools.base import Tool, ToolContext, ToolRegistry


class CandidateProvider(Provider):
    def __init__(self, answers: list[str]) -> None:
        super().__init__(model="fake")
        self.answers = iter(answers)

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        return ChatResponse(text=next(self.answers))


class WritePaperAvailabilityProvider(Provider):
    def __init__(self) -> None:
        super().__init__(model="fake-rewrite-circuit")
        self.calls = 0

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        self.calls += 1
        assert messages[0]["content"] == "current write_paper policy"
        names = {
            item.get("function", {}).get("name") for item in (tools or [])
        }
        assert "write_paper" in names
        if self.calls <= 2:
            return ChatResponse(
                text="",
                tool_calls=[ToolCall(
                    id=f"rewrite-{self.calls}",
                    name="write_paper",
                    arguments=json.dumps({
                        "title": "test",
                        "sections": [{"heading": "model", "body": "body"}],
                    }),
                )],
            )
        return ChatResponse(text="write_paper remained available after both errors.")


class DeadlineVerifierProvider(Provider):
    """Spend the inspection budget, ignore one deadline, then submit."""

    def __init__(self) -> None:
        super().__init__(model="fake-deadline-verifier")
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        tool_names = [
            item.get("function", {}).get("name")
            for item in (tools or [])
        ]
        self.calls.append({
            "tool_names": tool_names,
            "tool_choice": kwargs.get("tool_choice"),
            "messages": list(messages),
        })
        call_number = len(self.calls)
        if call_number <= 2:
            return ChatResponse(
                text="Continue inspecting.",
                tool_calls=[ToolCall(
                    id=f"inspect-{call_number}",
                    name="read_file",
                    arguments=json.dumps({"path": "problem.md"}),
                )],
            )
        if call_number == 3:
            # Simulate a provider/model that ignores the first forced tool choice.
            return ChatResponse(text="The evidence appears sufficient.")
        return ChatResponse(
            text="",
            tool_calls=[ToolCall(
                id="deadline-submit",
                name="submit_verification",
                arguments=json.dumps({
                    "verdict": "PASS",
                    "summary": "The reserved deadline step produced a verdict.",
                    "issues": [],
                }),
            )],
        )


class IsolatedRecoveryProvider(Provider):
    """Ignore in-context deadlines but obey a clean submission-only request."""

    def __init__(self, recover: bool = True) -> None:
        super().__init__(model="fake-isolated-recovery")
        self.recover = recover
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        tool_names = [
            item.get("function", {}).get("name")
            for item in (tools or [])
        ]
        isolated = any(
            message.get("role") == "system"
            and "isolated scoped-verifier FINALIZATION" in str(
                message.get("content", "")
            )
            for message in messages
        )
        self.calls.append({
            "tool_names": tool_names,
            "tool_choice": kwargs.get("tool_choice"),
            "isolated": isolated,
        })
        if isolated and self.recover:
            return ChatResponse(
                text="",
                tool_calls=[ToolCall(
                    id="isolated-fragment",
                    name="submit_verification_fragment",
                    arguments=json.dumps({
                        "verdict": "REVISE",
                        "summary": (
                            "Independent recomputation retained a material "
                            "numerical discrepancy."
                        ),
                        "checks_performed": [
                            "Recomputed the high-impact waiting-time claim.",
                        ],
                        "issues": [issue(
                            "Reported 3.2323 does not match recomputed 3.1818."
                        )],
                    }),
                )],
            )
        return ChatResponse(
            text=(
                "Independent recomputation found that reported 3.2323 does not "
                "match recomputed 3.1818; this is a material discrepancy."
            ),
            tool_calls=[ToolCall(
                id=f"ignored-deadline-{len(self.calls)}",
                name="run_code",
                arguments=json.dumps({"code": "print('recomputed 3.1818')"}),
            )],
        )


class ParallelVerifierProvider(Provider):
    """Role-aware fake provider that records concurrent scoped calls."""

    def __init__(
        self,
        failing_scope: str | None = None,
        *,
        force_synthesis_pass: bool = False,
    ) -> None:
        super().__init__(model="fake-parallel-verifier")
        self.failing_scope = failing_scope
        self.force_synthesis_pass = force_synthesis_pass
        self.lock = threading.Lock()
        self.active_scopes = 0
        self.max_active_scopes = 0
        self.plan_calls = 0
        self.scope_calls = 0
        self.synthesis_calls = 0

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        tool_names = {
            item.get("function", {}).get("name")
            for item in (tools or [])
        }
        if "submit_verification_plan" in tool_names:
            self.plan_calls += 1
            return ChatResponse(
                text="",
                tool_calls=[ToolCall(
                    id="parallel-plan",
                    name="submit_verification_plan",
                    arguments=json.dumps({
                        "scopes": [
                            {
                                "id": "model",
                                "title": "Model",
                                "instructions": "Check equations and assumptions.",
                                "rationale": "Mandatory formulation coverage.",
                            },
                            {
                                "id": "numerical",
                                "title": "Numerical",
                                "instructions": "Recompute key numerical claims.",
                                "rationale": "High-impact values require reproduction.",
                            },
                            {
                                "id": "consistency",
                                "title": "Consistency",
                                "instructions": "Compare artifacts and response.",
                                "rationale": "Detect cross-artifact drift.",
                            },
                            {
                                "id": "paper",
                                "title": "Paper",
                                "instructions": "Check paper quality and layout.",
                                "rationale": "The PDF is a required deliverable.",
                            },
                        ],
                    }),
                )],
            )
        if "submit_verification_fragment" in tool_names:
            task = next(
                str(message.get("content", ""))
                for message in reversed(messages)
                if message.get("role") == "user"
            )
            scope_id = next(
                line.split(":", 1)[1].strip()
                for line in task.splitlines()
                if line.startswith("Scope id:")
            )
            with self.lock:
                self.scope_calls += 1
                self.active_scopes += 1
                self.max_active_scopes = max(
                    self.max_active_scopes, self.active_scopes
                )
            time.sleep(0.08)
            with self.lock:
                self.active_scopes -= 1
            failing = scope_id == self.failing_scope
            issues = [issue("A scoped numerical result is inconsistent.")] if failing else []
            checklist = [
                f"{check_id} PASS: Checked {description}"
                for check_id, description
                in verifier_module._FIXED_SCOPE_CHECKLISTS.get(scope_id, [])
            ]
            checklist.extend(
                f"LEDGER-{issue_id} PASS: The current candidate no longer contains "
                "the prior defect."
                for issue_id in dict.fromkeys(
                    re.findall(r"LEDGER-([0-9a-f]{12})", task)
                )
            )
            return ChatResponse(
                text="",
                tool_calls=[ToolCall(
                    id=f"parallel-fragment-{scope_id}",
                    name="submit_verification_fragment",
                    arguments=json.dumps({
                        "verdict": "REVISE" if failing else "PASS",
                        "summary": (
                            "A material scoped defect was reproduced."
                            if failing else "Assigned checks passed."
                        ),
                        "checks_performed": checklist,
                        "issues": issues,
                    }),
                )],
            )
        if "submit_verification" in tool_names:
            self.synthesis_calls += 1
            revise = bool(self.failing_scope) and not self.force_synthesis_pass
            return ChatResponse(
                text="",
                tool_calls=[ToolCall(
                    id="parallel-synthesis",
                    name="submit_verification",
                    arguments=json.dumps({
                        "verdict": "REVISE" if revise else "PASS",
                        "summary": (
                            "The coordinator retained the scoped defect."
                            if revise
                            else "The coordinator synthesized all scoped reports."
                        ),
                        "issues": (
                            [issue("A scoped numerical result is inconsistent.")]
                            if revise else []
                        ),
                    }),
                )],
            )
        raise AssertionError(f"Unexpected verifier tool set: {tool_names}")


def issue(message: str) -> dict:
    return {
        "severity": "major",
        "category": "numerical-check",
        "message": message,
        "evidence": "Independent recomputation disagreed.",
        "required_fix": "Correct the model and regenerate the result artifacts.",
    }


def check_revise_then_pass(workdir: Path) -> None:
    events: list[tuple[str, dict]] = []
    candidates: list[str] = []

    def verifier(candidate: str, attempt: int) -> dict:
        candidates.append(candidate)
        if attempt == 1:
            return {
                "verdict": "REVISE",
                "summary": "The first candidate has a material numerical error.",
                "issues": [issue("Objective value is inconsistent.")],
                "attempt": attempt,
            }
        return {
            "verdict": "PASS",
            "summary": "Independent checks reproduce the corrected result.",
            "issues": [],
            "attempt": attempt,
        }

    agent = Agent(
        provider=CandidateProvider(["unverified answer", "verified answer"]),
        registry=ToolRegistry(),
        ctx=ToolContext(workdir=workdir, sandbox=None),
        system_prompt="test",
        max_steps=3,
        on_event=lambda kind, data: events.append((kind, data)),
        final_verifier=verifier,
        max_verification_attempts=3,
    )
    result = agent.run("solve")
    assert result == "verified answer"
    assert candidates == ["unverified answer", "verified answer"]
    assert agent.last_stop_reason == "done"
    assert [kind for kind, _ in events].count("verification_start") == 2
    assert [kind for kind, _ in events].count("verification_result") == 2
    assert [kind for kind, _ in events].count("done") == 1
    assert not any(kind == "assistant" for kind, _ in events), (
        "Candidate final answers must not be shown before verification."
    )


def check_attempt_number_survives_resume(workdir: Path) -> None:
    events: list[tuple[str, dict]] = []
    attempts: list[int] = []
    state_path = workdir / "session_state.json"

    def verifier(candidate: str, attempt: int) -> dict:
        attempts.append(attempt)
        return {
            "verdict": "REVISE" if attempt == 1 else "PASS",
            "summary": f"Verification attempt {attempt}.",
            "issues": [issue("One repair is required.")] if attempt == 1 else [],
            "attempt": attempt,
        }

    first = Agent(
        provider=CandidateProvider(["candidate one"]),
        registry=ToolRegistry(),
        ctx=ToolContext(workdir=workdir, sandbox=None),
        system_prompt="test",
        max_steps=1,
        on_event=lambda kind, data: events.append((kind, data)),
        final_verifier=verifier,
        max_verification_attempts=3,
        state_path=state_path,
    )
    assert first.run("solve") == "[stopped: reached max_steps]"
    persisted = load_agent_state(state_path)
    assert persisted is not None
    assert persisted["runtime_controls"]["verification_attempts_completed"] == 1

    second_events: list[tuple[str, dict]] = []
    second = Agent(
        provider=CandidateProvider(["candidate two"]),
        registry=ToolRegistry(),
        ctx=ToolContext(workdir=workdir, sandbox=None),
        system_prompt="test",
        max_steps=1,
        on_event=lambda kind, data: second_events.append((kind, data)),
        final_verifier=verifier,
        max_verification_attempts=3,
        state_path=state_path,
        initial_messages=persisted["messages"],
        initial_runtime_controls=persisted["runtime_controls"],
    )
    assert second.run("continue") == "candidate two"
    assert attempts == [1, 2]
    assert [
        data["attempt"] for kind, data in second_events
        if kind == "verification_start"
    ] == [2]
    completed = load_agent_state(state_path)
    assert completed is not None
    assert completed["runtime_controls"]["verification_attempts_completed"] == 0


def check_repair_budget_is_separate(workdir: Path) -> None:
    events: list[tuple[str, dict]] = []

    def verifier(candidate: str, attempt: int) -> dict:
        if attempt == 1:
            return {
                "verdict": "REVISE",
                "summary": "One repair is required.",
                "issues": [issue("Repair the candidate once.")],
                "attempt": attempt,
            }
        return {
            "verdict": "PASS",
            "summary": "The repair passed.",
            "issues": [],
            "attempt": attempt,
        }

    agent = Agent(
        provider=CandidateProvider(["candidate", "repaired candidate"]),
        registry=ToolRegistry(),
        ctx=ToolContext(workdir=workdir, sandbox=None),
        system_prompt="test",
        max_steps=1,
        repair_steps_per_verification=1,
        on_event=lambda kind, data: events.append((kind, data)),
        final_verifier=verifier,
    )
    assert agent.run("solve") == "repaired candidate"
    budgets = [
        data for kind, data in events
        if kind == "verification_repair_budget"
    ]
    assert budgets == [{
        "step": 1,
        "attempt": 1,
        "added_steps": 1,
        "previous_limit": 1,
        "new_limit": 2,
    }]
    assert not any(kind == "max_steps" for kind, _ in events)


def check_write_paper_remains_available(workdir: Path) -> None:
    registry = ToolRegistry()
    registry.register(Tool(
        name="write_paper",
        description="test full rewrite",
        parameters={"type": "object"},
        handler=lambda _ctx, _args: "compile FAILED. malformed LaTeX",
    ))
    provider = WritePaperAvailabilityProvider()
    state_path = workdir / "session_state.json"
    agent = Agent(
        provider=provider,
        registry=registry,
        ctx=ToolContext(workdir=workdir, sandbox=None),
        system_prompt="current write_paper policy",
        max_steps=3,
        state_path=state_path,
        initial_messages=[
            {
                "role": "system",
                "content": (
                    "retired policy: disable write_paper and ask the user to "
                    "re-enable it"
                ),
            },
            {"role": "system", "content": "old working memory"},
        ],
        initial_runtime_controls={
            # Old checkpoints can still contain these retired fields. They must
            # have no effect and disappear on the next save.
            "write_paper_failures": 99,
            "write_paper_locked": True,
        },
    )
    assert agent.run("write") == "write_paper remained available after both errors."
    assert provider.calls == 3
    persisted = load_agent_state(state_path)
    assert persisted is not None
    controls = persisted["runtime_controls"]
    assert "write_paper_failures" not in controls
    assert "write_paper_locked" not in controls


def check_semantic_issue_deduplication() -> None:
    duplicate_model = verifier_module.VerificationIssue(
        "critical",
        "paper-structure",
        "The Model Evaluation section (模型评价、改进与推广) is duplicated.",
        "The four subsections occur twice in paper/main.tex.",
        "Remove the second copy.",
    )
    duplicate_model_reworded = verifier_module.VerificationIssue(
        "major",
        "structural-defect",
        "Section 模型评价、改进与推广 is repeated with two copies of 模型优点 and 模型缺点.",
        "A detailed version is followed by an abbreviated version.",
        "Keep the detailed copy only.",
    )
    numerical = verifier_module.VerificationIssue(
        "major",
        "numerical-inconsistency",
        "Arrival scaling data cannot be independently reproduced.",
        "q3_sensitivity.json differs from a clean recomputation.",
        "Regenerate the sensitivity sweep.",
    )
    deduplicated = verifier_module._deduplicate_issues([
        duplicate_model,
        duplicate_model_reworded,
        numerical,
    ])
    assert len(deduplicated) == 2
    assert deduplicated[0].severity == "critical"
    assert "abbreviated version" in deduplicated[0].evidence
    assert (
        verifier_module._issue_fingerprint(duplicate_model)
        == verifier_module._issue_fingerprint(duplicate_model_reworded)
    )

    result_heading_one = verifier_module.VerificationIssue(
        "critical",
        "ART-TABLES",
        "The 结果与分析 block is duplicated and repeats tab:det_results.",
        "The same result block occurs three times.",
        "Keep one complete result block.",
    )
    result_heading_two = verifier_module.VerificationIssue(
        "major",
        "MODEL-DERIVATION",
        "Repeated 结果与分析 sections redefine central tables and figures.",
        "Three copies were found in main.tex.",
        "Remove both stale copies.",
    )
    assert len(verifier_module._deduplicate_issues([
        result_heading_one,
        result_heading_two,
    ])) == 1, "Cross-scope category wording must not duplicate one structural defect."


def check_programmatic_checklist_completion() -> None:
    scope = verifier_module._default_scopes()[0]
    fragment = verifier_module.VerificationFragment(
        scope_id=scope.id,
        scope_title=scope.title,
        verdict="REVISE",
        summary="Readable finding, but the model omitted checklist prefixes.",
        checks_performed=["Inspected equations and found a dimensional mismatch."],
        issues=[verifier_module.VerificationIssue(
            "major",
            "model",
            "One governing equation is dimensionally inconsistent.",
            "The left side is time and the right side is dimensionless.",
            "Correct the equation and rerun the model.",
        )],
    )
    verifier_module._ensure_fragment_checklist(fragment, scope, [])
    required = {
        check_id
        for check_id, _ in verifier_module._FIXED_SCOPE_CHECKLISTS[scope.id]
    }
    recorded = {
        line.split()[0]
        for line in fragment.checks_performed
        if " FAIL:" in line or " PASS:" in line
    }
    assert required <= recorded
    assert fragment.verdict == "REVISE"


def check_retry_limit_delivers_after_final_repair(workdir: Path) -> None:
    events: list[tuple[str, dict]] = []
    attempts: list[int] = []

    def verifier(candidate: str, attempt: int) -> dict:
        attempts.append(attempt)
        return {
            "verdict": "REVISE",
            "summary": "Still not reproducible.",
            "issues": [issue("Required evidence remains missing.")],
            "attempt": attempt,
        }

    agent = Agent(
        provider=CandidateProvider([
            "candidate one",
            "candidate two",
            "candidate three",
            "final paper delivery",
        ]),
        registry=ToolRegistry(),
        ctx=ToolContext(workdir=workdir, sandbox=None),
        system_prompt="test",
        max_steps=4,
        on_event=lambda kind, data: events.append((kind, data)),
        final_verifier=verifier,
        max_verification_attempts=3,
    )
    result = agent.run("solve")
    assert result == "final paper delivery"
    assert attempts == [1, 2, 3]
    assert agent.last_stop_reason == "done"
    assert any(kind == "verification_exhausted" for kind, _ in events)
    assert any(kind == "verification_override_delivery" for kind, _ in events)
    assert any(kind == "done" for kind, _ in events)
    assert not any(kind == "verification_failed" for kind, _ in events)
    assert any(
        "there will be no further verification attempt"
        in str(message.get("content", ""))
        for message in agent.messages
    )


def build_valid_artifacts(workdir: Path) -> None:
    (workdir / "problem.md").write_text("A complete modeling problem.")
    (workdir / "results").mkdir(exist_ok=True)
    (workdir / "results" / "answer.json").write_text(json.dumps({"value": 42}))
    (workdir / "logs").mkdir(exist_ok=True)
    (workdir / "logs" / "run_1.log").write_text(
        "=== source ===\nprint(42)\n--- exit_code=0 timed_out=False stopped=False ---"
    )
    (workdir / "paper").mkdir(exist_ok=True)
    equations = "\n".join(r"\[x=1\]" for _ in range(12))
    (workdir / "paper" / "main.tex").write_text(
        "\\documentclass{ctexart}\n"
        "\\begin{document}\n"
        "\\begin{abstract}\n"
        + ("模型验证结果可靠。" * 120)
        + "\n\\end{abstract}\n\\clearpage\n"
        "\\section{Final model}\n"
        + equations
        + "\n\\end{document}\n"
    )
    pdf = fitz.open()
    first = pdf.new_page(width=595, height=842)
    first.insert_text((50, 80), "Competition paper abstract")
    first.insert_text((50, 740), "Keywords: model, validation, optimization")
    second = pdf.new_page(width=595, height=842)
    second.insert_text((50, 80), "1 Final model")
    for page_number in range(3, 21):
        page = pdf.new_page(width=595, height=842)
        page.insert_text((50, 80), f"Substantive paper content page {page_number}")
    pdf.save(workdir / "paper" / "main.pdf")
    pdf.close()
    (workdir / "plan.json").write_text(json.dumps({
        "tasks": [{"id": "q1", "title": "solve", "status": "done"}],
    }))


def check_preflight(workdir: Path) -> None:
    build_valid_artifacts(workdir)
    assert _preflight(workdir) == []

    tex_path = workdir / "paper" / "main.tex"
    stable_tex = tex_path.read_text()
    tex_path.write_text(stable_tex.replace(
        "\\end{document}",
        "The success rate is 95% but this text would be truncated.\n"
        "\\end{document}",
    ))
    percent_issues = _preflight(workdir)
    assert any(
        item.severity == "critical"
        and "percent" in item.message.lower()
        for item in percent_issues
    )
    tex_path.write_text(stable_tex)
    rendered = render_latex_fragment(
        "% preserved source comment\nThe success rate is 95%.\nAlready 90\\%.",
        workdir,
    )
    assert rendered.startswith("% preserved source comment")
    assert "95\\%" in rendered
    assert "90\\%" in rendered

    original_pdf = workdir / "paper" / "main.pdf"
    shortened_pdf = workdir / "paper" / "short.pdf"
    doc = fitz.open(original_pdf)
    doc.select(list(range(19)))
    doc.save(shortened_pdf)
    doc.close()
    shortened_pdf.replace(original_pdf)
    assert "paper-length" not in {
        item.category for item in _preflight(workdir)
    }, "19-page papers should be accepted"

    accepted_minimum_pdf = workdir / "paper" / "accepted-minimum.pdf"
    doc = fitz.open(original_pdf)
    doc.select(list(range(17)))
    doc.save(accepted_minimum_pdf)
    doc.close()
    accepted_minimum_pdf.replace(original_pdf)
    assert "paper-length" not in {
        item.category for item in _preflight(workdir)
    }, "17-page papers should be accepted"

    too_short_pdf = workdir / "paper" / "too-short.pdf"
    doc = fitz.open(original_pdf)
    doc.select(list(range(16)))
    doc.save(too_short_pdf)
    doc.close()
    too_short_pdf.replace(original_pdf)
    (workdir / "paper" / "main.tex").write_text(
        "\\documentclass{ctexart}\n\\begin{document}\n"
        "\\begin{abstract}摘要太短。\\end{abstract}\n\\clearpage\n"
        "\\section{Final model}\n\\[x=1\\]\n\\end{document}\n"
    )
    (workdir / "logs" / "run_1.log").write_text(
        "=== source ===\n"
        "import matplotlib.pyplot as plt\n"
        "plt.xlabel('时间')\n"
        "--- exit_code=0 timed_out=False stopped=False ---"
    )
    quality_issues = _preflight(workdir)
    quality_categories = {item.category for item in quality_issues}
    assert {
        "paper-length", "abstract-content", "model-formulation", "figure-language",
    } <= quality_categories

    (workdir / "plan.json").write_text(json.dumps({
        "tasks": [{"id": "q1", "title": "solve", "status": "pending"}],
    }))
    (workdir / "paper" / "main.tex").write_text(r"\section{2.5 2.5 Final model}")
    issues = _preflight(workdir)
    categories = {item.category for item in issues}
    assert "coverage" in categories
    assert "paper-format" in categories

    (workdir / "paper" / "main.tex").write_text(
        "\\section{结果与分析}\\label{tab:det_results}\n"
        "\\section{结果与分析}\\label{tab:det_results}\n"
    )
    structural = _preflight(workdir)
    assert sum(item.severity == "critical" for item in structural) >= 2
    assert any("labels are duplicated" in item.message for item in structural)
    assert any("headings are repeated" in item.message for item in structural)


def check_critical_preflight_short_circuit(workdir: Path) -> None:
    build_valid_artifacts(workdir)
    tex_path = workdir / "paper" / "main.tex"
    tex_path.write_text(
        tex_path.read_text()
        .replace(
            "\\section{Final model}",
            "\\section{Final model}\\label{eq:duplicate}",
        )
        .replace(
            "\\end{document}",
            "\\section{Final model}\\label{eq:duplicate}\\end{document}",
        )
    )
    calls = 0
    original_provider = verifier_module.build_provider

    def fail_if_called(_cfg):
        nonlocal calls
        calls += 1
        raise AssertionError("Critical deterministic failures must skip model calls.")

    progress: list[tuple[str, dict]] = []
    try:
        verifier_module.build_provider = fail_if_called
        report = verifier_module.verify_candidate(
            cfg={
                "provider": "fake",
                "model": "fake",
                "context": {"compact_threshold_tokens": 100_000},
                "verification": {"max_steps": 128, "provider_config": {}},
                "sandbox": "local",
            },
            workdir=workdir,
            candidate="Structurally invalid candidate.",
            attempt=1,
            stop_event=threading.Event(),
            on_event=lambda kind, data: progress.append((kind, data)),
        )
    finally:
        verifier_module.build_provider = original_provider

    assert calls == 0
    assert report.verdict == "REVISE"
    payload = json.loads(
        (workdir / "verification" / "report.json").read_text()
    )
    assert payload["scope_reports"] == []
    assert payload["verification_usage"]["model_verification_skipped"] is True
    assert any(
        kind == "verification_progress"
        and data.get("phase") == "preflight_blocked"
        for kind, data in progress
    )


def check_isolated_verifier(workdir: Path) -> None:
    build_valid_artifacts(workdir)

    original_provider = verifier_module.build_provider
    original_sandbox = verifier_module.build_sandbox
    provider = ParallelVerifierProvider()
    progress: list[tuple[str, dict]] = []
    try:
        verifier_module.build_provider = lambda cfg: provider
        verifier_module.build_sandbox = lambda cfg, wd: LocalSandbox(wd)
        report = verifier_module.verify_candidate(
            cfg={
                "provider": "fake",
                "model": "fake",
                "context": {"compact_threshold_tokens": 100_000},
                "verification": {"max_steps": 3, "provider_config": {}},
                "sandbox": "local",
            },
            workdir=workdir,
            candidate="The verified value is 42.",
            attempt=1,
            stop_event=threading.Event(),
            on_event=lambda kind, data: progress.append((kind, data)),
        )
    finally:
        verifier_module.build_provider = original_provider
        verifier_module.build_sandbox = original_sandbox

    assert report.verdict == "PASS"
    assert (workdir / "verification" / "report.json").is_file()
    assert any(kind == "verification_progress" for kind, _ in progress)
    phases = {
        data.get("phase")
        for kind, data in progress
        if kind == "verification_progress"
    }
    assert {
        "single_check_start", "single_check_complete",
    } <= phases
    assert provider.plan_calls == 0
    assert provider.scope_calls == 0
    assert provider.synthesis_calls == 1
    payload = json.loads(
        (workdir / "verification" / "report.json").read_text()
    )
    assert [item["id"] for item in payload["verification_plan"]] == [
        "single-verifier"
    ]
    assert payload["scope_reports"] == []


def check_synthesis_pass_is_terminal(workdir: Path) -> None:
    build_valid_artifacts(workdir)

    original_provider = verifier_module.build_provider
    original_sandbox = verifier_module.build_sandbox
    provider = ParallelVerifierProvider(
        failing_scope="numerical-reproduction",
        force_synthesis_pass=True,
    )
    try:
        verifier_module.build_provider = lambda cfg: provider
        verifier_module.build_sandbox = lambda cfg, wd: LocalSandbox(wd)
        report = verifier_module.verify_candidate(
            cfg={
                "provider": "fake",
                "model": "fake",
                "context": {"compact_threshold_tokens": 100_000},
                "verification": {
                    "parallel_workers": 4,
                    "subagent_max_steps": 3,
                    "provider_config": {},
                },
                "sandbox": "local",
            },
            workdir=workdir,
            candidate="The verified value is 42.",
            attempt=1,
            stop_event=threading.Event(),
        )
    finally:
        verifier_module.build_provider = original_provider
        verifier_module.build_sandbox = original_sandbox

    assert provider.synthesis_calls == 1
    assert report.verdict == "PASS"
    assert report.issues == [], (
        "A structured synthesis PASS must not be expanded with raw subagent "
        "findings after the lead verifier has reconciled them."
    )


def check_cross_round_ledger_and_diff(workdir: Path) -> None:
    build_valid_artifacts(workdir)
    original_provider = verifier_module.build_provider
    original_sandbox = verifier_module.build_sandbox
    provider = ParallelVerifierProvider(
        failing_scope="numerical-reproduction"
    )
    try:
        verifier_module.build_provider = lambda cfg: provider
        verifier_module.build_sandbox = lambda cfg, wd: LocalSandbox(wd)
        first = verifier_module.verify_candidate(
            cfg={
                "provider": "fake",
                "model": "fake",
                "context": {"compact_threshold_tokens": 100_000},
                "verification": {
                    "max_steps": 20,
                    "parallel_workers": 4,
                    "provider_config": {},
                },
                "sandbox": "local",
            },
            workdir=workdir,
            candidate="Candidate one.",
            attempt=1,
            stop_event=threading.Event(),
        )
        assert first.verdict == "REVISE"
        provider.failing_scope = None
        paper = workdir / "paper" / "main.tex"
        paper.write_text(paper.read_text() + "\n% revised candidate\n")
        second = verifier_module.verify_candidate(
            cfg={
                "provider": "fake",
                "model": "fake",
                "context": {"compact_threshold_tokens": 100_000},
                "verification": {
                    "max_steps": 20,
                    "parallel_workers": 4,
                    "provider_config": {},
                },
                "sandbox": "local",
            },
            workdir=workdir,
            candidate="Candidate two.",
            attempt=2,
            stop_event=threading.Event(),
        )
    finally:
        verifier_module.build_provider = original_provider
        verifier_module.build_sandbox = original_sandbox

    assert second.verdict == "PASS"
    ledger = json.loads(
        (workdir / "verification" / "issue_ledger.json").read_text()
    )
    assert ledger["entries"]
    assert all(item["status"] == "resolved" for item in ledger["entries"])
    diff = (
        workdir / "verification" / "candidate_diff_attempt_2.patch"
    ).read_text()
    assert "revised candidate" in diff
    payload = json.loads(
        (workdir / "verification" / "report_attempt_2.json").read_text()
    )
    assert [item["id"] for item in payload["verification_plan"]] == [
        "single-verifier"
    ]


def check_verifier_deadline_fallback(workdir: Path) -> None:
    build_valid_artifacts(workdir)

    provider = DeadlineVerifierProvider()
    progress: list[tuple[str, dict]] = []
    submitted: list[dict] = []
    registry = ToolRegistry()
    registry.register(verifier_module.read_file_tool)
    registry.register(verifier_module.run_code_tool)
    registry.register(Tool(
        name="submit_verification",
        description="Submit verdict.",
        parameters=verifier_module._SUBMIT_PARAMS,
        handler=lambda _ctx, args: (
            submitted.append(args) or "Verification verdict recorded."
        ),
    ))
    agent = Agent(
        provider=provider,
        registry=registry,
        ctx=ToolContext(
            workdir=workdir,
            sandbox=LocalSandbox(workdir),
            stop_event=threading.Event(),
        ),
        system_prompt="Verifier deadline test.",
        max_steps=4,
        on_event=lambda kind, data: progress.append((kind, data)),
        finalization_tool="submit_verification",
        finalization_steps=2,
    )
    agent.run("Verify the candidate.")

    assert submitted and submitted[-1]["verdict"] == "PASS"
    assert len(provider.calls) == 4
    assert "read_file" in provider.calls[0]["tool_names"]
    assert "run_code" in provider.calls[1]["tool_names"]
    for deadline_call in provider.calls[2:]:
        assert deadline_call["tool_names"] == ["submit_verification"]
        assert deadline_call["tool_choice"] == {
            "type": "function",
            "function": {"name": "submit_verification"},
        }
    phases = [kind for kind, _ in progress]
    assert "finalization_required" in phases
    assert "done" in phases
    assert "max_steps" not in phases


def check_isolated_scope_recovery(workdir: Path) -> None:
    build_valid_artifacts(workdir)
    provider = IsolatedRecoveryProvider(recover=True)
    progress: list[tuple[str, dict]] = []
    original_provider = verifier_module.build_provider
    original_sandbox = verifier_module.build_sandbox
    scope = verifier_module.VerificationScope(
        id="numerical-recovery",
        title="Numerical recovery",
        instructions="Recompute the key waiting-time claim.",
        rationale="The number controls the final conclusion.",
    )
    try:
        verifier_module.build_provider = lambda cfg: provider
        verifier_module.build_sandbox = lambda cfg, wd: LocalSandbox(wd)
        fragment = verifier_module._run_scope_verifier(
            cfg={
                "provider": "fake",
                "model": "fake",
                "context": {"compact_threshold_tokens": 100_000},
                "verification": {"provider_config": {}},
                "sandbox": "local",
            },
            source=workdir,
            destination=workdir / "isolated-worker",
            candidate="Candidate answer.",
            static_issues=[],
            scope=scope,
            prior_issues=[],
            candidate_diff="[initial candidate]",
            max_steps=4,
            stop_event=threading.Event(),
            emit=lambda kind, data: progress.append((kind, data)),
        )
    finally:
        verifier_module.build_provider = original_provider
        verifier_module.build_sandbox = original_sandbox

    assert fragment.verdict == "REVISE"
    assert "material numerical discrepancy" in fragment.summary
    assert any(call["isolated"] for call in provider.calls)
    recovery_calls = [call for call in provider.calls if call["isolated"]]
    assert recovery_calls[-1]["tool_names"] == [
        "submit_verification_fragment"
    ]
    assert recovery_calls[-1]["tool_choice"] == {
        "type": "function",
        "function": {"name": "submit_verification_fragment"},
    }
    phases = [
        data.get("phase")
        for kind, data in progress
        if kind == "verification_progress"
    ]
    assert "finalization_recovery_start" in phases
    assert "finalization_recovery_complete" in phases


def check_readable_scope_fallback(workdir: Path) -> None:
    build_valid_artifacts(workdir)
    provider = IsolatedRecoveryProvider(recover=False)
    progress: list[tuple[str, dict]] = []
    original_provider = verifier_module.build_provider
    original_sandbox = verifier_module.build_sandbox
    scope = verifier_module.VerificationScope(
        id="numerical-fallback",
        title="Numerical fallback",
        instructions="Recompute the key waiting-time claim.",
        rationale="The number controls the final conclusion.",
    )
    try:
        verifier_module.build_provider = lambda cfg: provider
        verifier_module.build_sandbox = lambda cfg, wd: LocalSandbox(wd)
        fragment = verifier_module._run_scope_verifier(
            cfg={
                "provider": "fake",
                "model": "fake",
                "context": {"compact_threshold_tokens": 100_000},
                "verification": {"provider_config": {}},
                "sandbox": "local",
            },
            source=workdir,
            destination=workdir / "fallback-worker",
            candidate="Candidate answer.",
            static_issues=[],
            scope=scope,
            prior_issues=[],
            candidate_diff="[initial candidate]",
            max_steps=4,
            stop_event=threading.Event(),
            emit=lambda kind, data: progress.append((kind, data)),
        )
    finally:
        verifier_module.build_provider = original_provider
        verifier_module.build_sandbox = original_sandbox

    assert fragment.verdict == "INCONCLUSIVE"
    assert "3.2323" in fragment.summary
    assert "3.1818" in fragment.summary
    assert fragment.checks_performed
    assert "No submit_verification_fragment" not in fragment.issues[0].evidence
    phases = [
        data.get("phase")
        for kind, data in progress
        if kind == "verification_progress"
    ]
    assert "finalization_recovery_failed" in phases


def check_prompt_override(workdir: Path) -> None:
    default = verifier_module.get_verifier_prompt(workdir)
    assert default["is_custom"] is False
    assert "严格、审慎、证据驱动" in default["content"]
    assert "submit_verification" in default["content"]
    assert "submit_verification exactly once" in default["runtime_contract"]

    verifier_prompt_path = verifier_module.verifier_prompt_path(workdir)
    verifier_prompt_path.parent.mkdir()
    verifier_prompt_path.write_text("A stale historical custom prompt.")
    assert verifier_module.get_verifier_prompt(workdir) == default

    for attempted in ("Custom verifier prompt.", None):
        try:
            verifier_module.save_verifier_prompt(workdir, attempted)
        except ValueError as exc:
            assert "系统锁定" in str(exc)
        else:
            raise AssertionError("Verifier prompt changes must be rejected")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        gate_one = root / "gate-one"
        gate_one.mkdir()
        check_revise_then_pass(gate_one)
        print("[1] REVISE feedback returns to lead; only PASS candidate is published")

        gate_two = root / "gate-two"
        gate_two.mkdir()
        check_retry_limit_delivers_after_final_repair(gate_two)
        print("[2] retry limit returns the last verdict for one final direct delivery")

        resume_attempt = root / "resume-attempt"
        resume_attempt.mkdir()
        check_attempt_number_survives_resume(resume_attempt)
        print("[2b] interrupted verification resumes at the next attempt number")

        repair_budget = root / "repair-budget"
        repair_budget.mkdir()
        check_repair_budget_is_separate(repair_budget)
        print("[3] verifier rejection adds a separate lead repair budget")

        write_paper_availability = root / "write-paper-availability"
        write_paper_availability.mkdir()
        check_write_paper_remains_available(write_paper_availability)
        print("[4] write_paper remains available after repeated failures")

        check_semantic_issue_deduplication()
        print("[5] overlapping scoped findings collapse into semantic issue families")

        check_programmatic_checklist_completion()
        print("[6] readable scope verdicts receive machine-readable checklist closure")

        preflight = root / "preflight"
        preflight.mkdir()
        check_preflight(preflight)
        print("[7] deterministic artifact, plan, and LaTeX checks enforced")

        preflight_block = root / "preflight-block"
        preflight_block.mkdir()
        check_critical_preflight_short_circuit(preflight_block)
        print("[8] critical deterministic defects skip expensive model verification")

        isolated = root / "isolated"
        isolated.mkdir()
        check_isolated_verifier(isolated)
        print("[9] one verifier independently reviews the complete workspace")

        scoped_revise = root / "scoped-revise"
        scoped_revise.mkdir()
        check_synthesis_pass_is_terminal(scoped_revise)
        print("[10] a structured verifier PASS is terminal")

        prompt = root / "prompt"
        prompt.mkdir()
        check_prompt_override(prompt)
        print("[11] verifier Prompt is system-owned and per-run changes are rejected")

        deadline = root / "deadline"
        deadline.mkdir()
        check_verifier_deadline_fallback(deadline)
        print("[12] final two steps force a structured verification verdict")

        isolated_recovery = root / "isolated-recovery"
        isolated_recovery.mkdir()
        check_isolated_scope_recovery(isolated_recovery)
        print("[13] failed in-context submission retries in a clean finalizer")

        readable_fallback = root / "readable-fallback"
        readable_fallback.mkdir()
        check_readable_scope_fallback(readable_fallback)
        print("[14] failed structured submission retains readable findings")

        ledger_diff = root / "ledger-diff"
        ledger_diff.mkdir()
        check_cross_round_ledger_and_diff(ledger_diff)
        print("[15] prior issues close explicitly and candidate diffs are audited")

    print("\nOK: independent verification gate passes.")


if __name__ == "__main__":
    main()
