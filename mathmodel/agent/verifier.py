"""Independent final-answer verification for the lead modeling Agent.

The verifier receives a clean context and a disposable copy of the run
artifacts. It may execute adversarial checks without mutating the lead's data.
Its structured PASS/REVISE verdict is enforced by Agent.run; it is not advisory.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import build_provider, build_sandbox
from ..latex.quality import (
    find_non_english_plot_labels_in_sources,
    inspect_paper,
    selected_page_count,
)
from ..latex.render import find_unescaped_percent_lines
from ..latex.structure import inspect_latex_structure
from ..paper_profile import PAPER_PROFILE_FILENAME, resolve_paper_config
from ..providers.base import model_request_context
from ..tools.base import Tool, ToolContext, ToolRegistry
from ..tools.read_file import read_file_tool
from ..tools.results_store import results_get_tool, results_list_tool
from ..tools.run_code import run_code_tool
from .loop import Agent
from .prompts import VERIFIER_RUNTIME_CONTRACT, VERIFIER_SYSTEM
from .artifact_manifest import MANIFEST_FILENAME, write_artifact_manifest

VERIFIER_PROMPT_FILENAME = "prompt_override.md"
VERIFIER_FINALIZATION_STEPS = 2
VERIFIER_RECOVERY_ATTEMPTS = 2
VERIFIER_TRACE_MAX_CHARS = 48_000
VERIFIER_TRACE_ITEM_MAX_CHARS = 2_400
DEFAULT_VERIFIER_WORKERS = 4
DEFAULT_VERIFIER_TRIAGE_STEPS = 2
DEFAULT_VERIFIER_SYNTHESIS_STEPS = 2

_FIXED_SCOPE_CHECKLISTS: dict[str, list[tuple[str, str]]] = {
    "model-formulation": [
        ("MODEL-ASSUMPTIONS", "Assumptions are mutually consistent and match the implemented model."),
        ("MODEL-DERIVATION", "Equations, units, constraints, boundaries, and derivations are auditable."),
        ("MODEL-VALIDATION", "Validation is non-circular and its claims match what the test can prove."),
        ("MODEL-SYMBOLS", "Symbols are defined, used consistently, and not orphaned."),
    ],
    "numerical-reproduction": [
        ("NUM-RECOMPUTE", "Independently recompute every requested sub-problem's high-impact values."),
        ("NUM-CODE-MODEL", "Confirm executable code implements the paper's stated model."),
        ("NUM-FEASIBILITY", "Check constraints, units, residuals, boundaries, and edge cases."),
        ("NUM-PRECISION", "Check exact values, numerical tolerances, and rounding propagation."),
    ],
    "cross-artifact-consistency": [
        ("ART-RESULTS", "Compare every material paper claim with final source and result artifacts."),
        ("ART-FIGURES", "Compare figures, tables, captions, and labels with source data."),
        ("ART-DELIVERABLES", "Confirm every problem request and deliverable is covered."),
        ("DIFF-DEPENDENCIES", "Inspect changed content and every dependent abstract, table, result, and conclusion."),
    ],
    "paper-quality": [
        ("PAPER-FORMAT", "Check pages, abstract, section placement, equations, references, and figures."),
        ("PAPER-WRITING", "Check analysis depth, formulation depth, clarity, and unsupported claims."),
        ("PAPER-LANGUAGE", "Check English-only generated figure labels and readable typography."),
        ("PAPER-CONSISTENCY", "Check abstract, body, conclusion, and terminology for internal consistency."),
    ],
    "global-contradictions": [
        ("GLOBAL-ABSTRACT-BODY", "Find contradictions between abstract, body, tables, and conclusion."),
        ("GLOBAL-ASSUMPTION-MODEL", "Find contradictions between assumptions, equations, code, and limitations."),
        ("GLOBAL-VALIDATION-CLAIMS", "Check whether each validation method supports the claim made for it."),
        ("GLOBAL-ISSUE-FAMILY", "Search all dependent locations for every discovered issue family."),
        ("DIFF-DEPENDENCIES", "Audit the full dependency surface of all changes since the previous candidate."),
    ],
}


@dataclass
class VerificationIssue:
    severity: str
    category: str
    message: str
    evidence: str
    required_fix: str


@dataclass
class VerificationReport:
    verdict: str
    summary: str
    issues: list[VerificationIssue]
    attempt: int
    verification_usage: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def feedback(self) -> str:
        lines = [
            f"Independent verifier verdict: {self.verdict}",
            f"Summary: {self.summary}",
        ]
        for index, issue in enumerate(self.issues, start=1):
            lines.extend([
                f"{index}. [{issue.severity}] {issue.category}: {issue.message}",
                f"   Evidence: {issue.evidence}",
                f"   Required fix: {issue.required_fix}",
            ])
        return "\n".join(lines)


@dataclass
class VerificationScope:
    id: str
    title: str
    instructions: str
    rationale: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class VerificationFragment:
    scope_id: str
    scope_title: str
    verdict: str
    summary: str
    checks_performed: list[str]
    issues: list[VerificationIssue]
    tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def verifier_prompt_path(workdir: Path) -> Path:
    return workdir / "verification" / VERIFIER_PROMPT_FILENAME


def get_verifier_prompt(workdir: Path) -> dict[str, Any]:
    """Return the system-owned verifier prompt.

    Historical per-run override files are intentionally ignored. Verification
    policy is now fixed in code so a hidden stale override cannot change the
    behavior of one conversation.
    """
    return {
        "content": VERIFIER_SYSTEM.strip(),
        "is_custom": False,
        "runtime_contract": VERIFIER_RUNTIME_CONTRACT.strip(),
    }


def save_verifier_prompt(workdir: Path, prompt: str | None) -> dict[str, Any]:
    """Reject runtime prompt changes; verification policy is system-owned."""
    del workdir, prompt
    raise ValueError("验证 Agent Prompt 已由系统锁定，不能按会话修改。")


def effective_verifier_system_prompt(workdir: Path) -> str:
    prompt = get_verifier_prompt(workdir)
    return f"{prompt['content'].rstrip()}\n\n{prompt['runtime_contract']}"


_SUBMIT_PARAMS = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["PASS", "REVISE"],
            "description": "PASS only when every material claim is adequately verified.",
        },
        "summary": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "major", "minor"],
                    },
                    "category": {"type": "string"},
                    "message": {"type": "string"},
                    "evidence": {"type": "string"},
                    "required_fix": {"type": "string"},
                },
                "required": [
                    "severity", "category", "message", "evidence", "required_fix",
                ],
            },
        },
    },
    "required": ["verdict", "summary", "issues"],
}

_PLAN_PARAMS = {
    "type": "object",
    "properties": {
        "scopes": {
            "type": "array",
            "minItems": 3,
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Short stable identifier, e.g. numerical-results.",
                    },
                    "title": {"type": "string"},
                    "instructions": {
                        "type": "string",
                        "description": (
                            "A narrow, evidence-oriented assignment for one verifier."
                        ),
                    },
                    "rationale": {
                        "type": "string",
                        "description": (
                            "Why this scope is mandatory or appears high-risk."
                        ),
                    },
                },
                "required": ["id", "title", "instructions", "rationale"],
            },
        },
    },
    "required": ["scopes"],
}

_FRAGMENT_PARAMS = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["PASS", "REVISE", "INCONCLUSIVE"],
        },
        "summary": {"type": "string"},
        "checks_performed": {
            "type": "array",
            "items": {"type": "string"},
        },
        "issues": _SUBMIT_PARAMS["properties"]["issues"],
    },
    "required": ["verdict", "summary", "checks_performed", "issues"],
}


def _issue_from_dict(item: dict[str, Any]) -> VerificationIssue:
    return VerificationIssue(
        severity=str(item.get("severity", "major")),
        category=str(item.get("category", "verification")),
        message=str(item.get("message", "Unspecified verification issue.")),
        evidence=str(item.get("evidence", "No evidence was supplied.")),
        required_fix=str(item.get("required_fix", "Resolve and verify this issue.")),
    )


_SEVERITY_RANK = {"minor": 1, "major": 2, "critical": 3}
_ISSUE_FILE = re.compile(
    r"(?i)(?:[a-z0-9_.-]+/)*[a-z0-9_.-]+\."
    r"(?:tex|json|csv|md|png|jpg|jpeg|pdf|log|py)"
)


def _canonical_issue_category(category: str) -> str:
    """Collapse cosmetic category wording without weakening the finding."""
    value = category.casefold()
    groups = {
        "paper-structure": (
            "structure", "structural", "layout", "paper-format",
            "paper-quality", "art-table", "art-figure",
        ),
        "numerical": (
            "numerical", "consistency", "code-bug", "reproduction",
            "data-integrity", "result",
        ),
        "documentation": ("documentation", "artifact"),
        "bibliography": ("bibliography", "reference", "citation"),
        "model": (
            "model", "equation", "assumption", "derivation", "formulation",
        ),
    }
    for canonical, markers in groups.items():
        if any(marker in value for marker in markers):
            return canonical
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-") or "verification"


def _normalised_issue_text(value: str) -> str:
    return re.sub(
        r"[^0-9a-z\u4e00-\u9fff]+",
        "",
        value.casefold().replace("\\", " "),
    )


def _issue_family_hint(issue: VerificationIssue) -> str | None:
    """Return a stable high-signal family for common cross-scope restatements.

    Scoped verifiers often describe the same defect with different severity
    labels, categories, line positions, and prose. These hints intentionally
    use the defect subject rather than those volatile details.
    """
    # Classify from the finding itself. Evidence and proposed fixes commonly
    # mention neighbouring checks (for example a binary search used to verify a
    # critical-arrival claim) and would make the family drift after merging.
    text = issue.message.casefold()
    compact = _normalised_issue_text(text)
    duplicate = any(
        word in text
        for word in (
            "duplicate", "duplicated", "repeated", "included twice",
            "three copies", "two copies", "three competing",
        )
    )
    duplicate = duplicate or "重复" in text or "出现两次" in text
    files = sorted(set(_ISSUE_FILE.findall(text)))

    if (
        ("hard-coded" in text or "hardcoded" in text or "硬编码" in text)
        and ("ref" in text or "引用" in text)
    ):
        return "paper:hard-coded-references"
    if (
        ("page" in text or "页" in text)
        and ("pdf" in text or "paper" in text or "论文" in text)
        and any(
            marker in text
            for marker in (
                "count", "length", "unverif", "cannot be verif", "页数",
            )
        )
    ):
        return "paper:page-count"
    if (
        (
            "unused" in text or "unreferenced" in text or "未使用" in text
            or "never included" in text
        )
        and ("figure" in text or "png" in text or "图" in text)
    ):
        return "artifact:unused-figure"
    if (
        ("scope extension" in text or "extends" in text or "扩展" in text)
        and ("acknowledg" in text or "说明" in text)
    ):
        return "paper:scope-extension-disclosure"
    if (
        ("0.0139" in text or "0.007" in text or "0.005" in text or "0.0046" in text)
        and (
            "wait" in text or "等待" in text
            or "customer-average" in text or "arrival-weighted" in text
        )
    ):
        return "numerical:simulation-wait-difference"
    if (
        ("integration boundary" in text or "integration upper limit" in text
         or "积分上限" in text)
        and ("wait" in text or "等待" in text or "eq.(10)" in text)
    ):
        return "model:average-wait-integration-boundary"
    if (
        ("α=0.9" in text or "alpha=0.9" in text)
        and ("3-window" in text or "3 window" in text or "3窗口" in text)
    ):
        return "numerical:sensitivity:arrival-0.9-window-3"
    if (
        ("α=1.2" in text or "alpha=1.2" in text)
        and ("5-window" in text or "5 window" in text or "5窗口" in text)
    ):
        return "numerical:sensitivity:arrival-1.2-window-5"
    if (
        ("β=1.1" in text or "beta=1.1" in text)
        and ("4-window" in text or "4 window" in text or "4窗口" in text)
    ):
        return "numerical:sensitivity:service-1.1-window-4"
    if (
        ("结果与分析" in text or "results and analysis" in text)
        and duplicate
    ):
        return "duplicate:heading:results-analysis"
    if duplicate:
        image = next(
            (name for name in files if name.endswith((".png", ".jpg", ".jpeg"))),
            "",
        )
        if text.lstrip().startswith("figure") or (
            image and ("included twice" in text or "appears twice" in text)
        ):
            return f"duplicate:figure:{image or 'unspecified'}"
        if "问题分析" in text or "problem analysis" in text:
            return "duplicate:problem-analysis"
        if "模型评价" in text or "model evaluation" in text:
            return "duplicate:model-evaluation"
        if "仿真模型设计" in text or "simulation model design" in text:
            return "duplicate:heading:simulation-model-design"
        if "label" in text or "标签" in text:
            labels = re.findall(r"(?:eq|fig|tab):[a-z0-9_.:-]+", text)
            if {
                "tab:det_results", "fig:det_evolution", "eq:euler",
            } & set(labels):
                return "duplicate:heading:results-analysis"
            return "duplicate:latex-label:" + (
                ",".join(sorted(set(labels))) or "unspecified"
            )
        if "figure" in text or "图" in text:
            return f"duplicate:figure:{image or 'unspecified'}"
    if "display equation" in text or "公式数量" in text:
        return "paper:display-equation-count"
    if "little's law" in text or "little定律" in text or "little定律" in compact:
        return "model:little-law"
    if (
        ("abstract" in text or "摘要" in text)
        and ("critical arrival" in text or "临界到达" in text)
    ):
        return "consistency:abstract-critical-arrival"
    if (
        ("critical arrival" in text or "临界到达" in text)
        and ("student" in text or "学生" in text)
    ):
        return "numerical:critical-arrival-student-count"
    if (
        ("binary search" in text or "二分" in text)
        and any(
            marker in text
            for marker in (
                "inverted", "bug", "incorrect", "wrong", "threshold",
                "条件相反", "实现错误",
            )
        )
    ):
        subject = "service-rate" if (
            "service" in text or "服务率" in text
        ) else "unspecified"
        return f"code:binary-search:{subject}"
    if (
        ("bibliograph" in text or "reference" in text or "参考文献" in text)
        and ("uncited" in text or "never cited" in text or "未引用" in text)
    ):
        citation = next(
            (
                token for token in re.findall(r"\b[a-z][a-z0-9_-]{5,}\b", text)
                if any(char.isdigit() for char in token)
            ),
            "unspecified",
        )
        return f"bibliography:uncited:{citation}"
    if "avg_queue" in text and ("13.33" in text or "13.34" in text):
        return "numerical:period-average-queue-rounding"
    if (
        ("arrival-weighted" in text or "到达加权" in text)
        and (
            "31.97" in text or "12.10" in text or "queue" in text
            or "队列" in text or "q column" in text or "q列" in text
        )
    ):
        return "numerical:arrival-weighted-queue"
    if (
        ("130.03" in text or "130.0" in text)
        and ("end queue" in text or "q_end" in text or "期末队列" in text)
    ):
        return "numerical:three-window-end-queue"
    if (
        ("0.0139" in text or "0.007" in text or "0.005" in text or "0.0046" in text)
        and ("wait" in text or "等待" in text)
    ):
        return "numerical:simulation-wait-difference"
    if (
        ("3 window" in text or "3-window" in text or "3个窗口" in text or "3窗口" in text)
        and ("end queue" in text or "remaining" in text or "剩余" in text)
    ):
        return "numerical:three-window-end-queue"
    if (
        ("arrival sensitivity paragraph" in text or "到达率灵敏度" in text)
        and ("wait" in text or "等待" in text)
        and ("increase" in text or "增幅" in text)
    ):
        return "numerical:arrival-sensitivity-claim"
    if (
        ("4-window" in text or "4 window" in text or "4窗口" in text)
        and ("end queue" in text or "期末队列" in text)
        and ("residual" in text or "0.02" in text or "非零" in text)
    ):
        return "numerical:four-window-end-queue-residual"
    if (
        ("abstract" in text or "摘要" in text)
        and any(marker in text for marker in ("length", "short", "character", "字数", "过短"))
    ):
        return "paper:abstract-length"
    if "20/20" in text and ("cross" in text or "交叉" in text):
        return "validation:crosscheck-20-of-20"
    if (
        ("cost" in text or "成本" in text)
        and ("reinterpret" in text or "重解释" in text or "口径" in text)
    ):
        return "model:cost-definition"
    if (
        ("confidence interval" in text or "置信区间" in text)
        and ("period" in text or "周期" in text)
    ):
        return "validation:period-confidence-interval"
    return None


def _issue_text_similarity(left: VerificationIssue, right: VerificationIssue) -> float:
    left_text = _normalised_issue_text(left.message)
    right_text = _normalised_issue_text(right.message)
    if not left_text or not right_text:
        return 0.0
    sequence = difflib.SequenceMatcher(None, left_text, right_text).ratio()
    left_trigrams = {
        left_text[index:index + 3]
        for index in range(max(0, len(left_text) - 2))
    }
    right_trigrams = {
        right_text[index:index + 3]
        for index in range(max(0, len(right_text) - 2))
    }
    union = left_trigrams | right_trigrams
    jaccard = (
        len(left_trigrams & right_trigrams) / len(union)
        if union else 1.0
    )
    return max(sequence, jaccard)


def _same_issue_family(left: VerificationIssue, right: VerificationIssue) -> bool:
    left_hint = _issue_family_hint(left)
    right_hint = _issue_family_hint(right)
    if left_hint and right_hint:
        return left_hint == right_hint
    similarity = _issue_text_similarity(left, right)
    same_category = (
        _canonical_issue_category(left.category)
        == _canonical_issue_category(right.category)
    )
    if not same_category:
        # Different scoped verifiers frequently assign different category
        # labels to the same sentence-level defect. Only merge across categories
        # when the wording (or referenced file) is strongly aligned.
        left_files = set(_ISSUE_FILE.findall(left.message.casefold()))
        right_files = set(_ISSUE_FILE.findall(right.message.casefold()))
        return similarity >= 0.72 or (
            bool(left_files & right_files) and similarity >= 0.58
        )
    if similarity >= 0.58:
        return True
    left_files = set(_ISSUE_FILE.findall(left.message.casefold()))
    right_files = set(_ISSUE_FILE.findall(right.message.casefold()))
    return bool(left_files & right_files) and similarity >= 0.42


def _merge_issue_details(
    primary: VerificationIssue,
    duplicate: VerificationIssue,
) -> None:
    if _SEVERITY_RANK.get(duplicate.severity, 2) > _SEVERITY_RANK.get(
        primary.severity, 2
    ):
        primary.severity = duplicate.severity
    for field_name, limit in (("evidence", 1800), ("required_fix", 1200)):
        current = getattr(primary, field_name).strip()
        addition = getattr(duplicate, field_name).strip()
        if addition and addition not in current:
            combined = f"{current} | {addition}" if current else addition
            setattr(primary, field_name, combined[:limit])


def _deduplicate_issues(
    issues: list[VerificationIssue],
) -> list[VerificationIssue]:
    """Keep one actionable item per semantic issue family."""
    unique: list[VerificationIssue] = []
    for issue in issues:
        match = next(
            (candidate for candidate in unique if _same_issue_family(candidate, issue)),
            None,
        )
        if match is None:
            unique.append(issue)
        else:
            _merge_issue_details(match, issue)
    return unique


def _default_scopes() -> list[VerificationScope]:
    """Stable mandatory coverage; triage may add focus but cannot replace it."""
    return [
        VerificationScope(
            "model-formulation",
            "Mathematical model and assumptions",
            "Check every sub-problem's variables, assumptions, equations, units, "
            "constraints, initial/boundary conditions, and solution logic. Look for "
            "unsupported simplifications or mathematical gaps.",
            "The mathematical formulation is a mandatory high-impact acceptance area.",
        ),
        VerificationScope(
            "numerical-reproduction",
            "Code and numerical reproduction",
            "Inspect the canonical executable source under src/ and independently "
            "recompute representative high-impact numerical claims. Check residuals, "
            "feasibility, units, edge cases, and whether code implements the paper.",
            "A successful process exit alone does not establish numerical correctness.",
        ),
        VerificationScope(
            "cross-artifact-consistency",
            "Results, figures, tables, and response consistency",
            "Compare final source, result files, figures, tables, stated conclusions, "
            "and the proposed final response. Check coverage of all requested "
            "deliverables and identify contradictions or missing evidence.",
            "Cross-artifact drift is easy to miss when each artifact is checked alone.",
        ),
        VerificationScope(
            "paper-quality",
            "Paper structure, layout, and writing quality",
            "Check the compiled-paper acceptance requirements, abstract, section "
            "structure, equation depth, figure language, readability, and whether the "
            "analysis and formulation are substantive rather than prompt paraphrases.",
            "The final PDF is a first-class deliverable with explicit acceptance rules.",
        ),
        VerificationScope(
            "global-contradictions",
            "Global contradiction and issue-family audit",
            "Read across the complete candidate after the four specialist checks. "
            "Search for contradictions between abstract, assumptions, equations, "
            "results, tables, validation claims, limitations, and conclusion. Treat "
            "every finding as an issue family and inspect all dependent locations.",
            "A locally correct edit can leave stale or contradictory claims elsewhere.",
        ),
    ]


def _normalise_scopes(
    raw_scopes: list[dict[str, Any]],
    max_scopes: int,
) -> list[VerificationScope]:
    scopes: list[VerificationScope] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_scopes):
        if not isinstance(item, dict):
            continue
        raw_id = str(item.get("id", "")).strip().lower()
        scope_id = re.sub(r"[^a-z0-9_-]+", "-", raw_id).strip("-")
        if not scope_id:
            scope_id = f"scope-{index + 1}"
        if scope_id in seen:
            continue
        title = str(item.get("title", "")).strip()
        instructions = str(item.get("instructions", "")).strip()
        rationale = str(item.get("rationale", "")).strip()
        if not title or not instructions:
            continue
        seen.add(scope_id)
        scopes.append(VerificationScope(
            scope_id,
            title,
            instructions,
            rationale or "Selected by the lead verifier during risk triage.",
        ))
        if len(scopes) >= max_scopes:
            break

    for fallback in _default_scopes():
        if len(scopes) >= 3:
            break
        if fallback.id not in seen:
            scopes.append(fallback)
            seen.add(fallback.id)
    return scopes[:max_scopes]


def _read_for_triage(path: Path, max_chars: int) -> str:
    if not path.is_file():
        return f"[missing: {path.name}]"
    text = path.read_text(errors="replace")
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return (
        text[:half]
        + f"\n\n...[middle omitted: {len(text) - max_chars} characters]...\n\n"
        + text[-half:]
    )


def _triage_packet(workdir: Path) -> str:
    """Prepare one bounded, broad read for the lightweight lead verifier."""
    sections = [
        "## problem.md\n" + _read_for_triage(workdir / "problem.md", 100_000),
        f"## {MANIFEST_FILENAME}\n" + _read_for_triage(
            workdir / MANIFEST_FILENAME, 100_000
        ),
        "## paper/main.tex\n" + _read_for_triage(
            workdir / "paper" / "main.tex", 240_000
        ),
    ]
    results_dir = workdir / "results"
    result_budget = 100_000
    result_parts: list[str] = []
    if results_dir.is_dir():
        for path in sorted(results_dir.rglob("*")):
            if not path.is_file() or result_budget <= 0:
                continue
            relative = path.relative_to(workdir)
            chunk = _read_for_triage(path, min(20_000, result_budget))
            result_parts.append(f"### {relative}\n{chunk}")
            result_budget -= len(chunk)
    sections.append("## Computed results\n" + (
        "\n\n".join(result_parts) if result_parts else "[none]"
    ))

    source_budget = 120_000
    source_parts: list[str] = []
    source_dir = workdir / "src"
    if source_dir.is_dir():
        for path in sorted(source_dir.rglob("*")):
            if not path.is_file() or source_budget <= 0:
                continue
            relative = path.relative_to(workdir)
            chunk = _read_for_triage(path, min(30_000, source_budget))
            source_parts.append(f"### {relative}\n{chunk}")
            source_budget -= len(chunk)
    sections.append("## Canonical final source\n" + (
        "\n\n".join(source_parts) if source_parts else "[none]"
    ))

    for directory in ("figures", "data"):
        root = workdir / directory
        names = (
            sorted(str(path.relative_to(workdir)) for path in root.rglob("*")
                   if path.is_file())
            if root.is_dir() else []
        )
        sections.append(f"## {directory} index\n" + (
            "\n".join(names) if names else "[none]"
        ))
    return "\n\n".join(sections)


_STRUCTURAL_HEADING = re.compile(
    r"\\(section|subsection|subsubsection)\*?\{([^{}]+)\}"
)
_LATEX_LABEL = re.compile(r"\\label\{([^{}]+)\}")


def _duplicate_structural_headings(tex: str) -> list[str]:
    """Find repeated headings within the same structural parent."""
    section = ""
    subsection = ""
    counts: dict[tuple[str, ...], int] = {}
    titles: dict[tuple[str, ...], str] = {}
    for match in _STRUCTURAL_HEADING.finditer(tex):
        kind, raw_title = match.groups()
        title = re.sub(r"\s+", " ", raw_title).strip()
        if kind == "section":
            section = title
            subsection = ""
            key = (kind, title)
        elif kind == "subsection":
            subsection = title
            key = (kind, section, title)
        else:
            key = (kind, section, subsection, title)
        counts[key] = counts.get(key, 0) + 1
        titles[key] = title
    return sorted(
        f"{titles[key]} ({count} copies)"
        for key, count in counts.items()
        if count > 1
    )


def _deterministic_metrics_packet(
    workdir: Path,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return authoritative layout/source facts for every verifier scope."""
    paper_cfg = resolve_paper_config(cfg, workdir)
    tex_path = workdir / "paper" / "main.tex"
    pdf_path = workdir / "paper" / "main.pdf"
    packet: dict[str, Any] = {
        "source": "backend deterministic preflight",
        "authoritative": True,
        "configured_page_range": {
            "target": int(paper_cfg.get("target_pages", 20)),
            "min": int(paper_cfg.get("min_pages", 17)),
            "max": int(paper_cfg.get("max_pages", 20)),
            "metric": str(
                paper_cfg.get("page_count_metric") or "total_pages"
            ),
        },
    }
    if not tex_path.is_file():
        packet["inspection_error"] = "paper/main.tex is missing"
        return packet
    tex = tex_path.read_text(errors="replace")
    label_counts: dict[str, int] = {}
    for label in _LATEX_LABEL.findall(tex):
        label_counts[label] = label_counts.get(label, 0) + 1
    packet["duplicate_labels"] = {
        label: count for label, count in label_counts.items() if count > 1
    }
    packet["duplicate_headings"] = _duplicate_structural_headings(tex)
    packet["included_figures"] = sorted(set(re.findall(
        r"\\includegraphics(?:\[[^\]]*\])?\{([^{}]+)\}",
        tex,
    )))
    if not pdf_path.is_file():
        packet["inspection_error"] = "paper/main.pdf is missing"
        return packet
    try:
        packet["paper_metrics"] = asdict(inspect_paper(pdf_path, tex_path))
    except Exception as exc:
        packet["inspection_error"] = f"{type(exc).__name__}: {exc}"
    return packet


