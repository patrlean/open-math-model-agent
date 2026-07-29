"""run_code tool: write Python, execute it in the sandbox, return a compact result.

Full stdout/stderr is always written to logs/run_<n>.log; only a tail is returned
to the model. Results the paper will cite should be written by the code itself to
results/ (json/csv), not narrated back in prose -- that keeps numbers traceable
to an actual execution.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from ..sandbox.base import DEFAULT_EXEC_TIMEOUT_SECONDS
from .base import Tool, ToolContext, tail

_PARAMS = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": "Python source to execute. cwd is the run workdir; read "
            "inputs from data/, write results to results/*.json and plots to figures/.",
        },
        "timeout": {
            "type": "integer",
            "minimum": 1,
            "maximum": DEFAULT_EXEC_TIMEOUT_SECONDS,
            "description": (
                "Wall-clock limit in seconds "
                f"(default and maximum {DEFAULT_EXEC_TIMEOUT_SECONDS})."
            ),
        },
    },
    "required": ["code"],
}


def _run_code(ctx: ToolContext, args: dict) -> str:
    code = args["code"]
    timeout = max(
        1,
        min(
            int(args.get("timeout", DEFAULT_EXEC_TIMEOUT_SECONDS)),
            DEFAULT_EXEC_TIMEOUT_SECONDS,
        ),
    )
    idx = ctx.next_index("run_code")

    # ``run_code`` owns numerical artifacts, never the paper source. Keep an
    # atomic backup as a backend-independent guard (Docker additionally mounts
    # paper/ read-only). This catches direct writes, copies, deletions, and PDF
    # regeneration without blocking legitimate reads from paper/main.tex.
    paper_dir = ctx.workdir / "paper"
    paper_existed = paper_dir.is_dir()
    with tempfile.TemporaryDirectory(prefix="mathmodel-paper-guard-") as tmp:
        backup_dir = Path(tmp) / "paper"
        if paper_existed:
            shutil.copytree(paper_dir, backup_dir)
        result = ctx.sandbox.exec_python(
            code,
            timeout=timeout,
            stop_event=ctx.stop_event,
        )
        paper_artifacts = [
            path for path in result.artifacts
            if path == "paper" or path.startswith("paper/")
        ]
        if paper_artifacts:
            if paper_dir.exists():
                shutil.rmtree(paper_dir)
            if paper_existed:
                shutil.copytree(backup_dir, paper_dir)
            result.artifacts = [
                path for path in result.artifacts if path not in paper_artifacts
            ]

    # Persist full logs for later grep; keep the observation small.
    logs_dir = ctx.workdir / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_name = f"run_{ctx.scope}{idx}.log"
    (logs_dir / log_name).write_text(
        f"$ python (timeout={timeout}s)\n"
        f"=== source ===\n{code}\n"
        f"--- exit_code={result.exit_code} timed_out={result.timed_out} "
        f"stopped={result.stopped} duration={result.duration_s}s ---\n"
        f"=== stdout ===\n{result.stdout}\n=== stderr ===\n{result.stderr}\n"
    )

    if result.stopped:
        return f"[stopped by user after {result.duration_s}s]"

    lines = [
        f"exit_code={result.exit_code}  timed_out={result.timed_out}  "
        f"duration={result.duration_s}s",
    ]
    if result.artifacts:
        lines.append("artifacts: " + ", ".join(result.artifacts))
    if paper_artifacts:
        lines.append(
            "[blocked] run_code attempted to modify protected paper/ artifacts "
            "and those changes were rolled back. Use write_paper or "
            "edit_paragraph for paper changes."
        )
    out_tail = tail(result.stdout)
    err_tail = tail(result.stderr)
    if out_tail:
        lines.append("--- stdout ---\n" + out_tail)
    if err_tail:
        lines.append("--- stderr ---\n" + err_tail)
    if not out_tail and not err_tail:
        lines.append("(no output)")
    lines.append(f"(full log: logs/{log_name})")
    return "\n".join(lines)


run_code_tool = Tool(
    name="run_code",
    description="Execute a Python script in the sandbox and return exit code, a tail "
    "of stdout/stderr, and the list of files it created/modified.",
    parameters=_PARAMS,
    handler=_run_code,
)
