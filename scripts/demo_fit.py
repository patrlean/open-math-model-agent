"""End-to-end smoke test of the 'can run code' skeleton.

Gives the agent a tiny modeling task and checks that it: writes Python -> runs it
in the sandbox -> writes results/fit.json -> reads it back -> reports.

Uses the LocalSandbox (config sandbox=local) so it runs without Docker. Run:
    ./.venv/bin/python -m scripts.demo_fit
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

from mathmodel.agent.loop import Agent
from mathmodel.agent.prompts import SKELETON_SYSTEM
from mathmodel.config import PROJECT_ROOT, build_provider, build_sandbox, load_config
from mathmodel.tools.base import ToolContext, ToolRegistry
from mathmodel.tools.results_store import results_get_tool, results_list_tool
from mathmodel.tools.run_code import run_code_tool


def setup_run() -> Path:
    run_id = time.strftime("%Y%m%d-%H%M%S")
    workdir = PROJECT_ROOT / "workspace" / run_id
    (workdir / "data").mkdir(parents=True, exist_ok=True)
    # Noisy line: y = 3x + 2 + noise
    rng = np.random.default_rng(0)
    x = np.linspace(0, 10, 50)
    y = 3.0 * x + 2.0 + rng.normal(0, 0.5, size=x.size)
    np.savetxt(workdir / "data" / "points.csv", np.c_[x, y], delimiter=",", header="x,y", comments="")
    return workdir


def make_event_printer():
    def printer(kind: str, data: dict) -> None:
        if kind == "assistant":
            calls = data.get("tool_calls") or []
            tag = f"[step {data['step']}] tokens={data['total_tokens']}"
            if calls:
                print(f"{tag}  ->", ", ".join(f"{n}(...)" for n, _ in calls))
            if data.get("text"):
                print(f"{tag}  says: {data['text'][:200]}")
        elif kind == "tool_result":
            obs = data["observation"]
            print(f"    <{data['name']}> {obs[:300]}" + ("..." if len(obs) > 300 else ""))
        elif kind == "done":
            print("\n=== DONE ===")
        elif kind == "max_steps":
            print("\n=== HIT MAX STEPS ===")
    return printer


def main() -> None:
    cfg = load_config()
    # Sandbox choice: `python -m scripts.demo_fit [local|docker]` (default local).
    choice = sys.argv[1] if len(sys.argv) > 1 else "local"
    cfg["sandbox"] = choice
    if choice == "local":
        cfg["sandbox_python"] = sys.executable  # this venv has the scientific stack

    workdir = setup_run()
    print(f"workdir: {workdir}")

    provider = build_provider(cfg)
    sandbox = build_sandbox(cfg, workdir)
    ctx = ToolContext(workdir=workdir, sandbox=sandbox)

    registry = ToolRegistry()
    for tool in (run_code_tool, results_list_tool, results_get_tool):
        registry.register(tool)

    agent = Agent(
        provider=provider,
        registry=registry,
        ctx=ctx,
        system_prompt=SKELETON_SYSTEM,
        compact_threshold_tokens=cfg["context"]["compact_threshold_tokens"],
        on_event=make_event_printer(),
    )

    task = (
        "Fit a line y = a*x + b to data/points.csv by least squares. "
        "Write a, b, and the RMSE to results/fit.json. Verify by reading it back, "
        "then report the values."
    )
    summary = agent.run(task)

    print("\nfinal summary:\n" + summary)
    fit = workdir / "results" / "fit.json"
    print(f"\nresults/fit.json exists: {fit.exists()}")
    if fit.exists():
        print(fit.read_text())


if __name__ == "__main__":
    main()
