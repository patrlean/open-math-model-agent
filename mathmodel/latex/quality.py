"""Deterministic paper-quality checks shared by generation and verification."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import fitz


@dataclass
class PaperMetrics:
    page_count: int
    first_section_page: int | None
    abstract_fill_ratio: float
    abstract_cjk_chars: int
    abstract_english_words: int
    display_equation_count: int


_ABSTRACT = re.compile(
    r"\\begin\{abstract\}(.*?)\\end\{abstract\}",
    re.DOTALL,
)
_FIRST_SECTION = re.compile(r"\\section\*?\{([^{}]+)\}")
_DISPLAY_EQUATION = re.compile(
    r"\\begin\{(?:equation\*?|align\*?|gather\*?|multline\*?)\}|"
    r"\\\[|\$\$",
)
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_ENGLISH_WORD = re.compile(r"[A-Za-z]+(?:[-'][A-Za-z]+)*")
_LATEX_COMMAND = re.compile(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?")
_PLOT_TEXT_METHODS = {
    "title", "set_title", "xlabel", "set_xlabel", "ylabel", "set_ylabel",
    "suptitle", "text", "annotate", "clabel", "set_label",
}


def _plain_latex(text: str) -> str:
    text = re.sub(r"(?<!\\)%.*", "", text)
    text = _LATEX_COMMAND.sub("", text)
    return re.sub(r"[{}$~^_\\]", " ", text)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", _plain_latex(text))


def inspect_paper(pdf_path: Path, tex_path: Path) -> PaperMetrics:
    """Measure page count, abstract occupancy, and formula density."""
    tex = tex_path.read_text(errors="replace")
    abstract_match = _ABSTRACT.search(tex)
    abstract = _plain_latex(abstract_match.group(1) if abstract_match else "")
    section_match = _FIRST_SECTION.search(tex)
    first_heading = _normalize_text(section_match.group(1)) if section_match else ""

    doc = fitz.open(pdf_path)
    first_section_page: int | None = None
    # Hyperref emits reliable PDF bookmarks even when CJK glyph extraction from
    # the rendered page is garbled by subset fonts.
    top_level_bookmarks = [item for item in doc.get_toc() if item[0] == 1]
    if top_level_bookmarks:
        first_section_page = int(top_level_bookmarks[0][2])
    elif first_heading:
        for index, page in enumerate(doc):
            if first_heading in _normalize_text(page.get_text()):
                first_section_page = index + 1
                break

    fill_ratio = 0.0
    if len(doc):
        page = doc[0]
        text_blocks = [
            block for block in page.get_text("blocks")
            if (
                len(block) >= 5
                and str(block[4]).strip()
                # Ignore the automatic footer page number; otherwise even a
                # half-empty abstract falsely appears to fill 95% of the page.
                and not (
                    float(block[1]) > page.rect.height * 0.85
                    and re.fullmatch(
                        r"(?:\d+|[ivxlcdm]+)",
                        str(block[4]).strip(),
                        re.IGNORECASE,
                    )
                )
            )
        ]
        if text_blocks and page.rect.height:
            fill_ratio = max(float(block[3]) for block in text_blocks) / page.rect.height

    metrics = PaperMetrics(
        page_count=len(doc),
        first_section_page=first_section_page,
        abstract_fill_ratio=fill_ratio,
        abstract_cjk_chars=len(_CJK.findall(abstract)),
        abstract_english_words=len(_ENGLISH_WORD.findall(abstract)),
        display_equation_count=len(_DISPLAY_EQUATION.findall(tex)),
    )
    doc.close()
    return metrics


def extract_logged_sources(logs_dir: Path) -> list[tuple[Path, str]]:
    sources: list[tuple[Path, str]] = []
    if not logs_dir.is_dir():
        return sources
    for path in sorted(logs_dir.glob("run_*.log")):
        text = path.read_text(errors="replace")
        match = re.search(
            r"=== source ===\n(.*?)\n--- exit_code=",
            text,
            re.DOTALL,
        )
        if match:
            sources.append((path, match.group(1)))
    return sources


def find_non_english_plot_labels(logs_dir: Path) -> list[str]:
    """Find Chinese text literals used as Matplotlib labels in logged source."""
    findings: list[str] = []
    for path, source in extract_logged_sources(logs_dir):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            method = (
                node.func.attr if isinstance(node.func, ast.Attribute)
                else node.func.id if isinstance(node.func, ast.Name)
                else ""
            )
            candidates: list[ast.AST] = []
            if method in _PLOT_TEXT_METHODS:
                candidates.extend(node.args)
            candidates.extend(
                kw.value for kw in node.keywords
                if kw.arg in {"label", "labels"}
            )
            for candidate in candidates:
                if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
                    if _CJK.search(candidate.value):
                        findings.append(
                            f"{path.name}:{getattr(node, 'lineno', '?')} "
                            f"{method or 'plot'} -> {candidate.value!r}"
                        )
    return findings