def _preflight(
    workdir: Path,
    cfg: dict[str, Any] | None = None,
) -> list[VerificationIssue]:
    """Run deterministic checks before asking the verifier model."""
    issues: list[VerificationIssue] = []
    paper_cfg = resolve_paper_config(cfg, workdir)
    problem = workdir / "problem.md"
    if not problem.is_file() or not problem.read_text(errors="replace").strip():
        issues.append(VerificationIssue(
            "critical", "input", "Problem statement is missing or empty.",
            "problem.md was not found or contained no text.",
            "Restore the source problem and rerun the modeling workflow.",
        ))

    results_dir = workdir / "results"
    result_files = list(results_dir.glob("*")) if results_dir.is_dir() else []
    if not result_files:
        issues.append(VerificationIssue(
            "critical", "evidence", "No computed result artifacts exist.",
            "results/ is missing or empty.",
            "Run the model and persist every cited result under results/.",
        ))
    for path in results_dir.glob("*.json") if results_dir.is_dir() else []:
        try:
            json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            issues.append(VerificationIssue(
                "critical", "evidence", f"Invalid result JSON: {path.name}.",
                str(exc),
                "Regenerate the result file from executable code as valid JSON.",
            ))

    source_dir = workdir / "src"
    source_files = [
        path for path in source_dir.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ] if source_dir.is_dir() else []
    if not source_files:
        issues.append(VerificationIssue(
            "major", "reproducibility", "No canonical final source artifact exists.",
            "src/ is missing or contains no source files.",
            "Preserve the final executable model under src/ before verification.",
        ))
    for path in (item for item in source_files if item.suffix.lower() == ".py"):
        try:
            compile(path.read_text(errors="replace"), str(path), "exec")
        except SyntaxError as exc:
            issues.append(VerificationIssue(
                "critical", "reproducibility",
                f"Final Python source cannot be parsed: {path.name}.", str(exc),
                "Repair the canonical source and regenerate dependent results.",
            ))

    tex_path = workdir / "paper" / "main.tex"
    pdf_path = workdir / "paper" / "main.pdf"
    if not tex_path.is_file() or not pdf_path.is_file():
        issues.append(VerificationIssue(
            "critical", "deliverable", "The final paper source or PDF is missing.",
            f"main.tex={tex_path.is_file()}, main.pdf={pdf_path.is_file()}",
            "Regenerate and successfully compile the final paper.",
        ))
    elif pdf_path.stat().st_size < 1000:
        issues.append(VerificationIssue(
            "critical", "deliverable", "The generated PDF appears invalid.",
            f"paper/main.pdf size is {pdf_path.stat().st_size} bytes.",
            "Fix compilation and produce a readable non-empty PDF.",
        ))

    if tex_path.is_file():
        tex = tex_path.read_text(errors="replace")
        bare_percent_lines = find_unescaped_percent_lines(tex)
        if bare_percent_lines:
            issues.append(VerificationIssue(
                "critical",
                "paper-format",
                "Unescaped percent signs truncate visible LaTeX prose.",
                "; ".join(bare_percent_lines[:12]),
                "Replace every prose percent sign with \\% and recompile the PDF.",
            ))
        label_counts: dict[str, int] = {}
        for label in _LATEX_LABEL.findall(tex):
            label_counts[label] = label_counts.get(label, 0) + 1
        duplicate_labels = {
            label: count for label, count in label_counts.items() if count > 1
        }
        if duplicate_labels:
            issues.append(VerificationIssue(
                "critical",
                "paper-structure",
                "LaTeX labels are duplicated in the paper source.",
                "; ".join(
                    f"{label} appears {count} times"
                    for label, count in sorted(duplicate_labels.items())
                ),
                "Keep one definition for each label and remove the duplicated "
                "section, equation, table, or figure block before verification.",
            ))
        duplicate_headings = _duplicate_structural_headings(tex)
        if duplicate_headings:
            issues.append(VerificationIssue(
                "critical",
                "paper-structure",
                "Structural headings are repeated within the same parent section.",
                "; ".join(duplicate_headings),
                "Remove the stale duplicated blocks, then recompile the paper.",
            ))
        duplicate = re.search(
            r"\\(?:sub)*section\*?\{\s*((?:\d+\.)+\d+)\s+\1\b",
            tex,
        )
        if duplicate:
            issues.append(VerificationIssue(
                "major", "paper-format", "A section number is duplicated in its title.",
                duplicate.group(0),
                "Remove manual numbering and let LaTeX number the heading.",
            ))
        structural_failures = inspect_latex_structure(tex)
        for failure in structural_failures:
            issues.append(VerificationIssue(
                "critical",
                "paper-structure",
                "The LaTeX document hierarchy or region order is invalid.",
                failure,
                "Keep each main chapter in a separate top-level section, place "
                "appendices after the main paper, and place references last.",
            ))

    if tex_path.is_file() and pdf_path.is_file() and pdf_path.stat().st_size >= 1000:
        target_pages = int(paper_cfg.get("target_pages", 20))
        min_pages = int(paper_cfg.get("min_pages", 17))
        max_pages = int(paper_cfg.get("max_pages", target_pages))
        min_fill = float(paper_cfg.get("abstract_fill_min_ratio", 0.72))
        min_cjk_chars = int(paper_cfg.get("abstract_cjk_min_chars", 800))
        min_english_words = int(paper_cfg.get("abstract_english_min_words", 450))
        min_equations = int(paper_cfg.get("min_display_equations", 12))
        try:
            metrics = inspect_paper(pdf_path, tex_path)
        except Exception as exc:
            issues.append(VerificationIssue(
                "critical", "paper-layout", "The final PDF cannot be inspected.",
                f"{type(exc).__name__}: {exc}",
                "Regenerate a valid PDF and rerun the layout acceptance checks.",
            ))
        else:
            accepted_page_count = selected_page_count(metrics, paper_cfg)
            page_count_metric = str(
                paper_cfg.get("page_count_metric") or "total_pages"
            )
            if not min_pages <= accepted_page_count <= max_pages:
                issues.append(VerificationIssue(
                    "major", "paper-length",
                    f"The paper is outside the accepted {min_pages}–{max_pages} page range.",
                    f"Detected accepted_count={accepted_page_count} using "
                    f"{page_count_metric}; total={metrics.page_count}, "
                    f"main_body={metrics.main_body_page_count}, "
                    f"appendix={metrics.appendix_page_count}, "
                    f"references={metrics.reference_page_count}.",
                    "Add or trim substantive derivation, validation, interpretation, "
                    f"tables, and figures in the counted main paper until it is "
                    f"{min_pages}–{max_pages} pages; appendices cannot satisfy this gate.",
                ))
            if metrics.first_section_page != 2:
                issues.append(VerificationIssue(
                    "major", "abstract-layout",
                    "The abstract does not occupy exactly the first page.",
                    f"The first numbered section begins on page {metrics.first_section_page}.",
                    "Keep title, abstract, validation summary, and keywords on page 1, "
                    "then force Section 1 to begin on page 2.",
                ))
            if metrics.abstract_fill_ratio < min_fill:
                issues.append(VerificationIssue(
                    "major", "abstract-layout", "The abstract page is under-filled.",
                    f"Text reaches {metrics.abstract_fill_ratio:.0%} of page height; "
                    f"minimum is {min_fill:.0%}.",
                    "Expand the abstract with each sub-problem's model, method, key "
                    "result, and validation evidence without adding background filler.",
                ))
            is_cjk = "ctexart" in tex
            if is_cjk and metrics.abstract_cjk_chars < min_cjk_chars:
                issues.append(VerificationIssue(
                    "major", "abstract-content", "The Chinese abstract is too short.",
                    f"Detected {metrics.abstract_cjk_chars} Chinese characters; "
                    f"minimum is {min_cjk_chars}.",
                    "Write a complete 800–1200-character competition abstract covering "
                    "the overall route, every sub-problem, quantitative results, and validation.",
                ))
            if not is_cjk and metrics.abstract_english_words < min_english_words:
                issues.append(VerificationIssue(
                    "major", "abstract-content", "The English abstract is too short.",
                    f"Detected {metrics.abstract_english_words} words; "
                    f"minimum is {min_english_words}.",
                    "Write a complete one-page summary covering every task, result, "
                    "and validation outcome.",
                ))
            if metrics.display_equation_count < min_equations:
                issues.append(VerificationIssue(
                    "major", "model-formulation",
                    "The mathematical formulation is too sparse.",
                    f"Detected {metrics.display_equation_count} display equations; "
                    f"minimum is {min_equations}.",
                    "Add substantive definitions, governing/geometric relations, "
                    "derivation, objective, constraints, conditions, and validation "
                    "equations; do not split trivial identities merely to raise the count.",
                ))

    if paper_cfg.get("figures_english_only", True):
        bad_plot_labels = find_non_english_plot_labels_in_sources(workdir / "src")
        if bad_plot_labels:
            issues.append(VerificationIssue(
                "major", "figure-language",
                "Generated figures contain Chinese text labels.",
                "; ".join(bad_plot_labels[:12]),
                "Regenerate every affected figure with English-only titles, axes, "
                "legends, annotations, tick categories, and colorbar labels. Put "
                "Chinese explanations in the LaTeX captions.",
            ))
    return issues


