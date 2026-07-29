"""Compile a .tex file to PDF.

Compilation runs on the HOST (not in the code sandbox): tectonic needs network on
first run to fetch packages, while the sandbox is network-isolated. Figures and
data referenced with relative paths resolve because we compile with the paper
directory as cwd.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# tectonic is often outside the minimal PATH a subprocess inherits.
_EXTRA_PATH = ":/opt/homebrew/bin:/usr/local/bin:/Library/TeX/texbin"


@dataclass
class CompileResult:
    ok: bool
    pdf_path: Path | None
    log: str


def _default_cmd(tex_path: Path) -> list[str]:
    # tectonic writes <stem>.pdf next to the source by default.
    return ["tectonic", "--chatter", "minimal", str(tex_path.name)]


def compile_tex(
    tex_path: str | Path,
    cmd: list[str] | None = None,
    timeout: int = 300,
) -> CompileResult:
    tex_path = Path(tex_path).resolve()
    workdir = tex_path.parent
    run_cmd = cmd or _default_cmd(tex_path)

    env = {**os.environ, "PATH": os.environ.get("PATH", "") + _EXTRA_PATH}
    if not shutil.which(run_cmd[0], path=env["PATH"]):
        return CompileResult(False, None, f"[error] engine '{run_cmd[0]}' not found on PATH")

    try:
        proc = subprocess.run(
            run_cmd, cwd=workdir, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return CompileResult(False, None, f"[error] compile timed out after {timeout}s")

    pdf = tex_path.with_suffix(".pdf")
    log = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode == 0 and pdf.exists()
    return CompileResult(ok=ok, pdf_path=pdf if pdf.exists() else None, log=log)
