"""write_paper tool: render the LaTeX template with the agent's content, inject
real results, and compile to PDF.

The agent supplies title/abstract/sections. Section bodies may reference computed
values as \\VAR{results['<file>']['<key>']}; render_report substitutes the actual
numbers from results/*.json so the paper cannot cite a value that was not
computed. Compilation runs on the host (tectonic). On failure the compile log
tail is returned so the agent can fix its LaTeX.
"""

from __future__ import annotations

import json
import time

from ..latex.compile import compile_tex
from ..latex.quality import (
    find_non_english_plot_labels,
    inspect_paper,
    selected_page_count,
)
from ..latex.render import find_unescaped_percent_lines, render_report
from ..latex.structure import inspect_latex_structure, validate_document_regions
from ..paper_profile import active_paper_profile, resolve_paper_config
from .base import Tool, ToolContext, tail

_SECTION_BLOCK = {
    "type": "object",
    "properties": {
        "heading": {
            "type": "string",
            "description": (
                "Unnumbered, problem-specific title chosen freely by the writer. "
                "Do not include a chapter or section number."
            ),
        },
        "body": {
            "type": "string",
            "description": (
                "Substantive LaTeX body for this top-level region. It may contain "
                "subsection/subsubsection commands but never section, appendix, "
                "bibliography, document-class, or document-boundary commands."
            ),
        },
    },
    "required": ["heading", "body"],
}

_REFERENCE = {
    "type": "object",
    "properties": {
        "key": {
            "type": "string",
            "description": "Unique LaTeX citation key, for example smith2024.",
        },
        "text": {
            "type": "string",
            "description": (
                "Complete verified reference text without a bibitem command."
            ),
        },
    },
    "required": ["key", "text"],
}

_PARAMS = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "abstract": {
            "type": "string",
            "description": (
                "Full first-page competition abstract. For Chinese papers target "
                "approximately 800–1200 Chinese characters and cover every "
                "sub-problem's model, method, quantitative result, and validation."
            ),
        },
        "keywords": {"type": "string"},
        "cjk": {"type": "boolean", "description": "true to typeset Chinese."},
        "template": {
            "type": "string",
            "description": (
                "Compatibility override used only when no active competition "
                "writing skill has locked its own template."
            ),
        },
        "sections": {
            "type": "array",
            "description": (
                "Ordered main-paper sections of any count and with freely chosen "
                "headings. Each array item becomes exactly one numbered LaTeX "
                "section. Cite computed values with "
                "\\VAR{results['file']['key']}."
            ),
            "items": _SECTION_BLOCK,
        },
        "appendices": {
            "type": "array",
            "description": (
                "Optional appendices of any count. The template places them after "
                "the main paper and excludes them from counted competition pages."
            ),
            "items": _SECTION_BLOCK,
        },
        "references": {
            "type": "array",
            "description": (
                "Optional verified references. The template emits bibitems and "
                "always places this region last."
            ),
            "items": _REFERENCE,
        },
    },
    "required": ["title", "sections"],
}


def resolve_paper_template(ctx: ToolContext, args: dict) -> str:
    """Select the active skill template, with a generic compatibility fallback."""
    paper_cfg = resolve_paper_config(ctx.settings, ctx.workdir)
    active_profile = active_paper_profile(ctx.workdir)
    return str(
        (
            active_profile.get("paper", {}).get("template")
            if active_profile is not None
            else None
        )
        or args.get("template")
        or paper_cfg.get("template")
        or ctx.settings.get("template")
        or "generic"
    )