def _copy_artifacts(source: Path, destination: Path) -> None:
    """Create a final-artifact-only isolated verifier workspace."""
    for name in ("data", "src", "results", "figures", "paper", "assets"):
        src = source / name
        if src.is_dir():
            ignored = (
                shutil.ignore_patterns("revisions") if name == "paper"
                else shutil.ignore_patterns("__pycache__", "*.pyc") if name == "src"
                else None
            )
            shutil.copytree(
                src,
                destination / name,
                ignore=ignored,
            )
    for name in (
        "problem.md",
        PAPER_PROFILE_FILENAME,
    ):
        src = source / name
        if src.is_file():
            shutil.copy2(src, destination / name)
    write_artifact_manifest(destination)


def _inspection_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (read_file_tool, results_list_tool, results_get_tool, run_code_tool):
        registry.register(tool)
    return registry


def _shared_quality_prompt(workdir: Path) -> str:
    """Editable quality criteria without the final coordinator's tool contract."""
    return str(get_verifier_prompt(workdir)["content"]).strip()


def _record_agent_progress(
    emit: Callable[[str, dict[str, Any]], None],
    *,
    role: str,
    scope: VerificationScope | None = None,
) -> Callable[[str, dict[str, Any]], None]:
    def record(kind: str, data: dict[str, Any]) -> None:
        if kind in {"tool_heartbeat", "provider_heartbeat"}:
            payload: dict[str, Any] = {
                "role": role,
                "name": data.get("name"),
                "provider": data.get("provider"),
                "model": data.get("model"),
                "request_phase": data.get("request_phase"),
                "step": data.get("step"),
                "elapsed_seconds": data.get("elapsed_seconds"),
                "scope": data.get("scope"),
            }
            if scope is not None:
                payload["scope_id"] = scope.id
                payload["scope_title"] = scope.title
            emit(kind, payload)
            return

        payload: dict[str, Any] = {
            "phase": kind,
            "role": role,
            "step": data.get("step"),
        }
        if scope is not None:
            payload["scope_id"] = scope.id
            payload["scope_title"] = scope.title
        for key in (
            "name", "tool_calls", "tool", "total_tokens",
            "context_tokens", "num_messages", "timed_out",
        ):
            if data.get(key) is not None:
                payload[key] = data[key]
        if data.get("text"):
            payload["text"] = str(data["text"])[:2000]
        if data.get("observation"):
            payload["observation"] = str(data["observation"])[:1000]
        emit("verification_progress", payload)

    return record


