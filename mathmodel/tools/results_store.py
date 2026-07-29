"""results_store tools: read the results/ directory that code writes to.

This is the single source of truth for numbers the paper cites. Code (run via
run_code) writes results/*.json|*.csv; the model reads them back through these
tools to reason over and, later, to fill into the LaTeX template. The model does
not *write* results here -- values must come from execution, not from prose.
"""

from __future__ import annotations

import json

from .base import Tool, ToolContext, tail


def _results_dir(ctx: ToolContext):
    d = ctx.workdir / "results"
    d.mkdir(exist_ok=True)
    return d


def _results_list(ctx: ToolContext, args: dict) -> str:
    d = _results_dir(ctx)
    files = sorted(str(p.relative_to(ctx.workdir)) for p in d.rglob("*") if p.is_file())
    if not files:
        return "results/ is empty."
    return "results files:\n" + "\n".join(files)


def _results_get(ctx: ToolContext, args: dict) -> str:
    rel = args["path"]
    p = (ctx.workdir / rel).resolve()
    # Contain reads to the workdir.
    if not str(p).startswith(str(ctx.workdir)):
        return f"[error] path escapes workdir: {rel}"
    if not p.is_file():
        return f"[error] not found: {rel}"
    text = p.read_text(errors="replace")
    if p.suffix == ".json":
        try:  # pretty + validate
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass
    return f"{rel}:\n{tail(text, max_lines=80, max_chars=6000)}"


results_list_tool = Tool(
    name="results_list",
    description="List files under results/ (values written by executed code).",
    parameters={"type": "object", "properties": {}},
    handler=_results_list,
)

results_get_tool = Tool(
    name="results_get",
    description="Read a file under the run workdir (e.g. a results/*.json).",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workdir-relative path, e.g. results/fit.json"}
        },
        "required": ["path"],
    },
    handler=_results_get,
)
