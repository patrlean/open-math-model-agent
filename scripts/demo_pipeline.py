"""Full-pipeline demo: ingest -> model & run (Docker) -> plot -> compile LaTeX PDF.

Exercises tasks 1-3 together. Run (docker sandbox recommended):
    ./.venv/bin/python -m scripts.demo_pipeline docker
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from mathmodel.agent.loop import Agent
from mathmodel.agent.prompts import MODELING_SYSTEM
from mathmodel.config import PROJECT_ROOT, build_provider, build_sandbox, load_config
from mathmodel.ingest.ingest import ingest
from mathmodel.tools.base import ToolContext, ToolRegistry
from mathmodel.tools.read_file import read_file_tool
from mathmodel.tools.results_store import results_get_tool, results_list_tool
from mathmodel.tools.run_code import run_code_tool
from mathmodel.tools.write_paper import write_paper_tool
from scripts.demo_fit import make_event_printer

# tectonic lives on the host PATH.
os.environ["PATH"] = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"


def make_uploads(d: Path) -> list[Path]:
    d.mkdir(parents=True, exist_ok=True)
    import fitz
    doc = fitz.open(); page = doc.new_page()
    page.insert_text((72, 72),
                     "Problem: sales.xlsx (sheet 'data') has month (1-24) and sales.\n"
                     "Model the trend, forecast month 25-27, and quantify fit quality.\n"
                     "Produce a short report with a plot.",
                     fontsize=11)
    pdf = d / "problem.pdf"; doc.save(pdf); doc.close()

    rng = np.random.default_rng(7)
    m = np.arange(1, 25)
    sales = 100 + 8 * m + rng.normal(0, 12, m.size)  # linear trend + noise
    xlsx = d / "sales.xlsx"
    with pd.ExcelWriter(xlsx) as xl:
        pd.DataFrame({"month": m, "sales": sales}).to_excel(xl, sheet_name="data", index=False)
    return [pdf, xlsx]


def main() -> None:
    cfg = load_config()
    choice = sys.argv[1] if len(sys.argv) > 1 else "docker"
    cfg["sandbox"] = choice
    if choice == "local":
        cfg["sandbox_python"] = sys.executable

    run_id = time.strftime("%Y%m%d-%H%M%S")
    workdir = PROJECT_ROOT / "workspace" / run_id
    uploads = make_uploads(workdir / "_uploads")
    report = ingest(uploads, workdir)
    print(f"ingested -> data_files={report.data_files}\n")

    provider = build_provider(cfg)
    sandbox = build_sandbox(cfg, workdir)
    ctx = ToolContext(workdir=workdir, sandbox=sandbox)
    registry = ToolRegistry()
    for tool in (read_file_tool, run_code_tool, results_list_tool,
                 results_get_tool, write_paper_tool):
        registry.register(tool)

    agent = Agent(provider, registry, ctx, MODELING_SYSTEM,
                  compact_threshold_tokens=cfg["context"]["compact_threshold_tokens"],
                  max_steps=30, on_event=make_event_printer())

    task = ("Solve the problem described in problem.md end to end, then produce the "
            "compiled LaTeX report with a figure. Cite all numbers from results.")
    summary = agent.run(task)

    print("\nfinal summary:\n" + summary)
    pdf = workdir / "paper" / "main.pdf"
    print(f"\nworkdir: {workdir}")
    print(f"paper/main.pdf exists: {pdf.exists()}"
          + (f" ({pdf.stat().st_size} bytes)" if pdf.exists() else ""))
    print("results:", [p.name for p in (workdir / 'results').glob('*')] if (workdir / 'results').exists() else [])
    print("figures:", [p.name for p in (workdir / 'figures').glob('*')] if (workdir / 'figures').exists() else [])


if __name__ == "__main__":
    main()