def _ensure_fragment_checklist(
    fragment: VerificationFragment,
    scope: VerificationScope,
    prior_issues: list[dict[str, Any]],
) -> None:
    """Make a readable verdict machine-readable without discarding findings."""
    required = [
        check_id for check_id, _ in _FIXED_SCOPE_CHECKLISTS.get(scope.id, [])
    ]
    blocking = (
        fragment.verdict != "PASS"
        or any(
            issue.severity in {"critical", "major"}
            for issue in fragment.issues
        )
    )
    inferred_status = "FAIL" if blocking else "PASS"
    record = "\n".join(fragment.checks_performed)
    for check_id in required:
        completed = re.search(
            rf"(?im)^\s*{re.escape(check_id)}\s+(?:PASS|FAIL)\s*:",
            record,
        )
        if completed:
            continue
        if inferred_status == "PASS":
            detail = (
                "The scoped verifier returned PASS with no unresolved material "
                "finding for this mandatory check."
            )
        else:
            detail = (
                "The scoped verifier reported unresolved or insufficient evidence; "
                "see its summary and issues."
            )
        added = f"{check_id} {inferred_status}: {detail}"
        fragment.checks_performed.append(added)
        record += "\n" + added

    if scope.id != "global-contradictions":
        return
    for item in prior_issues:
        issue_id = item.get("id")
        if not issue_id:
            continue
        check_id = f"LEDGER-{issue_id}"
        completed = re.search(
            rf"(?im)^\s*{re.escape(check_id)}\s+(?:PASS|FAIL)\s*:",
            record,
        )
        if completed:
            continue
        prior_issue = _issue_from_dict(item)
        still_present = any(
            _same_issue_family(prior_issue, current)
            for current in fragment.issues
        )
        status = "FAIL" if still_present else "PASS"
        detail = (
            "The same issue family remains in the current scoped findings."
            if still_present else
            "The global audit did not reproduce this prior issue in the current "
            "candidate."
        )
        added = f"{check_id} {status}: {detail}"
        fragment.checks_performed.append(added)
        record += "\n" + added


def _bounded_trace(items: list[str], max_chars: int) -> list[str]:
    """Keep the newest complete trace items without carrying tool-call history."""
    selected: list[str] = []
    used = 0
    for item in reversed(items):
        cleaned = item.strip()
        if not cleaned:
            continue
        clipped = cleaned[:VERIFIER_TRACE_ITEM_MAX_CHARS]
        if selected and used + len(clipped) > max_chars:
            break
        selected.append(clipped)
        used += len(clipped)
    selected.reverse()
    return selected


def _collect_scope_trace(
    messages: list[dict[str, Any]],
) -> tuple[str, list[str], list[str]]:
    """Extract readable evidence without reusing the inspection conversation."""
    tool_names: dict[str, str] = {}
    analyses: list[str] = []
    observations: list[str] = []
    checks: list[str] = []
    seen_checks: set[str] = set()

    for message in messages:
        role = message.get("role")
        if role == "assistant":
            content = str(message.get("content") or "").strip()
            if content:
                analyses.append(content)
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or "")
                function = call.get("function") or {}
                if call_id and isinstance(function, dict):
                    tool_names[call_id] = str(function.get("name") or "tool")
        elif role == "tool":
            content = str(message.get("content") or "").strip()
            if not content or content.startswith("[error]"):
                continue
            name = tool_names.get(str(message.get("tool_call_id") or ""), "tool")
            first_line = content.splitlines()[0][:240]
            check = f"{name}: {first_line}"
            if check not in seen_checks:
                seen_checks.add(check)
                checks.append(check)
            observations.append(f"{name}\n{content}")

    analysis_items = _bounded_trace(
        analyses,
        VERIFIER_TRACE_MAX_CHARS // 2,
    )
    observation_items = _bounded_trace(
        observations,
        VERIFIER_TRACE_MAX_CHARS // 2,
    )
    packet_parts: list[str] = []
    if analysis_items:
        packet_parts.append(
            "## Retained verifier analysis\n"
            + "\n\n".join(
                f"Analysis note {index}:\n{item}"
                for index, item in enumerate(analysis_items, start=1)
            )
        )
    if observation_items:
        packet_parts.append(
            "## Retained successful tool evidence\n"
            + "\n\n".join(observation_items)
        )
    return "\n\n".join(packet_parts), checks[-24:], analyses