def paper_acceptance(
    ctx: ToolContext,
    pdf_path,
    tex_path,
    *,
    cjk: bool,
) -> tuple[str, list[str]]:
    """Run the shared post-compile paper checks used by all paper tools."""
    paper_cfg = resolve_paper_config(ctx.settings, ctx.workdir)
    target_pages = int(paper_cfg.get("target_pages", 20))
    min_pages = int(paper_cfg.get("min_pages", 17))
    max_pages = int(paper_cfg.get("max_pages", target_pages))
    min_cjk_chars = int(paper_cfg.get("abstract_cjk_min_chars", 800))
    min_english_words = int(paper_cfg.get("abstract_english_min_words", 450))
    min_fill = float(paper_cfg.get("abstract_fill_min_ratio", 0.72))
    min_equations = int(paper_cfg.get("min_display_equations", 12))
    metrics = inspect_paper(pdf_path, tex_path)
    accepted_page_count = selected_page_count(metrics, paper_cfg)
    page_count_metric = str(
        paper_cfg.get("page_count_metric") or "total_pages"
    )
    failures: list[str] = []
    bare_percent_lines = find_unescaped_percent_lines(
        tex_path.read_text(errors="replace")
    )

    if bare_percent_lines:
        failures.append(
            "paper source contains unescaped percent signs that comment out the "
            "rest of a prose line: " + "; ".join(bare_percent_lines[:8])
        )
    failures.extend(inspect_latex_structure(
        tex_path.read_text(errors="replace")
    ))
    if not min_pages <= accepted_page_count <= max_pages:
        failures.append(
            f"paper has {accepted_page_count} accepted-count pages using "
            f"{page_count_metric}; accepted range is {min_pages}-{max_pages} "
            f"(total PDF pages={metrics.page_count}, main-body pages="
            f"{metrics.main_body_page_count}, appendix pages="
            f"{metrics.appendix_page_count}, reference pages="
            f"{metrics.reference_page_count})"
        )
    if metrics.first_section_page != 2:
        failures.append(
            "the first numbered section must start on page 2, immediately "
            f"after the one-page abstract; detected page {metrics.first_section_page}"
        )
    if metrics.abstract_fill_ratio < min_fill:
        failures.append(
            "the abstract page is visibly under-filled "
            f"({metrics.abstract_fill_ratio:.0%}; minimum {min_fill:.0%})"
        )
    if cjk and metrics.abstract_cjk_chars < min_cjk_chars:
        failures.append(
            f"Chinese abstract has {metrics.abstract_cjk_chars} CJK characters; "
            f"write at least {min_cjk_chars}"
        )
    if not cjk and metrics.abstract_english_words < min_english_words:
        failures.append(
            f"English abstract has {metrics.abstract_english_words} words; "
            f"write at least {min_english_words}"
        )
    if metrics.display_equation_count < min_equations:
        failures.append(
            f"paper has only {metrics.display_equation_count} display equations; "
            f"the model derivation requires at least {min_equations}"
        )
    if paper_cfg.get("figures_english_only", True):
        bad_labels = find_non_english_plot_labels(ctx.workdir / "logs")
        if bad_labels:
            failures.append(
                "figure-generation source contains Chinese plot labels: "
                + "; ".join(bad_labels[:8])
            )

    metric_line = (
        f"pages={metrics.page_count}, counted_pages={metrics.counted_page_count}, "
        f"main_body_pages={metrics.main_body_page_count}, appendix_pages="
        f"{metrics.appendix_page_count}, reference_pages="
        f"{metrics.reference_page_count}, page_count_metric={page_count_metric}, "
        f"accepted_count={accepted_page_count}, target_pages={target_pages}, "
        f"accepted_pages={min_pages}-{max_pages}, first_section_page="
        f"{metrics.first_section_page}, abstract_fill="
        f"{metrics.abstract_fill_ratio:.0%}, display_equations="
        f"{metrics.display_equation_count}"
    )
    return metric_line, failures


