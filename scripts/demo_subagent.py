"""Live test of subagent delegation: two independent sub-problems that the lead
should each hand to a spawn_subagent, keeping its own context clean.

Run:  ./.venv/bin/python -m scripts.demo_subagent docker
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from mathmodel.agent.build import build_agent
from mathmodel.config import PROJECT_ROOT, load_config
from scripts.demo_fit import make_event_printer

os.environ["PATH"] = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"


def make_event_printer_sub():
    base = make_event_printer()
    def printer(kind, data):
        if kind == "tool_result" and data["name"] == "spawn_subagent":
            print("    <spawn_subagent> RETURNED:", data["observation"][:200].replace("\n", " "))
        else:
            base(kind, data)
    return printer


def main() -> None:
    cfg = load_config()
    choice = sys.argv[1] if len(sys.argv) > 1 else "docker"
    cfg["sandbox"] = choice
    if choice == "local":
        cfg["sandbox_python"] = sys.executable

    run_id = time.strftime("%Y%m%d-%H%M%S")
    workdir = PROJECT_ROOT / "workspace" / run_id
    (workdir / "data").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(3)
    x = np.linspace(0, 5, 30)
    pd.DataFrame({"x": x, "y": 4 * x + 1 + rng.normal(0, 0.3, x.size)}).to_csv(workdir / "data" / "q1.csv", index=False)
    pd.DataFrame({"x": x, "y": 2 * x**2 - 3 + rng.normal(0, 0.5, x.size)}).to_csv(workdir / "data" / "q2.csv", index=False)

    agent = build_agent(cfg, workdir, max_steps=25, on_event=make_event_printer_sub())
    task = (
        "There are two INDEPENDENT sub-problems. Delegate EACH to its own subagent:\n"
        "- q1: fit y = a*x + b to data/q1.csv; write results/q1.json (a, b, rmse).\n"
        "- q2: fit y = a*x^2 + c to data/q2.csv; write results/q2.json (a, c, rmse).\n"
        "Maintain plan.md. After both subagents finish, read both result files and "
        "report a combined summary. No paper needed."
    )
    summary = agent.run(task)

    print("\nfinal summary:\n" + summary)
    print("\ntotal lead tokens:", agent.total_usage.total_tokens)
    for f in ("plan.md", "decisions.md", "results/q1.json", "results/q2.json"):
        p = workdir / f
        print(f"{f}: {'OK' if p.exists() else 'MISSING'}")
    print("workdir:", workdir)


if __name__ == "__main__":
    main()