def _recover_scope_submission(
    *,
    cfg: dict[str, Any],
    scope: VerificationScope,
    agent: Agent,
    registry: ToolRegistry,
    ctx: ToolContext,
    submitted: list[VerificationFragment],
    prior_issues: list[dict[str, Any]],
    emit: Callable[[str, dict[str, Any]], None],
) -> bool:
    """Run a clean, submission-only pass after the inspection loop ends."""
    trace_packet, _, _ = _collect_scope_trace(agent.messages)
    submit_schemas = [
        schema for schema in registry.schemas()
        if schema.get("function", {}).get("name")
        == "submit_verification_fragment"
    ]
    if not submit_schemas:
        return False

    emit("verification_progress", {
        "phase": "finalization_recovery_start",
        "role": "subagent",
        "scope_id": scope.id,
        "scope_title": scope.title,
    })
    verification_cfg = cfg.get("verification", {})
    provider = build_provider({
        **cfg,
        **verification_cfg.get("provider_config", {}),
    })
    forced_choice = {
        "type": "function",
        "function": {"name": "submit_verification_fragment"},
    }
    system_message = (
        "You are in an isolated scoped-verifier FINALIZATION pass. The inspection "
        "conversation has ended and is intentionally not available. Do not inspect "
        "files, run code, request more evidence, or call any previous tool. Based "
        "only on the evidence packet, call submit_verification_fragment exactly "
        "once. Use INCONCLUSIVE if the retained evidence is insufficient."
    )
    user_message = (
        f"Scope id: {scope.id}\n"
        f"Scope title: {scope.title}\n"
        f"Original assignment: {scope.instructions}\n\n"
        "Mandatory checklist ids:\n"
        + "\n".join(
            f"- {check_id}: {description}"
            for check_id, description in _FIXED_SCOPE_CHECKLISTS.get(scope.id, [])
        )
        + (
            "\nPrior issue ledger ids requiring explicit PASS or FAIL closure:\n"
            + "\n".join(
                f"- LEDGER-{item['id']}: {item.get('message', '')}"
                for item in prior_issues if item.get("id")
            )
            if scope.id == "global-contradictions" and prior_issues else ""
        )
        + "\n\n"
        f"{trace_packet or 'No readable evidence was retained.'}\n\n"
        "Submit the structured scope verdict now."
    )

    for recovery_attempt in range(1, VERIFIER_RECOVERY_ATTEMPTS + 1):
        emit("verification_progress", {
            "phase": "finalization_recovery_attempt",
            "role": "subagent",
            "scope_id": scope.id,
            "scope_title": scope.title,
            "recovery_attempt": recovery_attempt,
        })
        try:
            with model_request_context(
                agent_role=f"Verifier · Recovery · {scope.title}",
                phase="verification_recovery",
                system_prompt_source="Isolated verifier recovery prompt",
            ):
                response = provider.chat(
                    [
                        {"role": "system", "content": system_message},
                        {"role": "user", "content": user_message},
                    ],
                    tools=submit_schemas,
                    tool_choice=forced_choice,
                )
            agent.total_usage = agent.total_usage + response.usage
        except Exception as exc:
            emit("verification_progress", {
                "phase": "finalization_recovery_error",
                "role": "subagent",
                "scope_id": scope.id,
                "scope_title": scope.title,
                "observation": f"{type(exc).__name__}: {exc}",
            })
            continue

        emit("verification_progress", {
            "phase": "assistant",
            "role": "subagent",
            "scope_id": scope.id,
            "scope_title": scope.title,
            "text": response.text or "",
            "tool_calls": [
                (call.name, call.arguments) for call in response.tool_calls
            ],
        })
        for call in response.tool_calls:
            if call.name != "submit_verification_fragment":
                continue
            try:
                args = json.loads(call.arguments or "{}")
            except json.JSONDecodeError as exc:
                emit("verification_progress", {
                    "phase": "finalization_recovery_error",
                    "role": "subagent",
                    "scope_id": scope.id,
                    "scope_title": scope.title,
                    "observation": f"Invalid submission JSON: {exc}",
                })
                continue
            if not isinstance(args, dict):
                continue
            observation = registry.dispatch(
                ctx,
                "submit_verification_fragment",
                args,
            )
            emit("verification_progress", {
                "phase": "tool_result",
                "role": "subagent",
                "scope_id": scope.id,
                "scope_title": scope.title,
                "name": "submit_verification_fragment",
                "observation": observation,
            })
            if submitted and not observation.startswith("[error]"):
                emit("verification_progress", {
                    "phase": "finalization_recovery_complete",
                    "role": "subagent",
                    "scope_id": scope.id,
                    "scope_title": scope.title,
                    "recovery_attempt": recovery_attempt,
                })
                return True

    emit("verification_progress", {
        "phase": "finalization_recovery_failed",
        "role": "subagent",
        "scope_id": scope.id,
        "scope_title": scope.title,
    })
    return False


def _fallback_scope_fragment(
    agent: Agent,
    scope: VerificationScope,
) -> VerificationFragment:
    """Preserve readable work when even the isolated submission pass fails."""
    _, checks, analyses = _collect_scope_trace(agent.messages)
    retained_notes = _bounded_trace(analyses, 3_600)[-6:]
    notes_text = "\n".join(f"- {note}" for note in retained_notes)
    summary = (
        "The scoped verifier completed inspection but its structured submission "
        f"failed. {len(checks)} successful check records were retained."
    )
    if notes_text:
        summary += f"\n\nRetained verifier notes:\n{notes_text}"
    evidence_parts = []
    if checks:
        evidence_parts.append(
            "Completed checks:\n" + "\n".join(f"- {check}" for check in checks)
        )
    if notes_text:
        evidence_parts.append("Verifier notes:\n" + notes_text)
    evidence = "\n\n".join(evidence_parts) or (
        "The inspection trace contained no readable successful result."
    )
    return VerificationFragment(
        scope_id=scope.id,
        scope_title=scope.title,
        verdict="INCONCLUSIVE",
        summary=summary,
        checks_performed=checks,
        issues=[VerificationIssue(
            "major",
            "verification-protocol",
            (
                f"The {scope.title} scope completed inspection, but its structured "
                "conclusion could not be recorded."
            ),
            evidence[:8_000],
            (
                "Review the retained notes above and rerun the isolated "
                "submission-only finalization pass before approval."
            ),
        )],
    )


def _run_triage(
    cfg: dict[str, Any],
    workdir: Path,
    isolated: Path,
    candidate: str,
    static_issues: list[VerificationIssue],
    stop_event,
    emit: Callable[[str, dict[str, Any]], None],
    max_scopes: int,
) -> tuple[list[VerificationScope], int]:
    submitted: list[dict[str, Any]] = []
    registry = ToolRegistry()

    def submit_plan(_ctx: ToolContext, args: dict[str, Any]) -> str:
        if submitted:
            return "[error] A verification plan has already been recorded."
        submitted.append(args)
        return "Verification plan recorded."

    registry.register(Tool(
        name="submit_verification_plan",
        description=(
            "Submit 3–5 adaptive risk-focus notes after broad triage. These notes "
            "guide, but never replace, the fixed mandatory verification scopes."
        ),
        parameters=_PLAN_PARAMS,
        handler=submit_plan,
    ))
    verification_cfg = cfg.get("verification", {})
    max_steps = max(
        1,
        int(verification_cfg.get(
            "triage_max_steps", DEFAULT_VERIFIER_TRIAGE_STEPS
        )),
    )
    agent = Agent(
        provider=build_provider({
            **cfg,
            **verification_cfg.get("provider_config", {}),
        }),
        registry=registry,
        ctx=ToolContext(
            workdir=isolated,
            sandbox=build_sandbox(cfg, isolated),
            scope="verify_lead_triage_",
            stop_event=stop_event,
            settings=cfg,
        ),
        system_prompt=(
            _shared_quality_prompt(workdir)
            + "\n\nYou are the lead verification coordinator in the TRIAGE stage. "
              "Read the supplied broad artifact packet once, identify likely failure "
              "points, and submit 3–5 concise adaptive risk-focus notes. Five fixed "
              "verification scopes are enforced by the orchestrator, so your notes "
              "cannot remove or narrow formulation, numerical reproduction, artifact "
              "consistency, paper quality, or global contradiction coverage. Do not "
              "perform deep recalculation yourself. Finish only by calling "
              "submit_verification_plan."
        ),
        compact_threshold_tokens=cfg["context"]["compact_threshold_tokens"],
        max_steps=max_steps,
        on_event=_record_agent_progress(emit, role="lead-triage"),
        include_planning_memory=False,
        finalization_tool="submit_verification_plan",
        finalization_steps=max_steps,
        finalization_instruction=(
            "[Lead-verifier triage boundary]\n"
            "Use the artifact packet already supplied and call "
            "submit_verification_plan now with 3–5 adaptive risk-focus notes."
        ),
    )
    emit("verification_progress", {
        "phase": "triage_start",
        "role": "lead-triage",
    })
    task = (
        "Identify adaptive risk-focus notes for the fixed verification plan.\n\n"
        f"## Proposed final answer\n{candidate}\n\n"
        "## Deterministic preflight findings\n"
        + json.dumps(
            [asdict(issue) for issue in static_issues],
            ensure_ascii=False,
            indent=2,
        )
        + "\n\n## Broad artifact packet\n"
        + _triage_packet(isolated)
    )
    agent.run(task)
    triage_tokens = agent.total_usage.total_tokens
    raw_scopes = submitted[-1].get("scopes", []) if submitted else []
    adaptive_focus = json.dumps(raw_scopes, ensure_ascii=False, indent=2)[:12_000]
    scopes = []
    for fixed in _default_scopes():
        focus = (
            "\n\nLead verifier adaptive risk notes (focus only; do not skip the "
            f"mandatory checklist):\n{adaptive_focus}"
            if adaptive_focus else ""
        )
        scopes.append(VerificationScope(
            fixed.id,
            fixed.title,
            fixed.instructions + focus,
            fixed.rationale,
        ))
    emit("verification_progress", {
        "phase": "triage_complete",
        "role": "lead-triage",
        "scope_count": len(scopes),
        "scopes": [scope.to_dict() for scope in scopes],
        "fallback": not bool(submitted),
        "fixed_coverage": True,
        "total_tokens": triage_tokens,
    })
    return scopes, triage_tokens


def _issue_fingerprint(issue: VerificationIssue | dict[str, Any]) -> str:
    value = issue if isinstance(issue, VerificationIssue) else _issue_from_dict(issue)
    family = _issue_family_hint(value)
    stable_text = family or (
        f"{_canonical_issue_category(value.category)}\n"
        f"{_normalised_issue_text(value.message)}"
    )
    digest = hashlib.sha256(stable_text.encode()).hexdigest()
    return digest[:12]


def _load_issue_ledger(verification_dir: Path) -> list[dict[str, Any]]:
    path = verification_dir / "issue_ledger.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    entries = payload.get("entries", []) if isinstance(payload, dict) else []
    # Migrate older ledgers that stored one entry per scoped wording. Re-key
    # them with the semantic family fingerprint so a resumed run does not ask
    # the global verifier to close dozens of aliases for the same defect.
    migrated: dict[str, dict[str, Any]] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        issue = _issue_from_dict(item)
        issue_id = next(
            (
                existing_id
                for existing_id, existing in migrated.items()
                if _same_issue_family(_issue_from_dict(existing), issue)
            ),
            _issue_fingerprint(issue),
        )
        current = migrated.get(issue_id)
        if current is None:
            migrated[issue_id] = {
                **item,
                "id": issue_id,
            }
            continue
        merged_issue = _issue_from_dict(current)
        _merge_issue_details(merged_issue, issue)
        current.update({
            "severity": merged_issue.severity,
            "category": merged_issue.category,
            "message": merged_issue.message,
            "evidence": merged_issue.evidence,
            "required_fix": merged_issue.required_fix,
            "status": (
                "open"
                if "open" in {current.get("status"), item.get("status")}
                else "resolved"
            ),
            "first_seen_attempt": min(
                int(current.get("first_seen_attempt", 1)),
                int(item.get("first_seen_attempt", 1)),
            ),
            "last_seen_attempt": max(
                int(current.get("last_seen_attempt", 0)),
                int(item.get("last_seen_attempt", 0)),
            ),
            "last_checked_attempt": max(
                int(current.get("last_checked_attempt", 0)),
                int(item.get("last_checked_attempt", 0)),
            ),
        })
    return list(migrated.values())


