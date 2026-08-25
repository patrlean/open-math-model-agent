"""Localized LaTeX paper editing with compilation and acceptance checks.

Unlike ``write_paper``, this tool never regenerates the document template. It
edits one explicitly selected block in ``paper/main.tex``, preserves a revision
backup, recompiles, and runs the same acceptance gate as the full paper writer.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from ..latex.compile import compile_tex
from ..latex.quality import inspect_paper, selected_page_count
from ..latex.render import render_latex_fragment
from ..latex.structure import inspect_latex_structure
from ..paper_profile import resolve_paper_config
from .base import Tool, ToolContext, tail
from .write_paper import paper_acceptance

_MAX_CONTENT_CHARS = 80_000
_HEADING_START = re.compile(
    r"\\(?P<kind>section|subsection|subsubsection)\*?\s*\{"
)
_LEVEL = {"section": 1, "subsection": 2, "subsubsection": 3}
_LAYOUT_COMMAND = re.compile(
    r"""\\(?:
        documentclass|usepackage|begin\s*\{document\}|end\s*\{document\}|
        title|author|date|maketitle|
        geometry|newgeometry|restoregeometry|
        pagestyle|thispagestyle|
        clearpage|newpage|pagebreak|
        setlength|addtolength|linespread|fontsize|
        newcommand|renewcommand|providecommand|def|
        input|include
    )(?![A-Za-z@])""",
    re.IGNORECASE | re.VERBOSE,
)
_LABEL = re.compile(r"\\label\{([^{}]+)\}")
_REFERENCE = re.compile(r"\\(?:eqref|ref|autoref)\{([^{}]+)\}")
_ANY_HEADING = re.compile(r"\\(section|subsection|subsubsection)\*?\s*\{")
_SEMANTIC_BLOCK = re.compile(
    r"""\\(?:
        begin\s*\{(?:equation\*?|align\*?|gather\*?|multline\*?|table\*?|figure\*?)\}|
        label\s*\{|includegraphics(?:\[[^\]]*\])?\s*\{
    )""",
    re.IGNORECASE | re.VERBOSE,
)
_DOUBLE_ESCAPED_COMMAND = re.compile(
    r"(?<!\\)\\\\(?=[A-Za-z@]+(?:\s*\{|\b))"
)
_DOUBLE_ESCAPED_SPECIAL = re.compile(r"(?<!\\)\\\\(?=[%&#_$])")
_FOUR_SLASH_LINE_END = re.compile(r"\\\\\\\\(?=\s*(?:\n|$))")

_EDIT_ITEM_PROPERTIES = {
    "target_type": {
        "type": "string",
        "enum": ["abstract", "section", "subsection", "subsubsection", "text"],
        "description": (
            "Block to edit. Omit when block_id identifies a structural block. "
            "Use text with block_id to limit an exact-text edit to that block."
        ),
    },
    "target": {
        "type": "string",
        "description": (
            "Heading title or existing text. Prefer block_id from "
            "inspect_paper_blocks for structural edits."
        ),
    },
    "block_id": {
        "type": "string",
        "description": (
            "Stable block identifier returned by inspect_paper_blocks. This is "
            "preferred over copying a heading or paragraph verbatim."
        ),
    },
    "expected_hash": {
        "type": "string",
        "description": (
            "Optional content hash returned by inspect_paper_blocks. The edit is "
            "rejected if that block changed since inspection."
        ),
    },
    "operation": {
        "type": "string",
        "enum": ["replace", "prepend", "append", "insert_before", "insert_after"],
    },
    "content": {
        "type": "string",
        "description": (
            "Replacement or inserted LaTeX. For a structural block this may be "
            "either body-only content or a complete matching outer heading; the "
            "matching outer heading is removed automatically."
        ),
    },
    "occurrence": {
        "type": "integer",
        "minimum": 1,
    },
}

_PARAMS = {
    "type": "object",
    "properties": {
        **_EDIT_ITEM_PROPERTIES,
        "edits": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "description": (
                "Apply several related edits as one transaction and compile once. "
                "Use this when one verifier issue affects a table, its analysis, "
                "the abstract, and the conclusion."
            ),
            "items": {
                "type": "object",
                "properties": _EDIT_ITEM_PROPERTIES,
                "required": ["operation", "content"],
            },
        },
    },
    "anyOf": [
        {"required": ["operation", "content"]},
        {"required": ["edits"]},
    ],
}


@dataclass(frozen=True)
class _Heading:
    kind: str
    title: str
    command_start: int
    body_start: int


@dataclass(frozen=True)
class _PaperBlock:
    block_id: str
    target_type: str
    title: str
    start: int
    end: int
    start_line: int
    end_line: int
    content_hash: str


def _matching_brace(text: str, open_index: int) -> int:
    depth = 0
    escaped = False
    for index in range(open_index, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("LaTeX heading has an unbalanced brace.")


def _headings(tex: str) -> list[_Heading]:
    found: list[_Heading] = []
    for match in _HEADING_START.finditer(tex):
        open_index = match.end() - 1
        close_index = _matching_brace(tex, open_index)
        found.append(_Heading(
            kind=match.group("kind"),
            title=tex[open_index + 1:close_index].strip(),
            command_start=match.start(),
            body_start=close_index + 1,
        ))
    return found


def _environment_body(tex: str, environment: str) -> tuple[int, int]:
    start_marker = rf"\begin{{{environment}}}"
    end_marker = rf"\end{{{environment}}}"
    start = tex.find(start_marker)
    if start < 0:
        raise ValueError(f"paper/main.tex has no {environment} environment.")
    body_start = start + len(start_marker)
    body_end = tex.find(end_marker, body_start)
    if body_end < 0:
        raise ValueError(f"paper/main.tex has no closing {end_marker}.")
    return body_start, body_end


def _abstract_text_body(tex: str) -> tuple[int, int]:
    """Select abstract prose without deleting the template-owned keywords."""
    body_start, body_end = _environment_body(tex, "abstract")
    keywords_marker = r"\noindent\textbf{Keywords:}"
    keywords_start = tex.find(keywords_marker, body_start, body_end)
    if keywords_start >= 0:
        body_end = keywords_start
    return body_start, body_end


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _paper_blocks(tex: str) -> list[_PaperBlock]:
    """Return addressable structural blocks with stable semantic identifiers."""
    blocks: list[_PaperBlock] = []
    try:
        start, end = _abstract_text_body(tex)
    except ValueError:
        pass
    else:
        blocks.append(_PaperBlock(
            block_id="abstract",
            target_type="abstract",
            title="Abstract",
            start=start,
            end=end,
            start_line=tex.count("\n", 0, start) + 1,
            end_line=tex.count("\n", 0, end) + 1,
            content_hash=_content_hash(tex[start:end]),
        ))

    headings = _headings(tex)
    section = ""
    subsection = ""
    occurrences: dict[tuple[str, ...], int] = {}
    identities: list[tuple[str, ...]] = []
    for heading in headings:
        if heading.kind == "section":
            section = heading.title
            subsection = ""
            path = ("section", heading.title)
        elif heading.kind == "subsection":
            subsection = heading.title
            path = ("subsection", section, heading.title)
        else:
            path = (
                "subsubsection",
                section,
                subsection,
                heading.title,
            )
        occurrences[path] = occurrences.get(path, 0) + 1
        identities.append((*path, str(occurrences[path])))

    for index, (heading, identity) in enumerate(zip(headings, identities)):
        end = len(tex)
        for following in headings[index + 1:]:
            if _LEVEL[following.kind] <= _LEVEL[heading.kind]:
                end = following.command_start
                break
        document_end = tex.find(r"\end{document}", heading.body_start)
        if document_end >= 0:
            end = min(end, document_end)
        digest = hashlib.sha256("\0".join(identity).encode()).hexdigest()[:12]
        blocks.append(_PaperBlock(
            block_id=f"blk_{digest}",
            target_type=heading.kind,
            title=heading.title,
            start=heading.body_start,
            end=end,
            start_line=tex.count("\n", 0, heading.body_start) + 1,
            end_line=tex.count("\n", 0, end) + 1,
            content_hash=_content_hash(tex[heading.body_start:end]),
        ))
    return blocks


def _block_by_id(tex: str, block_id: str) -> _PaperBlock:
    match = next(
        (block for block in _paper_blocks(tex) if block.block_id == block_id),
        None,
    )
    if match is None:
        available = ", ".join(block.block_id for block in _paper_blocks(tex))
        raise ValueError(
            f"No paper block named {block_id!r}. Inspect the current paper blocks "
            f"again. Available: {available or '(none)'}"
        )
    return match


def _heading_body(
    tex: str,
    kind: str,
    title: str,
    *,
    direct_only: bool = False,
) -> tuple[int, int]:
    headings = _headings(tex)
    matches = [
        (index, heading)
        for index, heading in enumerate(headings)
        if heading.kind == kind and heading.title == title.strip()
    ]
    if not matches:
        available = ", ".join(
            heading.title for heading in headings if heading.kind == kind
        )
        raise ValueError(
            f"No exact {kind} heading named {title!r}. "
            f"Available: {available or '(none)'}"
        )
    if len(matches) > 1:
        raise ValueError(
            f"Heading {title!r} appears {len(matches)} times; use target_type=text "
            "with a unique exact fragment."
        )

    index, heading = matches[0]
    body_end = len(tex)
    for following in headings[index + 1:]:
        if direct_only or _LEVEL[following.kind] <= _LEVEL[kind]:
            body_end = following.command_start
            break
    document_end = tex.find(r"\end{document}", heading.body_start)
    if document_end >= 0:
        body_end = min(body_end, document_end)
    return heading.body_start, body_end


def _flexible_text_pattern(target: str) -> re.Pattern[str]:
    pieces: list[str] = []
    index = 0
    while index < len(target):
        char = target[index]
        if char.isspace():
            while index < len(target) and target[index].isspace():
                index += 1
            pieces.append(r"\s+")
            continue
        if char == "\\" and index + 1 < len(target) and target[index + 1] == "%":
            pieces.append(r"\\?%")
            index += 2
            continue
        if char == "%":
            pieces.append(r"\\?%")
            index += 1
            continue
        pieces.append(re.escape(char))
        index += 1
    return re.compile("".join(pieces), re.MULTILINE)


def _nearest_text_candidates(text: str, target: str) -> list[str]:
    target_key = re.sub(r"\s+", "", target.replace(r"\%", "%")).casefold()
    candidates = [
        item.strip()
        for item in re.split(r"\n\s*\n|(?<=。)\s*", text)
        if len(item.strip()) >= 12
    ]
    ranked = sorted(
        candidates,
        key=lambda item: difflib.SequenceMatcher(
            None,
            target_key,
            re.sub(r"\s+", "", item.replace(r"\%", "%")).casefold(),
        ).ratio(),
        reverse=True,
    )
    return [item[:180] for item in ranked[:3]]


def _nth_exact(text: str, target: str, occurrence: int) -> tuple[int, int]:
    if not target:
        raise ValueError("target is required when target_type=text.")
    exact_matches = list(re.finditer(re.escape(target), text))
    if len(exact_matches) >= occurrence:
        match = exact_matches[occurrence - 1]
        return match.start(), match.end()

    flexible_matches = list(_flexible_text_pattern(target).finditer(text))
    if len(flexible_matches) >= occurrence:
        match = flexible_matches[occurrence - 1]
        return match.start(), match.end()

    suggestions = _nearest_text_candidates(text, target)
    hint = (
        " Closest current text: "
        + " || ".join(repr(item) for item in suggestions)
        if suggestions else ""
    )
    raise ValueError(
        f"Text target occurrence {occurrence} was not found, even after "
        f"normalizing whitespace and escaped percent signs.{hint}"
    )


def _splice(
    tex: str,
    *,
    start: int,
    end: int,
    operation: str,
    content: str,
    text_target: bool,
) -> str:
    block = tex[start:end]
    inserted = content.strip()
    if operation == "replace":
        replacement = inserted
    elif operation == "prepend" and not text_target:
        replacement = f"\n{inserted}\n{block.lstrip()}"
    elif operation == "append" and not text_target:
        replacement = f"{block.rstrip()}\n\n{inserted}\n"
    elif operation == "insert_before" and text_target:
        replacement = f"{inserted}\n\n{block}"
    elif operation == "insert_after" and text_target:
        replacement = f"{block}\n\n{inserted}"
    else:
        raise ValueError(
            f"operation={operation!r} is not valid for this target type."
        )
    return tex[:start] + replacement + tex[end:]


def _is_cjk_document(tex: str) -> bool:
    return bool(
        re.search(r"\\documentclass(?:\[[^\]]*\])?\{ctex", tex)
        or r"\usepackage{ctex}" in tex
    )


def _validate_local_fragment(content: str, target_type: str) -> None:
    """Reject document-wide layout changes from a localized editing tool."""
    command = _LAYOUT_COMMAND.search(content)
    if command:
        raise ValueError(
            f"localized content contains document-level layout command "
            f"{command.group(0)!r}; use write_paper for structural changes."
        )

    allowed_deeper_than = _LEVEL.get(target_type, 3)
    for match in _ANY_HEADING.finditer(content):
        kind = match.group(1)
        if _LEVEL[kind] <= allowed_deeper_than:
            raise ValueError(
                f"target_type={target_type!r} cannot insert {kind} headings at "
                "the same or a higher structural level."
            )


def _normalize_systematic_double_escaping(content: str) -> tuple[str, bool]:
    """Repair JSON-overescaped LaTeX when commands consistently use ``\\``."""
    if not _DOUBLE_ESCAPED_COMMAND.search(content):
        return content, False
    normalized = _DOUBLE_ESCAPED_COMMAND.sub("\\\\", content)
    normalized = _DOUBLE_ESCAPED_SPECIAL.sub("\\\\", normalized)
    normalized = _FOUR_SLASH_LINE_END.sub(r"\\\\", normalized)
    return normalized, normalized != content


def _strip_matching_outer_heading(
    content: str,
    target_type: str,
    target: str,
) -> tuple[str, bool]:
    """Accept either a structural body or a complete matching outer block."""
    if target_type not in _LEVEL or not target.strip():
        return content, False
    headings = _headings(content)
    if not headings:
        return content, False
    first = headings[0]
    if (
        content[:first.command_start].strip()
        or first.kind != target_type
        or first.title != target.strip()
    ):
        return content, False
    return content[first.body_start:].lstrip(), True


def _heading_signature(tex: str) -> list[tuple[str, str]]:
    return [(heading.kind, heading.title) for heading in _headings(tex)]


def _label_counts(tex: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label in _LABEL.findall(tex):
        counts[label] = counts.get(label, 0) + 1
    return counts


def _heading_context_counts(tex: str) -> dict[tuple[str, ...], int]:
    """Count headings under their structural parent, not just globally."""
    section = ""
    subsection = ""
    counts: dict[tuple[str, ...], int] = {}
    for heading in _headings(tex):
        if heading.kind == "section":
            section = heading.title
            subsection = ""
            key = ("section", heading.title)
        elif heading.kind == "subsection":
            subsection = heading.title
            key = ("subsection", section, heading.title)
        else:
            key = ("subsubsection", section, subsection, heading.title)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _unresolved_references(tex: str) -> set[str]:
    return set(_REFERENCE.findall(tex)) - set(_LABEL.findall(tex))


def _is_ordered_subsequence(
    original: list[tuple[str, str]],
    revised: list[tuple[str, str]],
) -> bool:
    """Allow new subordinate headings, but never delete or rename old ones."""
    cursor = iter(revised)
    return all(any(candidate == expected for candidate in cursor) for expected in original)


def _restore_after_rejected_edit(tex_path: Path, original: str) -> None:
    """Restore the last accepted source and regenerate its matching PDF."""
    tex_path.write_text(original)
    compile_tex(tex_path)


def _inspect_paper_blocks(ctx: ToolContext, args: dict) -> str:
    tex_path = ctx.workdir / "paper" / "main.tex"
    if not tex_path.is_file():
        return "[inspect error] paper/main.tex does not exist; call write_paper first."
    tex = tex_path.read_text(errors="replace")
    block_id = str(args.get("block_id") or "")
    target_type = str(args.get("target_type") or "")
    title = str(args.get("title") or "")
    include_content = bool(
        args.get("include_content", bool(block_id or title))
    )
    max_content_chars = max(
        500,
        min(40_000, int(args.get("max_content_chars") or 12_000)),
    )
    blocks = _paper_blocks(tex)
    if block_id:
        blocks = [block for block in blocks if block.block_id == block_id]
    if target_type:
        blocks = [
            block for block in blocks if block.target_type == target_type
        ]
    if title:
        blocks = [block for block in blocks if block.title == title]
    if not blocks:
        return (
            "[inspect error] no matching paper block. Refresh the block manifest "
            "and choose one of the current block IDs."
        )
    payload = []
    for block in blocks:
        item: dict[str, object] = {
            "block_id": block.block_id,
            "target_type": block.target_type,
            "title": block.title,
            "content_hash": block.content_hash,
            "start_line": block.start_line,
            "end_line": block.end_line,
        }
        content = tex[block.start:block.end].strip()
        if include_content:
            item["content"] = content[:max_content_chars]
            item["content_truncated"] = len(content) > max_content_chars
        else:
            item["preview"] = re.sub(r"\s+", " ", content)[:180]
        payload.append(item)
    return json.dumps(
        {
            "paper": "paper/main.tex",
            "block_count": len(payload),
            "blocks": payload,
        },
        ensure_ascii=False,
        indent=2,
    )


def _validate_expected_hashes(tex: str, edits: list[dict]) -> None:
    for edit in edits:
        block_id = str(edit.get("block_id") or "")
        expected_hash = str(edit.get("expected_hash") or "")
        if not block_id or not expected_hash:
            continue
        block = _block_by_id(tex, block_id)
        if block.content_hash != expected_hash:
            raise ValueError(
                f"paper block {block_id} changed after inspection "
                f"(expected {expected_hash}, current {block.content_hash}). "
                "Inspect the block again before editing."
            )


def _apply_one_edit(
    ctx: ToolContext,
    tex: str,
    args: dict,
) -> tuple[str, list[str]]:
    target_type = str(args.get("target_type") or "")
    operation = str(args.get("operation") or "")
    content = str(args.get("content") or "")
    target = str(args.get("target") or "")
    block_id = str(args.get("block_id") or "")
    occurrence = int(args.get("occurrence") or 1)
    notes: list[str] = []

    if not content.strip():
        raise ValueError("content must not be empty.")
    if len(content) > _MAX_CONTENT_CHARS:
        raise ValueError(f"content exceeds {_MAX_CONTENT_CHARS} characters.")
    if occurrence < 1:
        raise ValueError("occurrence must be at least 1.")

    scope: _PaperBlock | None = None
    if block_id:
        scope = _block_by_id(tex, block_id)
        if not target_type:
            target_type = scope.target_type
            target = scope.title
        elif target_type != "text" and target_type != scope.target_type:
            raise ValueError(
                f"block {block_id} is {scope.target_type}, not {target_type}."
            )

    content, normalized = _normalize_systematic_double_escaping(content)
    if normalized:
        notes.append("normalized systematically doubled LaTeX backslashes")

    inferred_target = target or (scope.title if scope is not None else "")
    content, stripped = _strip_matching_outer_heading(
        content,
        target_type,
        inferred_target,
    )
    if stripped:
        notes.append("removed the matching outer heading from replacement content")

    _validate_local_fragment(content, target_type)
    content = render_latex_fragment(content, workdir=ctx.workdir)
    if target_type == "abstract":
        if operation not in {"replace", "prepend", "append"}:
            raise ValueError("abstract supports replace, prepend, or append only.")
        if scope is not None:
            start, end = scope.start, scope.end
        else:
            start, end = _abstract_text_body(tex)
        text_target = False
    elif target_type in _LEVEL:
        if operation not in {"replace", "prepend", "append"}:
            raise ValueError(
                "heading blocks support replace, prepend, or append only."
            )
        if scope is not None:
            start, end = scope.start, scope.end
        else:
            if not target.strip():
                raise ValueError(
                    "target heading title or block_id is required."
                )
            start, end = _heading_body(
                tex,
                target_type,
                target,
                direct_only=False,
            )
        text_target = False
    elif target_type == "text":
        if operation not in {"replace", "insert_before", "insert_after"}:
            raise ValueError(
                "exact text supports replace, insert_before, or insert_after only."
            )
        content_has_semantic_block = bool(_SEMANTIC_BLOCK.search(content))
        target_has_semantic_block = bool(_SEMANTIC_BLOCK.search(target))
        if content_has_semantic_block and (
            operation != "replace" or not target_has_semantic_block
        ):
            raise ValueError(
                "a text edit cannot insert a formula/table/figure/label into "
                "ordinary prose. Replace the complete containing block."
            )
        if scope is not None:
            relative_start, relative_end = _nth_exact(
                tex[scope.start:scope.end],
                target,
                occurrence,
            )
            start = scope.start + relative_start
            end = scope.start + relative_end
        else:
            start, end = _nth_exact(tex, target, occurrence)
        text_target = True
    else:
        raise ValueError(
            "target_type is required unless block_id identifies a block."
        )

    revised = _splice(
        tex,
        start=start,
        end=end,
        operation=operation,
        content=content,
        text_target=text_target,
    )
    structural_replace = target_type in _LEVEL and operation == "replace"
    if not structural_replace and not _is_ordered_subsequence(
        _heading_signature(tex),
        _heading_signature(revised),
    ):
        raise ValueError(
            "localized edit would delete or rename an existing heading; use "
            "write_paper for structural changes."
        )
    original_labels = _label_counts(tex)
    revised_labels = _label_counts(revised)
    newly_duplicated = {
        label: count
        for label, count in revised_labels.items()
        if count > max(1, original_labels.get(label, 0))
    }
    if newly_duplicated:
        details = ", ".join(
            f"{label} ({count} copies)"
            for label, count in sorted(newly_duplicated.items())
        )
        raise ValueError(
            "localized edit would introduce duplicate LaTeX labels: "
            f"{details}. Replace the complete containing block or omit the "
            "already-existing equation/table/figure."
        )
    original_headings = _heading_context_counts(tex)
    revised_headings = _heading_context_counts(revised)
    newly_repeated_headings = {
        key: count
        for key, count in revised_headings.items()
        if count > max(1, original_headings.get(key, 0))
    }
    if newly_repeated_headings:
        details = ", ".join(
            f"{key[-1]} ({count} copies)"
            for key, count in sorted(newly_repeated_headings.items())
        )
        raise ValueError(
            "localized edit would repeat headings inside the same parent "
            f"section: {details}. Replace the complete parent block instead."
        )
    newly_unresolved = _unresolved_references(revised) - _unresolved_references(tex)
    if newly_unresolved:
        raise ValueError(
            "localized edit would create unresolved LaTeX references: "
            + ", ".join(sorted(newly_unresolved))
            + ". Keep the referenced label or update every dependent reference."
        )
    return revised, notes


def _edit_paragraph(ctx: ToolContext, args: dict) -> str:
    tex_path = ctx.workdir / "paper" / "main.tex"
    if not tex_path.is_file():
        return "[edit error] paper/main.tex does not exist; call write_paper first."

    raw_edits = args.get("edits")
    if raw_edits is not None:
        if not isinstance(raw_edits, list) or not raw_edits:
            return "[edit error] edits must be a non-empty array."
        if len(raw_edits) > 20:
            return "[edit error] edits cannot contain more than 20 operations."
        if not all(isinstance(item, dict) for item in raw_edits):
            return "[edit error] every edits item must be an object."
        edits = [dict(item) for item in raw_edits]
    else:
        edits = [dict(args)]

    tex = tex_path.read_text(errors="replace")
    baseline_metrics = None
    pdf_path = tex_path.with_suffix(".pdf")
    if pdf_path.is_file():
        try:
            baseline_metrics = inspect_paper(pdf_path, tex_path)
        except Exception:
            baseline_metrics = None
    try:
        _validate_expected_hashes(tex, edits)
        revised = tex
        normalization_notes: list[str] = []
        for edit in edits:
            revised, notes = _apply_one_edit(ctx, revised, edit)
            normalization_notes.extend(notes)
    except ValueError as exc:
        return f"[edit_paragraph:v2] status=rejected reason=invalid_edit\n{exc}"
    except Exception as exc:
        return (
            "[edit_paragraph:v2] status=rejected reason=render_failure\n"
            f"{type(exc).__name__}: {exc}"
        )

    if revised == tex:
        return (
            "[edit_paragraph:v2] status=rejected reason=no_change\n"
            "The requested transaction did not change paper/main.tex."
        )
    if revised.count(r"\begin{document}") != 1 or revised.count(r"\end{document}") != 1:
        return (
            "[edit_paragraph:v2] status=rejected reason=document_boundary\n"
            "The transaction would damage the document boundary; paper/main.tex "
            "was not changed."
        )

    revision_dir = tex_path.parent / "revisions"
    revision_dir.mkdir(exist_ok=True)
    revision_index = ctx.next_index("paper_edit")
    backup = revision_dir / f"main.before-edit-{revision_index:03d}.tex"
    backup.write_text(tex)
    temporary = tex_path.with_suffix(".tex.tmp")
    temporary.write_text(revised)
    temporary.replace(tex_path)

    res = compile_tex(tex_path)
    backup_rel = backup.relative_to(ctx.workdir)
    if not res.ok:
        _restore_after_rejected_edit(tex_path, tex)
        return (
            "[edit_paragraph:v2] status=rolled_back reason=compile_failure\n"
            f"Localized transaction ({len(edits)} edit(s)) rejected and rolled "
            "back because compilation failed. "
            f"Backup: {backup_rel}. The previous paper/main.tex and PDF were "
            "restored.\n--- log tail ---\n"
            + tail(res.log, max_lines=25)
        )

    structural_failures = inspect_latex_structure(revised)
    if structural_failures:
        _restore_after_rejected_edit(tex_path, tex)
        return (
            "[edit_paragraph:v2] status=rolled_back "
            "reason=paper_structure\nLocalized transaction rejected and "
            "rolled back because it broke the document hierarchy or region "
            "order. "
            + " ".join(structural_failures)
            + f" Backup: {backup_rel}."
        )

    metric_line, failures = paper_acceptance(
        ctx,
        res.pdf_path,
        tex_path,
        cjk=_is_cjk_document(revised),
    )
    revised_metrics = inspect_paper(res.pdf_path, tex_path)
    paper_cfg = resolve_paper_config(ctx.settings, ctx.workdir)
    max_pages = int(
        paper_cfg.get(
            "max_pages",
            paper_cfg.get("target_pages", 20),
        )
    )
    min_pages = int(paper_cfg.get("min_pages", 17))
    min_equations = int(paper_cfg.get("min_display_equations", 12))
    min_fill = float(paper_cfg.get("abstract_fill_min_ratio", 0.72))
    revised_page_count = selected_page_count(revised_metrics, paper_cfg)
    baseline_page_count = (
        selected_page_count(baseline_metrics, paper_cfg)
        if baseline_metrics is not None
        else None
    )
    structural_regression = (
        (
            baseline_metrics is not None
            and baseline_metrics.first_section_page == 2
            and revised_metrics.first_section_page != 2
        )
        or (
            baseline_metrics is not None
            and baseline_metrics.first_section_page not in {None, 2}
            and revised_metrics.first_section_page is not None
            and revised_metrics.first_section_page
            > baseline_metrics.first_section_page
        )
        or (
            revised_page_count > max_pages
            and (
                baseline_page_count is None
                or baseline_page_count <= max_pages
                or revised_page_count >= baseline_page_count
            )
        )
        or (
            baseline_metrics is not None
            and baseline_page_count is not None
            and revised_page_count < min_pages
            and revised_page_count < baseline_page_count
        )
        or (
            baseline_metrics is not None
            and revised_metrics.display_equation_count < min_equations
            and revised_metrics.display_equation_count
            < baseline_metrics.display_equation_count
        )
        or (
            baseline_metrics is not None
            and revised_metrics.abstract_fill_ratio < min_fill
            and revised_metrics.abstract_fill_ratio
            < baseline_metrics.abstract_fill_ratio
        )
    )
    if structural_regression:
        _restore_after_rejected_edit(tex_path, tex)
        return (
            "[edit_paragraph:v2] status=rolled_back reason=layout_regression\n"
            "Localized transaction rejected and rolled back because it damaged the "
            f"document layout ({metric_line}). Backup: {backup_rel}. The previous "
            "paper/main.tex and PDF were restored. Split the change into a smaller "
            "paragraph edit, or use write_paper for a genuine structural rewrite."
        )
    if failures:
        return (
            "[edit_paragraph:v2] status=applied acceptance=pending "
            f"edits={len(edits)}\n"
            "Localized transaction APPLIED and compiled OK; DOCUMENT ACCEPTANCE IS "
            "STILL PENDING "
            f"({metric_line}). Backup: {backup_rel}.\n"
            + "\n".join(f"- {failure}" for failure in failures)
            + "\nThe requested local change is present in paper/main.tex; do not "
              "repeat it or restore an older revision. Continue from this source "
              "with the next specific edit_paragraph repair. Regenerate the whole "
              "paper only when its overall architecture is genuinely wrong."
        )

    delivery_path = tex_path.parent / "delivery.json"
    delivery: dict = {}
    if delivery_path.is_file():
        try:
            delivery = json.loads(delivery_path.read_text())
        except (json.JSONDecodeError, OSError):
            delivery = {}
    delivery["generated_at"] = time.time()
    delivery_path.write_text(json.dumps(delivery, ensure_ascii=False, indent=2))
    notes = sorted(set(normalization_notes))
    note_line = f" Normalizations: {'; '.join(notes)}." if notes else ""
    return (
        "[edit_paragraph:v2] status=applied acceptance=passed "
        f"edits={len(edits)}\n"
        "Localized transaction compiled and paper acceptance PASSED -> "
        f"paper/main.pdf ({metric_line}). Backup: {backup_rel}. "
        f"Source: paper/main.tex.{note_line}"
    )


edit_paragraph_tool = Tool(
    name="edit_paragraph",
    description=(
        "Apply localized edits to paper/main.tex. Inspect the current block first, "
        "prefer block_id + expected_hash, and batch dependent changes in edits[]. "
        "The transaction compiles once and rolls back on compile or layout regression."
    ),
    parameters=_PARAMS,
    handler=_edit_paragraph,
)


inspect_paper_blocks_tool = Tool(
    name="inspect_paper_blocks",
    description=(
        "Inspect the current paper structure before editing. Returns stable block_id "
        "and content_hash values plus the complete current body for a selected "
        "abstract/section/subsection/subsubsection. Call this before edit_paragraph "
        "instead of copying stale text from verifier evidence."
    ),
    parameters={
        "type": "object",
        "properties": {
            "block_id": {"type": "string"},
            "target_type": {
                "type": "string",
                "enum": ["abstract", "section", "subsection", "subsubsection"],
            },
            "title": {"type": "string"},
            "include_content": {"type": "boolean"},
            "max_content_chars": {
                "type": "integer",
                "minimum": 500,
                "maximum": 40_000,
            },
        },
    },
    handler=_inspect_paper_blocks,
)
