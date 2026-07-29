"""read_file tool: let the agent read text files in the workdir (problem.md, a
data/*.csv, a log). Output is tailed by default; an explicit line range can be
used to inspect a complete paper section without pulling the whole document into
context.
"""

from __future__ import annotations

from .base import Tool, ToolContext, tail


def _read_file(ctx: ToolContext, args: dict) -> str:
    rel = args["path"]
    p = (ctx.workdir / rel).resolve()
    if not str(p).startswith(str(ctx.workdir)):
        return f"[error] path escapes workdir: {rel}"
    if not p.is_file():
        return f"[error] not found: {rel}"
    text = p.read_text(errors="replace")
    start_line = args.get("start_line")
    end_line = args.get("end_line")
    if start_line is None and end_line is None:
        return f"{rel}:\n{tail(text, max_lines=200, max_chars=8000)}"

    lines = text.splitlines()
    start = max(1, int(start_line or 1))
    end = min(len(lines), int(end_line or len(lines)))
    if end < start:
        return "[error] end_line must be greater than or equal to start_line."
    if end - start + 1 > 500:
        return "[error] requested line range exceeds 500 lines; split the read."
    numbered = "\n".join(
        f"{line_number:>5}: {lines[line_number - 1]}"
        for line_number in range(start, end + 1)
    )
    if len(numbered) > 30_000:
        return "[error] requested line range exceeds 30000 characters; split the read."
    return f"{rel} lines {start}-{end}:\n{numbered}"


read_file_tool = Tool(
    name="read_file",
    description=(
        "Read a text file under the run workdir. For paper/main.tex, use an "
        "explicit start_line/end_line range to inspect the complete parent section "
        "before a localized edit."
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