def _write_issue_ledger(
    verification_dir: Path,
    attempt: int,
    prior_items: list[dict[str, Any]],
    report: VerificationReport,
    fragments: list[VerificationFragment],
    *,
    full_review_completed: bool = False,
) -> list[dict[str, Any]]:
    global_fragment = next(
        (item for item in fragments if item.scope_id == "global-contradictions"),
        None,
    )
    global_checks = "\n".join(
        global_fragment.checks_performed if global_fragment else []
    ).upper()
    current_by_id = {
        _issue_fingerprint(issue): issue for issue in report.issues
    }
    entries: dict[str, dict[str, Any]] = {}
    for item in prior_items:
        issue_id = str(item.get("id", ""))
        if not issue_id:
            continue
        already_resolved = (
            item.get("status") == "resolved"
            and issue_id not in current_by_id
        )
        resolved = already_resolved or (
            issue_id not in current_by_id
            and (
                full_review_completed
                or f"LEDGER-{issue_id} PASS".upper() in global_checks
            )
        )
        entries[issue_id] = {
            **item,
            "status": "resolved" if resolved else "open",
            "last_checked_attempt": attempt,
        }
    for issue_id, issue in current_by_id.items():
        previous = entries.get(issue_id, {})
        entries[issue_id] = {
            "id": issue_id,
            "severity": issue.severity,
            "category": issue.category,
            "message": issue.message,
            "evidence": issue.evidence,
            "required_fix": issue.required_fix,
            "status": "open",
            "first_seen_attempt": previous.get("first_seen_attempt", attempt),
            "last_seen_attempt": attempt,
            "last_checked_attempt": attempt,
        }
    payload = {
        "attempt": attempt,
        "entries": sorted(entries.values(), key=lambda item: (
            item.get("status") == "resolved",
            item.get("severity", ""),
            item.get("id", ""),
        )),
    }
    (verification_dir / "issue_ledger.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2)
    )
    return payload["entries"]


def _candidate_diff_packet(
    workdir: Path,
    verification_dir: Path,
    attempt: int,
) -> str:
    previous_root = verification_dir / "candidates" / f"attempt_{attempt - 1}"
    if attempt <= 1 or not previous_root.is_dir():
        text = "[initial candidate; no previous candidate diff]"
        (verification_dir / f"candidate_diff_attempt_{attempt}.patch").write_text(text)
        return text

    paths: set[Path] = set()
    for relative in (Path("paper/main.tex"),):
        paths.add(relative)
    for root in (workdir / "src", previous_root / "src"):
        if root.is_dir():
            for path in root.rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts:
                    paths.add(Path("src") / path.relative_to(root))
    for root in (workdir / "results", previous_root / "results"):
        if root.is_dir():
            for path in root.rglob("*.json"):
                paths.add(Path("results") / path.relative_to(root))

    chunks: list[str] = []
    for relative in sorted(paths):
        before_path = previous_root / relative
        after_path = workdir / relative
        before = (
            before_path.read_text(errors="replace").splitlines()
            if before_path.is_file() else []
        )
        after = (
            after_path.read_text(errors="replace").splitlines()
            if after_path.is_file() else []
        )
        if before == after:
            continue
        chunks.extend(difflib.unified_diff(
            before,
            after,
            fromfile=f"attempt_{attempt - 1}/{relative}",
            tofile=f"attempt_{attempt}/{relative}",
            lineterm="",
            n=3,
        ))
    text = "\n".join(chunks) or "[no textual candidate changes detected]"
    figure_paths: set[Path] = set()
    for root in (workdir / "figures", previous_root / "figures"):
        if root.is_dir():
            for path in root.rglob("*"):
                if path.is_file():
                    figure_paths.add(Path("figures") / path.relative_to(root))
    figure_changes: list[str] = []
    for relative in sorted(figure_paths):
        before_path = previous_root / relative
        after_path = workdir / relative
        before_hash = (
            hashlib.sha256(before_path.read_bytes()).hexdigest()[:12]
            if before_path.is_file() else "missing"
        )
        after_hash = (
            hashlib.sha256(after_path.read_bytes()).hexdigest()[:12]
            if after_path.is_file() else "missing"
        )
        if before_hash != after_hash:
            figure_changes.append(
                f"FIGURE {relative}: {before_hash} -> {after_hash}"
            )
    if figure_changes:
        text += "\n\n" + "\n".join(figure_changes)
    (verification_dir / f"candidate_diff_attempt_{attempt}.patch").write_text(text)
    return text[:60_000]


