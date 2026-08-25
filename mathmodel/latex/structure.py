"""Deterministic structural rules for model-authored competition papers.

The rules deliberately constrain document regions and LaTeX hierarchy, not the
number or names of mathematical problems.  A writer remains free to choose any
top-level headings and any number of sections appropriate to the problem.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


_SECTION = re.compile(r"\\section\*?\s*\{([^{}]*)\}")
_SECTION_COMMAND = re.compile(r"\\section\*?\s*\{")
_SUBSECTION_COMMAND = re.compile(r"\\subsection\*?\s*\{")
_BIBLIOGRAPHY_BEGIN = re.compile(r"\\begin\s*\{thebibliography\}")
_BIBLIOGRAPHY_END = re.compile(r"\\end\s*\{thebibliography\}")
_APPENDIX = re.compile(r"\\appendix\b")
_REFERENCE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9:._-]*$")
_FORBIDDEN_BODY_COMMANDS = (
    (re.compile(r"\\begin\s*\{document\}"), "document environment"),
    (re.compile(r"\\end\s*\{document\}"), "document environment"),
    (_BIBLIOGRAPHY_BEGIN, "bibliography environment"),
    (_BIBLIOGRAPHY_END, "bibliography environment"),
    (_APPENDIX, "appendix boundary"),
)
_PLACEHOLDER_HEADINGS = {
    "test",
    "untitled",
    "paper",
    "report",
    "body",
    "正文",
    "论文",
    "测试",
    "未命名",
}


def _plain_heading(value: object) -> str:
    text = re.sub(r"\\[A-Za-z@]+\*?", "", str(value or ""))
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", text).casefold()


def _region_items(context: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    value = context.get(name, [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def validate_document_regions(context: Mapping[str, Any]) -> list[str]:
    """Validate flexible main/appendix/reference input before rendering."""
    failures: list[str] = []
    sections = _region_items(context, "sections")
    appendices = _region_items(context, "appendices")
    references = _region_items(context, "references")

    if not sections:
        failures.append("sections must contain at least one main-paper section")

    for region_name, items in (("sections", sections), ("appendices", appendices)):
        seen: set[str] = set()
        for index, item in enumerate(items, 1):
            heading = str(item.get("heading") or "").strip()
            body = str(item.get("body") or "")
            plain = _plain_heading(heading)
            label = f"{region_name}[{index}]"
            if not heading:
                failures.append(f"{label}.heading must not be empty")
            elif plain in _PLACEHOLDER_HEADINGS:
                failures.append(
                    f"{label}.heading is a placeholder ({heading!r}); use a "
                    "descriptive, problem-specific title"
                )
            elif plain in seen:
                failures.append(
                    f"{region_name} contains duplicate top-level heading {heading!r}"
                )
            seen.add(plain)

            if not body.strip():
                failures.append(f"{label}.body must not be empty")
            if _SECTION_COMMAND.search(body):
                failures.append(
                    f"{label}.body contains \\section; each top-level section must "
                    f"be a separate {region_name} array item"
                )
            for pattern, description in _FORBIDDEN_BODY_COMMANDS:
                if pattern.search(body):
                    failures.append(
                        f"{label}.body contains a template-owned {description}; "
                        "use appendices/references fields instead"
                    )

    if len(sections) == 1:
        subsection_count = len(_SUBSECTION_COMMAND.findall(
            str(sections[0].get("body") or "")
        ))
        if subsection_count >= 4:
            failures.append(
                "the paper has one top-level section containing "
                f"{subsection_count} subsections; split the independent chapters "
                "into separate sections array items"
            )

    seen_keys: set[str] = set()
    for index, item in enumerate(references, 1):
        key = str(item.get("key") or "").strip()
        text = str(item.get("text") or "").strip()
        label = f"references[{index}]"
        if not _REFERENCE_KEY.fullmatch(key):
            failures.append(
                f"{label}.key must start with a letter and contain only letters, "
                "digits, colon, dot, underscore, or hyphen"
            )
        elif key in seen_keys:
            failures.append(f"references contains duplicate key {key!r}")
        seen_keys.add(key)
        if not text:
            failures.append(f"{label}.text must not be empty")
        if re.search(r"\\bibitem\s*\{", text):
            failures.append(
                f"{label}.text must contain only the reference text; the template "
                "creates \\bibitem"
            )

    return failures


def inspect_latex_structure(tex: str) -> list[str]:
    """Check hierarchy and document-region order in rendered LaTeX source."""
    failures: list[str] = []
    bibliography_begins = list(_BIBLIOGRAPHY_BEGIN.finditer(tex))
    bibliography_ends = list(_BIBLIOGRAPHY_END.finditer(tex))
    appendices = list(_APPENDIX.finditer(tex))

    if len(bibliography_begins) > 1 or len(bibliography_ends) > 1:
        failures.append("the paper contains more than one bibliography environment")
    if len(appendices) > 1:
        failures.append("the paper contains more than one appendix boundary")
    if bibliography_begins and appendices:
        if bibliography_begins[0].start() < appendices[0].start():
            failures.append("references must appear after all appendices")

    if bibliography_begins and bibliography_ends:
        if bibliography_ends[0].start() < bibliography_begins[0].start():
            failures.append("the bibliography environment is out of order")
        else:
            tail = tex[bibliography_ends[0].end():]
            tail = re.sub(r"(?m)^\s*%.*$", "", tail)
            tail = tail.replace(r"\end{document}", "").strip()
            if tail:
                failures.append("references must be the final document region")

    main_end = min(
        [match.start() for match in (*appendices, *bibliography_begins)]
        or [len(tex)]
    )
    main_tex = tex[:main_end]
    main_sections = [
        heading for heading in _SECTION.findall(main_tex)
        if _plain_heading(heading)
    ]
    if len(main_sections) == 1:
        subsection_count = len(_SUBSECTION_COMMAND.findall(main_tex))
        if subsection_count >= 4:
            failures.append(
                "rendered paper has one top-level section containing "
                f"{subsection_count} subsections"
            )
    for heading in main_sections:
        if _plain_heading(heading) in _PLACEHOLDER_HEADINGS:
            failures.append(
                f"rendered paper contains placeholder top-level heading {heading!r}"
            )
    return failures
