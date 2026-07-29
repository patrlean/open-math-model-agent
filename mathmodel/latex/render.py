"""Render a LaTeX document from a pluggable template + a context dict.

Jinja2 is configured with LaTeX-safe delimiters (\\VAR{}, \\BLOCK{}) so template
files stay valid-ish TeX and don't collide with LaTeX's own braces.

Traceability hook: `results/*.json` is loaded into the context as `results`, and
each section body is itself rendered against that context. So a writer can put
\\VAR{results['fit']['a']} in prose and it resolves to the actually-computed
value -- numbers come from execution, not from the model retyping them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES_ROOT = Path(__file__).resolve().parent.parent.parent / "templates"

_CHINESE_NUMERALS = "零〇一二三四五六七八九十百千"
_MANUAL_HEADING_PREFIXES = (
    # 2.5, 5.2.1, 2.5、 and similar subsection/subsubsection prefixes.
    re.compile(r"^\s*\d+(?:\.\d+)+(?:\s*[、．.:：\-]\s*)?\s*"),
    # Top-level Arabic numbering such as "2. Problem Analysis" or "2、问题分析".
    re.compile(r"^\s*\d{1,3}(?:\s*[、．.:：\-]\s*|\s+)(?=\S)"),
    # Chinese numbering such as "二、问题分析".
    re.compile(rf"^\s*[{_CHINESE_NUMERALS}]+\s*[、．.:：\-]\s*"),
    # Chapter-style numbering such as "第二章 模型建立".
    re.compile(rf"^\s*第\s*[0-9{_CHINESE_NUMERALS}]+\s*[章节篇部分]\s*[、．.:：\-]?\s*"),
)
_SECTION_COMMAND = re.compile(r"\\(?:sub)*section\*?\{")


def _percent_is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def escape_unescaped_percent_literals(text: str) -> str:
    """Escape model-authored percent signs without rewriting TeX comment lines.

    A bare ``%`` comments out the rest of a LaTeX source line, which can silently
    truncate an abstract or paragraph while still producing a valid PDF. Whole
    lines beginning with ``%`` remain available for template/source comments;
    every other unescaped percent sign is treated as visible prose and normalized
    to ``\\%``.
    """
    normalized: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("%"):
            normalized.append(line)
            continue
        pieces: list[str] = []
        for index, char in enumerate(line):
            if char == "%" and not _percent_is_escaped(line, index):
                pieces.append("\\")
            pieces.append(char)
        normalized.append("".join(pieces))
    return "".join(normalized)


def find_unescaped_percent_lines(text: str) -> list[str]:
    """Return document-body lines whose bare percent would hide visible text."""
    document_start = text.find(r"\begin{document}")
    body = text[document_start:] if document_start >= 0 else text
    first_line = text[:document_start].count("\n") + 1 if document_start >= 0 else 1
    findings: list[str] = []
    for offset, line in enumerate(body.splitlines()):
        if line.lstrip().startswith("%"):
            continue
        if any(
            char == "%" and not _percent_is_escaped(line, index)
            for index, char in enumerate(line)
        ):
            excerpt = line.strip()
            if len(excerpt) > 180:
                excerpt = excerpt[:177] + "..."
            findings.append(f"line {first_line + offset}: {excerpt}")
    return findings


def strip_manual_heading_number(heading: str) -> str:
    """Remove numbering that LaTeX section commands generate automatically.

    The loop intentionally handles duplicated input such as
    ``2.5 2.5 最终思路`` as well as the normal ``2.5 最终思路``.
    """
    cleaned = heading.strip()
    while cleaned:
        for pattern in _MANUAL_HEADING_PREFIXES:
            updated = pattern.sub("", cleaned, count=1).strip()
            if updated != cleaned:
                cleaned = updated
                break
        else:
            break
    return cleaned or heading.strip()


def normalize_latex_section_headings(text: str) -> str:
    """Strip manual numbers inside section commands, including nested braces."""
    pieces: list[str] = []
    cursor = 0
    while match := _SECTION_COMMAND.search(text, cursor):
        open_brace = match.end() - 1
        depth = 1
        index = open_brace + 1
        while index < len(text) and depth:
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
            index += 1
        if depth:
            break
        heading = text[open_brace + 1:index - 1]
        pieces.append(text[cursor:open_brace + 1])
        pieces.append(strip_manual_heading_number(heading))
        pieces.append("}")
        cursor = index
    pieces.append(text[cursor:])
    return "".join(pieces)


def _env(template_dir: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(template_dir)),
        block_start_string=r"\BLOCK{",
        block_end_string="}",
        variable_start_string=r"\VAR{",
        variable_end_string="}",
        comment_start_string=r"\#{",
        comment_end_string="}",
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
        undefined=StrictUndefined,
    )


def load_results(workdir: Path) -> dict[str, Any]:
    """Load every results/*.json into a dict keyed by filename stem."""
    out: dict[str, Any] = {}
    rdir = workdir / "results"
    if rdir.is_dir():
        for p in sorted(rdir.glob("*.json")):
            try:
                out[p.stem] = json.loads(p.read_text())
            except json.JSONDecodeError:
                pass
    return out


def render_latex_fragment(
    fragment: str,
    workdir: Path,
    template: str = "generic",
) -> str:
    """Render one model-authored LaTeX fragment with the paper's conventions.

    ``write_paper`` already resolves ``\\VAR{results[...]}`` placeholders and
    removes manual section numbering. Localized edits must take the same path;
    otherwise a later ``edit_paragraph`` call can reintroduce unresolved result
    references or duplicated heading numbers into an otherwise normalized paper.
    """
    env = _env(TEMPLATES_ROOT / template)
    rendered = env.from_string(fragment).render(results=load_results(workdir))
    return escape_unescaped_percent_literals(
        normalize_latex_section_headings(rendered)
    )


def render_report(
    context: dict[str, Any],
    workdir: Path,
    template: str = "generic",
    template_file: str = "report.tex.j2",
) -> str:
    """Render `templates/<template>/<template_file>` with `context` + results.

    `context` keys used by the generic template: title, author, date, abstract,
    keywords, cjk (bool), sections (list of {heading, body}).
    """
    template_dir = TEMPLATES_ROOT / template
    env = _env(template_dir)

    results = load_results(workdir)
    full_ctx = {"results": results, "author": "", "date": r"\today", "abstract": "",
                "keywords": "", "cjk": False, "sections": [], **context}

    # First pass: resolve \VAR{...} embedded inside each section body.
    rendered_sections = []
    for s in full_ctx.get("sections", []):
        body = env.from_string(s.get("body", "")).render(**full_ctx)
        rendered_sections.append({
            **s,
            "heading": strip_manual_heading_number(s.get("heading", "")),
            "body": normalize_latex_section_headings(body),
        })
    full_ctx["sections"] = rendered_sections
    if full_ctx.get("abstract"):
        full_ctx["abstract"] = env.from_string(full_ctx["abstract"]).render(**full_ctx)

    return escape_unescaped_percent_literals(
        env.get_template(template_file).render(**full_ctx)
    )