def _snapshot_candidate(
    workdir: Path,
    verification_dir: Path,
    attempt: int,
) -> None:
    destination = verification_dir / "candidates" / f"attempt_{attempt}"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for relative in (Path("paper/main.tex"),):
        source = workdir / relative
        if source.is_file():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    source_dir = workdir / "src"
    if source_dir.is_dir():
        shutil.copytree(
            source_dir,
            destination / "src",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    results = workdir / "results"
    if results.is_dir():
        shutil.copytree(results, destination / "results")
    figures = workdir / "figures"
    if figures.is_dir():
        shutil.copytree(figures, destination / "figures")


def _run_scope_verifier(
    cfg: dict[str, Any],
    source: Path,
    destination: Path,
    candidate: str,
    static_issues: list[VerificationIssue],
    scope: VerificationScope,
    prior_issues: list[dict[str, Any]],
    candidate_diff: str,
    max_steps: int,
    stop_event,
    emit: Callable[[str, dict[str, Any]], None],
    upstream_reports: list[VerificationFragment] | None = None,
) -> VerificationFragment:
    destination.mkdir(parents=True, exist_ok=True)
    _copy_artifacts(source, destination)
    submitted: list[VerificationFragment] = []
    registry = _inspection_registry()

    def submit_fragment(_ctx: ToolContext, args: dict[str, Any]) -> str:
        if submitted:
            return "[error] A scoped verification result has already been recorded."
        fragment = VerificationFragment(
            scope_id=scope.id,
            scope_title=scope.title,
            verdict=str(args.get("verdict", "INCONCLUSIVE")).upper(),
            summary=str(args.get("summary", "")),
            checks_performed=[
                str(item) for item in args.get("checks_performed", [])
            ],
            issues=[
                _issue_from_dict(item)
                for item in args.get("issues", [])
                if isinstance(item, dict)
            ],
        )
        _ensure_fragment_checklist(fragment, scope, prior_issues)
        submitted.append(fragment)
        return "Scoped verification result recorded."

    registry.register(Tool(
        name="submit_verification_fragment",
        description=(
            "Submit the structured result for your assigned scope. Use "
            "INCONCLUSIVE when required evidence cannot be obtained."
        ),
        parameters=_FRAGMENT_PARAMS,
        handler=submit_fragment,
    ))
    verification_cfg = cfg.get("verification", {})
    max_steps = max(VERIFIER_FINALIZATION_STEPS, int(max_steps))
    provider_cfg = {
        **cfg,
        **verification_cfg.get("provider_config", {}),
    }
    ctx = ToolContext(
        workdir=destination,
        sandbox=build_sandbox(provider_cfg, destination),
        scope=f"verify_{scope.id}_",
        stop_event=stop_event,
        settings=cfg,
    )
    agent = Agent(
        provider=build_provider(provider_cfg),
        registry=registry,
        ctx=ctx,
        system_prompt=(
            _shared_quality_prompt(source)
            + "\n\nYou are an independent scoped verification subagent. Work only "
              "on the assignment below, except where a direct dependency must be "
              "checked. Use concrete artifact evidence and independent computation; "
              "do not trust the paper's own assertions. Do not edit the candidate. "
              "Finish by calling submit_verification_fragment. PASS means this scope "
              "has sufficient evidence and no critical or major defect; use REVISE "
              "for a demonstrated material defect and INCONCLUSIVE when evidence is "
              "missing or the check cannot be completed."
        ),
        compact_threshold_tokens=cfg["context"]["compact_threshold_tokens"],
        max_steps=max_steps,
        on_event=_record_agent_progress(
            emit,
            role="subagent",
            scope=scope,
        ),
        include_planning_memory=False,
        finalization_tool="submit_verification_fragment",
        finalization_steps=VERIFIER_FINALIZATION_STEPS,
        finalization_instruction=(
            "[Scoped-verifier deadline]\n"
            "Stop inspecting. Based on evidence already collected, call "
            "submit_verification_fragment now. Use INCONCLUSIVE rather than "
            "inventing support. Plain text is not accepted."
        ),
    )
    emit("verification_progress", {
        "phase": "subcheck_start",
        "role": "subagent",
        "scope_id": scope.id,
        "scope_title": scope.title,
    })
    task = (
        f"## Assigned scope: {scope.title}\n"
        f"Scope id: {scope.id}\n"
        f"Why assigned: {scope.rationale}\n"
        f"Instructions: {scope.instructions}\n\n"
        f"## Proposed final answer\n{candidate}\n\n"
        "## Deterministic preflight findings\n"
        + json.dumps(
            [asdict(issue) for issue in static_issues],
            ensure_ascii=False,
            indent=2,
        )
        + "\n\n## Authoritative deterministic paper metrics\n"
        + json.dumps(
            _deterministic_metrics_packet(source, cfg),
            ensure_ascii=False,
            indent=2,
        )
        + "\nThese backend measurements are authoritative. Do not replace page, "
          "abstract, equation, duplicate-label, or duplicate-heading values with "
          "a raw PDF parser estimate. If another tool disagrees, report the "
          "disagreement but use this packet for the verdict."
        + "\n\n## Mandatory checklist\n"
        + "\n".join(
            f"- {check_id}: {description}"
            for check_id, description in _FIXED_SCOPE_CHECKLISTS.get(scope.id, [])
        )
        + (
            "\n\n## Prior unresolved issue ledger\n"
            + json.dumps(prior_issues, ensure_ascii=False, indent=2)
            + "\nRequired closure ids:\n"
            + "\n".join(
                f"- LEDGER-{item['id']}: {item.get('message', '')}"
                for item in prior_issues if item.get("id")
            )
            + "\nFor every prior issue, the global-contradictions scope must add "
              "exactly one checks_performed entry beginning with "
              "`LEDGER-<id> PASS:` or `LEDGER-<id> FAIL:` and cite the current "
              "candidate evidence. Other scopes should recheck relevant prior issues."
            if prior_issues else
            "\n\n## Prior unresolved issue ledger\n[none]"
        )
        + "\n\n## Candidate changes since previous verification\n"
        + candidate_diff
        + (
            "\n\n## Completed specialist reports for global reconciliation\n"
            + json.dumps(
                [item.to_dict() for item in upstream_reports],
                ensure_ascii=False,
                indent=2,
            )
            if upstream_reports else ""
        )
        + "\n\nInspect only the files and claims needed for this assignment, "
          "but complete every mandatory checklist item even after finding a blocking "
          "defect. In checks_performed, begin each checklist result with its exact "
          "id followed by PASS or FAIL and concrete evidence. Treat each defect as "
          "an issue family and inspect every dependent occurrence before submitting "
          "one structured fragment."
    )
    agent.run(task)
    if not submitted:
        _recover_scope_submission(
            cfg=cfg,
            scope=scope,
            agent=agent,
            registry=registry,
            ctx=ctx,
            submitted=submitted,
            prior_issues=prior_issues,
            emit=emit,
        )
    if submitted:
        fragment = submitted[-1]
    else:
        fragment = _fallback_scope_fragment(agent, scope)
        _ensure_fragment_checklist(fragment, scope, prior_issues)
    fragment.tokens = agent.total_usage.total_tokens
    emit("verification_progress", {
        "phase": "subcheck_complete",
        "role": "subagent",
        "scope_id": scope.id,
        "scope_title": scope.title,
        "verdict": fragment.verdict,
        "summary": fragment.summary[:1000],
        "issue_count": len(fragment.issues),
        "total_tokens": fragment.tokens,
    })
    return fragment


def _fallback_synthesis(
    fragments: list[VerificationFragment],
    attempt: int,
) -> VerificationReport:
    issues = [
        issue
        for fragment in fragments
        for issue in fragment.issues
    ]
    all_pass = bool(fragments) and all(
        fragment.verdict == "PASS"
        and not any(
            issue.severity in {"critical", "major"}
            for issue in fragment.issues
        )
        for fragment in fragments
    )
    if all_pass:
        return VerificationReport(
            "PASS",
            "All parallel verification scopes returned structured PASS verdicts.",
            issues,
            attempt,
        )
    unresolved = ", ".join(
        fragment.scope_title
        for fragment in fragments
        if fragment.verdict != "PASS"
    ) or "unknown scope"
    return VerificationReport(
        "REVISE",
        f"Parallel verification left unresolved checks in: {unresolved}.",
        issues,
        attempt,
    )


def _verify_candidate_parallel_legacy(
    cfg: dict[str, Any],
    workdir: Path,
    candidate: str,
    attempt: int,
    stop_event,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> VerificationReport:
    """Run lead triage, parallel scoped checks, and one unified synthesis."""
    static_issues = _preflight(workdir, cfg)
    verification_dir = workdir / "verification"
    verification_dir.mkdir(exist_ok=True)
    ledger_history = _load_issue_ledger(verification_dir)
    prior_issues = [
        item for item in ledger_history if item.get("status") != "resolved"
    ]
    candidate_diff = _candidate_diff_packet(
        workdir,
        verification_dir,
        attempt,
    )
    verification_cfg = cfg.get("verification", {})
    worker_count = max(
        3,
        min(5, int(verification_cfg.get(
            "parallel_workers", DEFAULT_VERIFIER_WORKERS
        ))),
    )
    synthesis_submitted: list[VerificationReport] = []
    event_lock = threading.Lock()

    def emit(kind: str, data: dict[str, Any]) -> None:
        if on_event is None:
            return
        payload = {"attempt": attempt, **data}
        # Scoped agents finish on different threads. Serialize dashboard writes
        # so a JSONL event sink never receives interleaved records.
        with event_lock:
            on_event(kind, payload)

    deterministic_metrics = _deterministic_metrics_packet(workdir, cfg)
    blocking_static = [
        issue for issue in static_issues if issue.severity == "critical"
    ]
    if blocking_static:
        verification_usage = {
            "triage_tokens": 0,
            "scope_tokens": 0,
            "synthesis_tokens": 0,
            "reported_total_tokens": 0,
            "model_verification_skipped": True,
        }
        report = VerificationReport(
            verdict="REVISE",
            summary=(
                "Deterministic preflight found a critical artifact defect. "
                "Expensive model-based verification was skipped until the "
                "candidate is structurally valid."
            ),
            issues=_deduplicate_issues(static_issues),
            attempt=attempt,
            verification_usage=verification_usage,
        )
        scopes = _default_scopes()
        fragments: list[VerificationFragment] = []
        emit("verification_progress", {
            "phase": "preflight_blocked",
            "role": "deterministic-preflight",
            "issue_count": len(report.issues),
            "metrics": deterministic_metrics,
        })
        issue_ledger = _write_issue_ledger(
            verification_dir,
            attempt,
            ledger_history,
            report,
            fragments,
        )
        _snapshot_candidate(workdir, verification_dir, attempt)
        payload = {
            **report.to_dict(),
            "verification_plan": [scope.to_dict() for scope in scopes],
            "scope_reports": [],
            "issue_ledger": issue_ledger,
            "deterministic_metrics": deterministic_metrics,
            "verification_usage": verification_usage,
        }
        (verification_dir / f"report_attempt_{attempt}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2)
        )
        (verification_dir / "report.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2)
        )
        return report

    synthesis_tokens = 0
    with tempfile.TemporaryDirectory(prefix="attempt-", dir=verification_dir) as tmp:
        attempt_root = Path(tmp)
        triage_workspace = attempt_root / "lead"
        triage_workspace.mkdir()
        _copy_artifacts(workdir, triage_workspace)
        scopes, triage_tokens = _run_triage(
            cfg,
            workdir,
            triage_workspace,
            candidate,
            static_issues,
            stop_event,
            emit,
            worker_count,
        )
        total_step_budget = max(
            1,
            int(verification_cfg.get("max_steps", 16)),
        )
        parallel_scopes = [
            scope for scope in scopes if scope.id != "global-contradictions"
        ]
        global_scope = next(
            (scope for scope in scopes if scope.id == "global-contradictions"),
            None,
        )
        global_step_budget = max(
            VERIFIER_FINALIZATION_STEPS + 2,
            int(verification_cfg.get("global_check_steps", 24)),
        )
        parallel_total_budget = max(
            len(parallel_scopes) * (VERIFIER_FINALIZATION_STEPS + 2),
            total_step_budget - global_step_budget,
        )
        scoped_step_budget = max(
            VERIFIER_FINALIZATION_STEPS + 2,
            int(verification_cfg.get(
                "subagent_max_steps",
                (
                    parallel_total_budget + len(parallel_scopes) - 1
                ) // max(1, len(parallel_scopes)),
            )),
        )

        fragments_by_id: dict[str, VerificationFragment] = {}
        workers_root = attempt_root / "workers"
        workers_root.mkdir()
        with ThreadPoolExecutor(
            max_workers=min(worker_count, len(parallel_scopes)),
            thread_name_prefix="verification",
        ) as executor:
            future_scope = {
                executor.submit(
                    _run_scope_verifier,
                    cfg,
                    workdir,
                    workers_root / scope.id,
                    candidate,
                    static_issues,
                    scope,
                    prior_issues,
                    candidate_diff,
                    scoped_step_budget,
                    stop_event,
                    emit,
                ): scope
                for scope in parallel_scopes
            }
            for future in as_completed(future_scope):
                scope = future_scope[future]
                try:
                    fragment = future.result()
                except Exception as exc:
                    fragment = VerificationFragment(
                        scope_id=scope.id,
                        scope_title=scope.title,
                        verdict="INCONCLUSIVE",
                        summary=(
                            "The scoped verifier failed before returning a verdict."
                        ),
                        checks_performed=[],
                        issues=[VerificationIssue(
                            "major",
                            "verification-runtime",
                            f"{scope.title} verification failed.",
                            f"{type(exc).__name__}: {exc}",
                            "Repair or rerun this scoped verifier before approval.",
                        )],
                    )
                    emit("verification_progress", {
                        "phase": "subcheck_complete",
                        "role": "subagent",
                        "scope_id": scope.id,
                        "scope_title": scope.title,
                        "verdict": fragment.verdict,
                        "summary": fragment.summary,
                        "issue_count": len(fragment.issues),
                    })
                fragments_by_id[scope.id] = fragment

        # The global contradiction audit runs only after all four mandatory
        # specialists finish, and receives their structured reports as evidence.
        specialist_fragments = [
            fragments_by_id[scope.id] for scope in parallel_scopes
        ]
        if global_scope is not None:
            try:
                fragments_by_id[global_scope.id] = _run_scope_verifier(
                    cfg=cfg,
                    source=workdir,
                    destination=workers_root / global_scope.id,
                    candidate=candidate,
                    static_issues=static_issues,
                    scope=global_scope,
                    prior_issues=prior_issues,
                    candidate_diff=candidate_diff,
                    max_steps=global_step_budget,
                    stop_event=stop_event,
                    emit=emit,
                    upstream_reports=specialist_fragments,
                )
            except Exception as exc:
                fragments_by_id[global_scope.id] = VerificationFragment(
                    scope_id=global_scope.id,
                    scope_title=global_scope.title,
                    verdict="INCONCLUSIVE",
                    summary="The global contradiction audit failed.",
                    checks_performed=[],
                    issues=[VerificationIssue(
                        "major",
                        "verification-runtime",
                        "The global contradiction audit failed.",
                        f"{type(exc).__name__}: {exc}",
                        "Repair and rerun the global audit before approval.",
                    )],
                )
                emit("verification_progress", {
                    "phase": "subcheck_complete",
                    "role": "subagent",
                    "scope_id": global_scope.id,
                    "scope_title": global_scope.title,
                    "verdict": "INCONCLUSIVE",
                    "summary": "The global contradiction audit failed.",
                    "issue_count": 1,
                })

        # Preserve fixed scope order independent of concurrent completion order.
        fragments = [fragments_by_id[scope.id] for scope in scopes]
        emit("verification_progress", {
            "phase": "synthesis_start",
            "role": "lead-synthesis",
            "completed_scopes": [
                {
                    "id": fragment.scope_id,
                    "title": fragment.scope_title,
                    "verdict": fragment.verdict,
                }
                for fragment in fragments
            ],
        })

        synthesis_workspace = attempt_root / "synthesis"
        synthesis_workspace.mkdir()
        registry = ToolRegistry()

        def submit(_ctx: ToolContext, args: dict[str, Any]) -> str:
            if synthesis_submitted:
                return "[error] A verification verdict has already been recorded."
            synthesis_submitted.append(VerificationReport(
                verdict=str(args.get("verdict", "REVISE")).upper(),
                summary=str(args.get("summary", "")),
                issues=[
                    _issue_from_dict(item)
                    for item in args.get("issues", [])
                    if isinstance(item, dict)
                ],
                attempt=attempt,
            ))
            return "Unified verification verdict recorded."

        registry.register(Tool(
            name="submit_verification",
            description=(
                "Submit one unified verdict after reconciling all scoped results. "
                "Do not omit or waive unresolved material findings."
            ),
            parameters=_SUBMIT_PARAMS,
            handler=submit,
        ))
        synthesis_steps = max(
            1,
            int(verification_cfg.get(
                "synthesis_max_steps", DEFAULT_VERIFIER_SYNTHESIS_STEPS
            )),
        )
        verifier_cfg = {
            **cfg,
            **verification_cfg.get("provider_config", {}),
        }
        agent = Agent(
            provider=build_provider(verifier_cfg),
            registry=registry,
            ctx=ToolContext(
                workdir=synthesis_workspace,
                sandbox=build_sandbox(verifier_cfg, synthesis_workspace),
                scope=f"verify{attempt}_synthesis_",
                stop_event=stop_event,
                settings=cfg,
            ),
            system_prompt=(
                effective_verifier_system_prompt(workdir)
                + "\n\nYou are the lead verification coordinator in the SYNTHESIS "
                  "stage. Reconcile the structured scoped reports into one concise "
                  "verdict for the lead modeling Agent. Deduplicate overlapping "
                  "issues, retain concrete evidence and actionable fixes, and do not "
                  "weaken a material scoped finding. A missing or INCONCLUSIVE "
                  "mandatory scope prevents PASS."
            ),
            compact_threshold_tokens=cfg["context"]["compact_threshold_tokens"],
            max_steps=synthesis_steps,
            on_event=_record_agent_progress(emit, role="lead-synthesis"),
            include_planning_memory=False,
            finalization_tool="submit_verification",
            finalization_steps=synthesis_steps,
            finalization_instruction=(
                "[Lead-verifier synthesis boundary]\n"
                "All scoped evidence has been collected. Call submit_verification "
                "now with the single unified PASS or REVISE verdict. Plain text is "
                "not accepted."
            ),
        )
        agent.run(
            "Synthesize one final verification verdict from this evidence.\n\n"
            "## Deterministic preflight\n"
            + json.dumps(
                [asdict(issue) for issue in static_issues],
                ensure_ascii=False,
                indent=2,
            )
            + "\n\n## Authoritative deterministic paper metrics\n"
            + json.dumps(
                deterministic_metrics,
                ensure_ascii=False,
                indent=2,
            )
            + "\n\n## Lead verification plan\n"
            + json.dumps(
                [scope.to_dict() for scope in scopes],
                ensure_ascii=False,
                indent=2,
            )
            + "\n\n## Parallel scoped reports\n"
            + json.dumps(
                [fragment.to_dict() for fragment in fragments],
                ensure_ascii=False,
                indent=2,
            )
            + "\n\nCall submit_verification exactly once."
        )
        synthesis_tokens = agent.total_usage.total_tokens

    if synthesis_submitted:
        # The lead synthesis Agent is the sole authority for reconciling scoped
        # reports. In particular, a structured PASS is a terminal verdict: do
        # not reopen or re-expand the raw subagent findings after synthesis.
        report = synthesis_submitted[-1]
    else:
        # The scoped reports are already structured, so even a provider-level
        # synthesis failure still yields a machine-readable conservative verdict.
        report = _fallback_synthesis(fragments, attempt)

    # Deterministic critical/major failures cannot be waived by the language model.
    if static_issues:
        for issue in static_issues:
            existing = next(
                (
                    candidate for candidate in report.issues
                    if _same_issue_family(candidate, issue)
                ),
                None,
            )
            if existing is None:
                report.issues.append(issue)
            else:
                _merge_issue_details(existing, issue)
        report.issues = _deduplicate_issues(report.issues)
        report.verdict = "REVISE"
        report.summary = (
            "Deterministic preflight checks found unresolved problems.\n\n"
            + report.summary
        )

    issue_ledger = _write_issue_ledger(
        verification_dir,
        attempt,
        ledger_history,
        report,
        fragments,
    )
    _snapshot_candidate(workdir, verification_dir, attempt)
    verification_usage = {
        "triage_tokens": triage_tokens,
        "scope_tokens": sum(fragment.tokens for fragment in fragments),
        "synthesis_tokens": synthesis_tokens,
        "reported_total_tokens": (
            triage_tokens
            + sum(fragment.tokens for fragment in fragments)
            + synthesis_tokens
        ),
        "model_verification_skipped": False,
    }
    report.verification_usage = verification_usage
    payload = {
        **report.to_dict(),
        "verification_plan": [scope.to_dict() for scope in scopes],
        "scope_reports": [fragment.to_dict() for fragment in fragments],
        "issue_ledger": issue_ledger,
        "deterministic_metrics": deterministic_metrics,
        "verification_usage": verification_usage,
    }
    (verification_dir / f"report_attempt_{attempt}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2)
    )
    (verification_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2)
    )
    return report


def verify_candidate(
    cfg: dict[str, Any],
    workdir: Path,
    candidate: str,
    attempt: int,
    stop_event,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> VerificationReport:
    """Run one independent verifier over the complete candidate workspace."""
    static_issues = _preflight(workdir, cfg)
    verification_dir = workdir / "verification"
    verification_dir.mkdir(exist_ok=True)
    ledger_history = _load_issue_ledger(verification_dir)
    prior_issues = [
        item for item in ledger_history if item.get("status") != "resolved"
    ]
    candidate_diff = _candidate_diff_packet(
        workdir,
        verification_dir,
        attempt,
    )
    verification_cfg = cfg.get("verification", {})

    def emit(kind: str, data: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(kind, {"attempt": attempt, **data})

    deterministic_metrics = _deterministic_metrics_packet(workdir, cfg)
    blocking_static = [
        issue for issue in static_issues if issue.severity == "critical"
    ]
    if blocking_static:
        verification_usage = {
            "triage_tokens": 0,
            "scope_tokens": 0,
            "synthesis_tokens": 0,
            "reported_total_tokens": 0,
            "model_verification_skipped": True,
        }
        report = VerificationReport(
            verdict="REVISE",
            summary=(
                "Deterministic preflight found a critical artifact defect. "
                "Model-based verification was skipped until the candidate is "
                "structurally valid."
            ),
            issues=_deduplicate_issues(static_issues),
            attempt=attempt,
            verification_usage=verification_usage,
        )
        emit("verification_progress", {
            "phase": "preflight_blocked",
            "role": "deterministic-preflight",
            "issue_count": len(report.issues),
            "metrics": deterministic_metrics,
        })
        issue_ledger = _write_issue_ledger(
            verification_dir,
            attempt,
            ledger_history,
            report,
            [],
        )
        _snapshot_candidate(workdir, verification_dir, attempt)
        payload = {
            **report.to_dict(),
            "verification_plan": [],
            "scope_reports": [],
            "issue_ledger": issue_ledger,
            "deterministic_metrics": deterministic_metrics,
            "verification_usage": verification_usage,
        }
        (verification_dir / f"report_attempt_{attempt}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2)
        )
        (verification_dir / "report.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2)
        )
        return report

    submitted: list[VerificationReport] = []
    agent_tokens = 0
    with tempfile.TemporaryDirectory(prefix="attempt-", dir=verification_dir) as tmp:
        isolated = Path(tmp) / "verifier"
        isolated.mkdir()
        _copy_artifacts(workdir, isolated)
        registry = _inspection_registry()

        def submit(_ctx: ToolContext, args: dict[str, Any]) -> str:
            if submitted:
                return "[error] A verification verdict has already been recorded."
            verdict = str(args.get("verdict", "REVISE")).upper()
            if verdict not in {"PASS", "REVISE"}:
                verdict = "REVISE"
            issues = [
                _issue_from_dict(item)
                for item in args.get("issues", [])
                if isinstance(item, dict)
            ]
            if verdict == "REVISE" and not issues:
                issues.append(VerificationIssue(
                    "major",
                    "verification",
                    "The verifier rejected the candidate without a structured issue.",
                    str(args.get("summary", "No evidence was supplied.")),
                    "Record the concrete defect, evidence, and required correction.",
                ))
            if any(
                issue.severity in {"critical", "major"} for issue in issues
            ):
                verdict = "REVISE"
            submitted.append(VerificationReport(
                verdict=verdict,
                summary=str(args.get("summary", "")).strip(),
                issues=_deduplicate_issues(issues),
                attempt=attempt,
            ))
            return "Verification verdict recorded."

        registry.register(Tool(
            name="submit_verification",
            description=(
                "Submit the single final verdict after independently inspecting "
                "the complete modeling workspace."
            ),
            parameters=_SUBMIT_PARAMS,
            handler=submit,
        ))
        verifier_cfg = {
            **cfg,
            **verification_cfg.get("provider_config", {}),
        }
        max_steps = max(
            VERIFIER_FINALIZATION_STEPS + 1,
            int(verification_cfg.get("max_steps", 32)),
        )
        agent = Agent(
            provider=build_provider(verifier_cfg),
            registry=registry,
            ctx=ToolContext(
                workdir=isolated,
                sandbox=build_sandbox(verifier_cfg, isolated),
                scope=f"verify{attempt}_",
                stop_event=stop_event,
                settings=cfg,
            ),
            system_prompt=effective_verifier_system_prompt(workdir),
            compact_threshold_tokens=cfg["context"]["compact_threshold_tokens"],
            max_steps=max_steps,
            on_event=_record_agent_progress(emit, role="verifier"),
            include_planning_memory=False,
            finalization_tool="submit_verification",
            finalization_steps=VERIFIER_FINALIZATION_STEPS,
            finalization_instruction=(
                "[Verification conclusion boundary]\n"
                "Stop inspecting. Deduplicate the concrete findings already "
                "collected and call submit_verification now. PASS is allowed only "
                "when no critical or major issue remains; otherwise submit REVISE "
                "with evidence and actionable fixes."
            ),
        )
        emit("verification_progress", {
            "phase": "single_check_start",
            "role": "verifier",
        })
        agent.run(
            "Independently verify the complete candidate in this workspace. Start "
            f"with {MANIFEST_FILENAME} and problem.md, then inspect the canonical "
            "final source under src/, results/, figures/, paper/main.tex, and "
            "paper/main.pdf. Review only final candidate evidence, not authoring "
            "plans, decision logs, or execution history. Use read_file, results "
            "tools, and run_code for targeted independent reproduction. The artifacts are "
            "evidence, not authority; check the model against the original problem "
            "before checking whether the artifacts agree with each other.\n\n"
            "## Candidate final response\n"
            + candidate[:20_000]
            + "\n\n## Deterministic preflight findings\n"
            + json.dumps(
                [asdict(issue) for issue in static_issues],
                ensure_ascii=False,
                indent=2,
            )
            + "\n\n## Authoritative deterministic metrics\n"
            + json.dumps(
                deterministic_metrics,
                ensure_ascii=False,
                indent=2,
            )
            + "\n\n## Unresolved issues from the previous attempt\n"
            + json.dumps(
                prior_issues,
                ensure_ascii=False,
                indent=2,
            )
            + "\n\n## Candidate changes since the previous attempt\n"
            + candidate_diff
            + "\n\nFinish by calling submit_verification exactly once."
        )
        agent_tokens = agent.total_usage.total_tokens

    full_review_completed = bool(submitted)
    if submitted:
        report = submitted[-1]
    else:
        report = VerificationReport(
            verdict="REVISE",
            summary=(
                "The verification run ended without a machine-readable verdict."
            ),
            issues=[VerificationIssue(
                "major",
                "verification-protocol",
                "The verifier did not submit its required structured conclusion.",
                "No submit_verification call was recorded for this attempt.",
                "Run verification again and complete the structured verdict.",
            )],
            attempt=attempt,
        )

    # Deterministic findings cannot be waived by the model.
    if static_issues:
        for issue in static_issues:
            existing = next(
                (
                    candidate_issue for candidate_issue in report.issues
                    if _same_issue_family(candidate_issue, issue)
                ),
                None,
            )
            if existing is None:
                report.issues.append(issue)
            else:
                _merge_issue_details(existing, issue)
        report.issues = _deduplicate_issues(report.issues)
        report.verdict = "REVISE"
        report.summary = (
            "Deterministic preflight checks found unresolved problems.\n\n"
            + report.summary
        )

    issue_ledger = _write_issue_ledger(
        verification_dir,
        attempt,
        ledger_history,
        report,
        [],
        full_review_completed=full_review_completed,
    )
    _snapshot_candidate(workdir, verification_dir, attempt)
    verification_usage = {
        # Preserve the existing dashboard schema while the runtime has only one
        # verification stream.
        "triage_tokens": 0,
        "scope_tokens": agent_tokens,
        "synthesis_tokens": 0,
        "reported_total_tokens": agent_tokens,
        "usage": agent.total_usage.to_dict(),
        "model_verification_skipped": False,
    }
    report.verification_usage = verification_usage
    emit("verification_progress", {
        "phase": "single_check_complete",
        "role": "verifier",
        "verdict": report.verdict,
        "summary": report.summary,
        "issue_count": len(report.issues),
        "total_tokens": agent_tokens,
    })
    payload = {
        **report.to_dict(),
        "verification_plan": [{
            "id": "single-verifier",
            "title": "Independent verification Agent",
            "instructions": (
                "Read the complete candidate workspace, independently reproduce "
                "high-impact claims, and submit one deduplicated verdict."
            ),
            "rationale": (
                "One verifier avoids repeated generic risk reports and removes "
                "the need for a separate contradiction-synthesis pass."
            ),
        }],
        "scope_reports": [],
        "issue_ledger": issue_ledger,
        "deterministic_metrics": deterministic_metrics,
        "verification_usage": verification_usage,
    }
    (verification_dir / f"report_attempt_{attempt}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2)
    )
    (verification_dir / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2)
    )
    return report
