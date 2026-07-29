"""Integration demo: ingest uploaded materials -> agent reads problem.md ->
solves using the provided data. Proves the front half of the pipeline closes.

Run (docker sandbox):  ./.venv/bin/python -m scripts.demo_ingest_solve docker
     (local sandbox):   ./.venv/bin/python -m scripts.demo_ingest_solve
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from mathmodel.agent.loop import Agent
from mathmodel.agent.prompts import SKELETON_SYSTEM
from mathmodel.config import PROJECT_ROOT, build_provider, build_sandbox, load_config
from mathmodel.ingest.ingest import ingest
from mathmodel.tools.base import ToolContext, ToolRegistry
from mathmodel.tools.read_file import read_file_tool
from mathmodel.tools.results_store import results_get_tool, results_list_tool
from mathmodel.tools.run_code import run_code_tool

# Reuse the event printer from the fit demo.
from scripts.demo_fit import make_event_printer


def make_uploads(d: Path) -> list[Path]:
    d.mkdir(parents=True, exist_ok=True)
    # A "problem statement" PDF.
    import fitz
    doc = fitz.open(); page = doc.new_page()
    page.insert_text((72, 72),
                     "Task: The file measurements.xlsx (sheet 'obs') contains x and y.\n"
                     "Fit y = a*x + b by least squares. Report a, b, RMSE.",
                     fontsize=11)
    pdf = d / "task.pdf"; doc.save(pdf); doc.close()

    # The data as Excel.
    rng = np.random.default_rng(1)
    x = np.linspace(0, 8, 40); y = 2.5 * x - 1.0 + rng.normal(0, 0.3, x.size)
    xlsx = d / "measurements.xlsx"
    with pd.ExcelWriter(xlsx) as xl:
        pd.DataFrame({"x": x, "y": y}).to_excel(xl, sheet_name="obs", index=False)
    return [pdf, xlsx]


def main() -> None:
    cfg = load_config()
    choice = sys.argv[1] if len(sys.argv) > 1 else "local"
    cfg["sandbox"] = choice
    if choice == "local":
        cfg["sandbox_python"] = sys.executable

    run_id = time.strftime("%Y%m%d-%H%M%S")
    workdir = PROJECT_ROOT / "workspace" / run_id
    uploads = make_uploads(workdir / "_uploads")

    report = ingest(uploads, workdir)
    print(f"ingested -> problem.md, data_files={report.data_files}\n")

    provider = build_provider(cfg)
    sandbox = build_sandbox(cfg, workdir)
    ctx = ToolContext(workdir=workdir, sandbox=sandbox)
    registry = ToolRegistry()
    for tool in (read_file_tool, run_code_tool, results_list_tool, results_get_tool):
        registry.register(tool)

    agent = Agent(provider, registry, ctx, SKELETON_SYSTEM,
                  compact_threshold_tokens=cfg["context"]["compact_threshold_tokens"],
                  on_event=make_event_printer())

    task = ("Read problem.md to understand the task and what data is available, "
            "then solve it. Write the requested values to results/fit.json and report them.")
    summary = agent.run(task)

    print("\nfinal summary:\n" + summary)
    fit = workdir / "results" / "fit.json"
    print(f"\nresults/fit.json exists: {fit.exists()}")
    if fit.exists():
        print(fit.read_text())


if __name__ == "__main__":
    main()
