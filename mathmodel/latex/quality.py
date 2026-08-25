"""Deterministic paper-quality checks shared by generation and verification."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import fitz


@dataclass
class PaperMetrics:
    page_count: int
    counted_page_count: int
    main_body_page_count: int
    appendix_page_count: int
    reference_page_count: int
    first_section_page: int | None
    appendix_start_page: int | None
    references_start_page: int | None
    abstract_fill_ratio: float
    abstract_cjk_chars: int
    abstract_english_words: int
    display_equation_count: int


_ABSTRACT = re.compile(
    r"\\begin\{abstract\}(.*?)\\end\{abstract\}",
    re.DOTALL,
)
_FIRST_SECTION = re.compile(r"\\section\*?\{([^{}]+)\}")
_APPENDIX = re.compile(r"\\appendix\b")
_REFERENCES_TOC_TITLE = re.compile(
    r"(?m)^%% MATHMODEL_REFERENCES_TOC_TITLE:\s*(.+?)\s*$"
)
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


def _bookmark_page(
    bookmarks: list[list[Any]],
    title: str,
    *,
    prefer_last: bool = False,
) -> int | None:
    wanted = _normalize_text(title)
    pages = [
        int(item[2])
        for item in bookmarks
        if len(item) >= 3 and _normalize_text(str(item[1])) == wanted
    ]
    if not pages:
        return None
    return pages[-1] if prefer_last else pages[0]


def selected_page_count(
    metrics: PaperMetrics,
    paper_cfg: Mapping[str, Any] | None,
) -> int:
    """Return the page count selected by the active competition profile."""
    metric = str((paper_cfg or {}).get("page_count_metric") or "total_pages")
    if metric == "counted_pages":
        return metrics.counted_page_count
    return metrics.page_count


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

    appendix_start_page: int | None = None
    appendix_match = _APPENDIX.search(tex)
    if appendix_match:
        appendix_section = _FIRST_SECTION.search(tex, appendix_match.end())
        if appendix_section:
            appendix_start_page = _bookmark_page(
                top_level_bookmarks,
                appendix_section.group(1),
                prefer_last=True,
            )

    references_start_page: int | None = None
    references_title = _REFERENCES_TOC_TITLE.search(tex)
    if references_title:
        references_start_page = _bookmark_page(
            top_level_bookmarks,
            references_title.group(1),
            prefer_last=True,
        )
    if references_start_page is None:
        for fallback_title in ("References", "参考文献"):
            references_start_page = _bookmark_page(
                top_level_bookmarks,
                fallback_title,
                prefer_last=True,
            )
            if references_start_page is not None:
                break

    boundary_pages = [
        page for page in (appendix_start_page, references_start_page)
        if page is not None
    ]
    counted_page_count = min(boundary_pages) - 1 if boundary_pages else len(doc)
    counted_page_count = max(0, counted_page_count)
    main_body_page_count = (
        max(0, counted_page_count - first_section_page + 1)
        if first_section_page is not None
        else 0
    )
    appendix_end_page = (
        references_start_page - 1
        if references_start_page is not None
        and appendix_start_page is not None
        and references_start_page > appendix_start_page
        else len(doc)
    )
    appendix_page_count = (
        max(0, appendix_end_page - appendix_start_page + 1)
        if appendix_start_page is not None
        else 0
    )
    reference_page_count = (
        max(0, len(doc) - references_start_page + 1)
        if references_start_page is not None
        else 0
    )

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
        counted_page_count=counted_page_count,
        main_body_page_count=main_body_page_count,
        appendix_page_count=appendix_page_count,
        reference_page_count=reference_page_count,
        first_section_page=first_section_page,
        appendix_start_page=appendix_start_page,
        references_start_page=references_start_page,
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
    return _find_non_english_plot_labels(extract_logged_sources(logs_dir))


def find_non_english_plot_labels_in_sources(source_dir: Path) -> list[str]:
    """Find Chinese Matplotlib labels in the canonical final Python source."""
    sources = [
        (path, path.read_text(errors="replace"))
        for path in sorted(source_dir.rglob("*.py"))
        if path.is_file()
    ] if source_dir.is_dir() else []
    return _find_non_english_plot_labels(sources)


def _find_non_english_plot_labels(
    sources: list[tuple[Path, str]],
) -> list[str]:
    findings: list[str] = []
    for path, source in sources:
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