def _write_paper(ctx: ToolContext, args: dict) -> str:
    paper_dir = ctx.workdir / "paper"
    paper_dir.mkdir(exist_ok=True)

    context = {
        "title": args["title"],
        "abstract": args.get("abstract", ""),
        "keywords": args.get("keywords", ""),
        "cjk": bool(args.get("cjk", False)),
        "sections": args["sections"],
        "appendices": args.get("appendices", []),
        "references": args.get("references", []),
    }
    structure_failures = validate_document_regions(context)
    if structure_failures:
        return (
            "[paper structure error]\n"
            + "\n".join(f"- {failure}" for failure in structure_failures)
        )

    paper_cfg = resolve_paper_config(ctx.settings, ctx.workdir)
    template = resolve_paper_template(ctx, args)

    try:
        tex = render_report(context, workdir=ctx.workdir, template=template)
    except Exception as e:
        # Most often a \VAR reference to a result key that does not exist.
        return f"[render error] {type(e).__name__}: {e}\n(check that referenced results exist via results_list)"

    rendered_structure_failures = inspect_latex_structure(tex)
    if rendered_structure_failures:
        return (
            "[paper structure error after render]\n"
            + "\n".join(
                f"- {failure}" for failure in rendered_structure_failures
            )
        )

    tex_path = paper_dir / "main.tex"
    pdf_path = paper_dir / "main.pdf"
    delivery_path = paper_dir / "delivery.json"
    protected_paths = (tex_path, pdf_path, delivery_path)
    previous = {
        path: path.read_bytes() if path.is_file() else None
        for path in protected_paths
    }
    baseline_metrics = None
    if tex_path.is_file() and pdf_path.is_file():
        try:
            baseline_metrics = inspect_paper(pdf_path, tex_path)
        except Exception:
            baseline_metrics = None

    if previous[tex_path] is not None:
        revision_dir = paper_dir / "revisions"
        revision_dir.mkdir(exist_ok=True)
        revision_index = ctx.next_index("paper_write")
        (revision_dir / f"main.before-write-{revision_index:03d}.tex").write_bytes(
            previous[tex_path] or b""
        )

    def restore_previous() -> None:
        for path, content in previous.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)

    tex_path.write_text(tex)

    res = compile_tex(tex_path)
    if res.ok:
        rel = res.pdf_path.relative_to(ctx.workdir)
        metric_line, failures = paper_acceptance(
            ctx,
            res.pdf_path,
            tex_path,
            cjk=context["cjk"],
        )
        if failures:
            revised_metrics = inspect_paper(res.pdf_path, tex_path)
            baseline_page_count = (
                selected_page_count(baseline_metrics, paper_cfg)
                if baseline_metrics is not None
                else None
            )
            revised_page_count = selected_page_count(revised_metrics, paper_cfg)
            severe_regression = bool(
                baseline_metrics is not None
                and (
                    (
                        baseline_page_count is not None
                        and baseline_page_count >= 5
                        and revised_page_count
                        < max(4, int(baseline_page_count * 0.6))
                    )
                    or (
                        baseline_metrics.display_equation_count >= 4
                        and revised_metrics.display_equation_count
                        < max(1, baseline_metrics.display_equation_count // 2)
                    )
                    or (
                        baseline_metrics.first_section_page == 2
                        and revised_metrics.first_section_page != 2
                    )
                )
            )
            if severe_regression:
                restore_previous()
                return (
                    "full-paper rewrite rejected and rolled back because it "
                    f"severely regressed the stable document ({metric_line}).\n"
                    + "\n".join(f"- {failure}" for failure in failures)
                    + "\nThe previous paper/main.tex and matching PDF were "
                      "restored. Continue from that stable source with "
                      "edit_paragraph."
                )
            return (
                f"compiled OK -> {rel}, but PAPER ACCEPTANCE FAILED ({metric_line}).\n"
                + "\n".join(f"- {failure}" for failure in failures)
                + "\nRevise substantive analysis, derivation, validation, tables, "
                  "and interpretation; do not pad with repetition. Use "
                  "edit_paragraph for localized defects. Call write_paper again "
                  "only when the paper needs a full structural rewrite."
            )
        generated_at = time.time()
        (paper_dir / "delivery.json").write_text(json.dumps({
            "title": args["title"],
            "generated_at": generated_at,
        }, ensure_ascii=False, indent=2))
        return (
            f"compiled and paper acceptance PASSED -> {rel} "
            f"({res.pdf_path.stat().st_size} bytes; {metric_line}). "
            "Source: paper/main.tex"
        )
    restore_previous()
    return (
        "compile FAILED; the previous paper/main.tex and matching PDF were "
        "restored. Fix the request or continue with edit_paragraph.\n"
        "--- log tail ---\n" + tail(res.log, max_lines=25)
    )


write_paper_tool = Tool(
    name="write_paper",
    description="Render the active competition template from flexible main "
    "sections, optional appendices, and final references, then compile it to "
    "PDF. Section names and counts are writer-defined; each sections item becomes "
    "one top-level section. Compilation is followed by hard hierarchy, region "
    "order, counted-page, abstract, equation-density, and figure-language checks.",
    parameters=_PARAMS,
    handler=_write_paper,
)
