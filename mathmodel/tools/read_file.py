"""Deterministically page through text files in the run workdir."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .base import Tool, ToolContext


DEFAULT_PAGE_LINES = 160
MAX_RANGE_LINES = 500
MAX_PAGE_CHARS = 30_000


def render_text_page(
    path: Path,
    relative_path: str,
    text: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    view: str = "text",
) -> str:
    """Return one numbered page with enough metadata to request the next page."""
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        start = end = 0
    else:
        start = max(1, int(start_line or 1))
        if start > total:
            return (
                f"[error] start_line {start} exceeds total_lines {total} "
                f"for {relative_path}."
            )
        if end_line is None:
            end = min(total, start + DEFAULT_PAGE_LINES - 1)
        else:
            end = min(total, int(end_line))
        if end < start:
            return "[error] end_line must be greater than or equal to start_line."
        if end - start + 1 > MAX_RANGE_LINES:
            return (
                f"[error] requested line range exceeds {MAX_RANGE_LINES} lines; "
                f"request at most lines {start}-{start + MAX_RANGE_LINES - 1}."
            )

    numbered = "\n".join(
        f"{line_number:>5}: {lines[line_number - 1]}"
        for line_number in range(start, end + 1)
    ) if total else ""
    if len(numbered) > MAX_PAGE_CHARS:
        return (
            f"[error] requested page is {len(numbered)} characters, exceeding "
            f"{MAX_PAGE_CHARS}; request a smaller line range. "
            f"path={relative_path} total_lines={total}."
        )

    page_size = max(1, end - start + 1) if total else DEFAULT_PAGE_LINES
    previous_start = max(1, start - page_size) if start > 1 else None
    next_start = end + 1 if end < total else None
    size_bytes = path.stat().st_size
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    header = [
        f"path: {relative_path}",
        f"view: {view}",
        f"sha256: {sha256}",
        f"size_bytes: {size_bytes}",
        f"total_lines: {total}",
        f"returned_lines: {start}-{end}",
        f"has_more_before: {'true' if start > 1 else 'false'}",
        f"has_more_after: {'true' if end < total else 'false'}",
        f"previous_start_line: {previous_start if previous_start is not None else 'null'}",
        f"next_start_line: {next_start if next_start is not None else 'null'}",
    ]
    return "\n".join(header) + "\n\ncontent:\n" + numbered


def _read_file(ctx: ToolContext, args: dict) -> str:
    rel = args["path"]
    root = ctx.workdir.resolve()
    p = (root / rel).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return f"[error] path escapes workdir: {rel}"
    if not p.is_file():
        return f"[error] not found: {rel}"
    return render_text_page(
        p,
        rel,
        p.read_text(errors="replace"),
        start_line=args.get("start_line"),
        end_line=args.get("end_line"),
    )


read_file_tool = Tool(
    name="read_file",
    description=(
        "Read a deterministic numbered page from a text file under the run "
        "workdir. Without a range, returns lines 1-160 plus total_lines and "
        "next_start_line. Use start_line/end_line to continue or target a section."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workdir-relative path, e.g. problem.md",
            },
            "start_line": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional 1-based first line for a targeted read.",
            },
            "end_line": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional inclusive last line for a targeted read.",
            },
        },
        "required": ["path"],
    },
    handler=_read_file,
)
