"""Long-horizon run on the 2025 CUMCM Problem A (smoke-screen deployment strategy).

Ingests the problem PDF + the three result*.xlsx templates, copies the templates
into the workdir so the agent can fill them, and runs the lead agent with a
generous step budget. Streams a compact event log and mirrors it to a file.

Run:  ./.venv/bin/python -m scripts.run_a_problem
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from mathmodel.agent.build import build_agent
from mathmodel.config import PROJECT_ROOT, load_config
from mathmodel.ingest.ingest import ingest
from mathmodel.runlog import JsonlLogger, compose

os.environ["PATH"] = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"

PROBLEM_DIR = Path("/Users/tianyou/Documents/文稿 - tianyou的Mac mini/mathmodel/knowledge-base/2025国赛/A题")


def make_logger(log_path: Path):
    def log(kind: str, data: dict) -> None:
        line = None
        if kind == "assistant":
            calls = data.get("tool_calls") or []
            tag = f"[step {data['step']}] tok={data['total_tokens']}"
            if calls:
                line = f"{tag} -> " + ", ".join(n for n, _ in calls)
            elif data.get("text"):
                line = f"{tag} says: {data['text'][:160]}"
        elif kind == "tool_result":
            obs = data["observation"].replace("\n", " ")
            line = f"    <{data['name']}> {obs[:220]}"
        elif kind == "compact_start":
            line = f"    [compacting @ {data['total_tokens']} tok: summarize {data['summarizing']}, keep {data['keeping']}]"
        elif kind == "done":
            line = "=== DONE ==="
        elif kind == "max_steps":
            line = "=== HIT MAX STEPS ==="
        if line:
            print(line, flush=True)
            with log_path.open("a") as f:
                f.write(line + "\n")
    return log


def main() -> None:
    cfg = load_config()
    cfg["sandbox"] = "docker"

    run_id = "A-" + time.strftime("%Y%m%d-%H%M%S")
    workdir = PROJECT_ROOT / "workspace" / run_id
    workdir.mkdir(parents=True, exist_ok=True)

    inputs = [PROBLEM_DIR / "A题.pdf"]
    for name in ("result1.xlsx", "result2.xlsx", "result3.xlsx"):
        src = PROBLEM_DIR / "附件" / name
        if src.exists():
            inputs.append(src)
            shutil.copy2(src, workdir / name)  # writable copy for the agent to fill

    report = ingest(inputs, workdir)
    print("ingested -> problem.md; data_files:", report.data_files, flush=True)

    log_path = workdir / "run.log"
    on_event = compose(make_logger(log_path), JsonlLogger(workdir / "events.jsonl"))
    agent = build_agent(cfg, workdir, max_steps=80, sub_max_steps=60,
                        on_event=on_event)

    task = (
        "Solve this mathematical-modeling problem (2025 CUMCM Problem A) end to end, "
        "in Chinese. problem.md has the full statement (problems 1-5, increasing in "
        "difficulty). Work through them in order and maintain plan.md.\n\n"
        "For each sub-problem's heavy computation/optimization, delegate to a "
        "spawn_subagent with a clear deliverable and a time budget (probe tractability "
        "on a reduced instance before a long solve; log_decision when you switch method).\n\n"
        "Problems 3/4/5 require saving results into result1.xlsx / result2.xlsx / "
        "result3.xlsx (writable copies are in the workdir root; match the template "
        "columns; use openpyxl/pandas).\n\n"
        "When all solvable sub-problems are done and their numbers verified from "
        "results/, write a Chinese LaTeX report with write_paper (cjk=true): for each "
        "problem give the model, method, key results (cite via \\VAR{results[...]}) and "
        "a figure where useful."
    )

    t0 = time.time()
    summary = agent.run(task)
    dt = time.time() - t0

    print(f"\n=== finished in {dt/60:.1f} min, lead tokens={agent.total_usage.total_tokens} ===", flush=True)
    print("final summary:\n" + (summary or "(none)"))
    print("\nworkdir:", workdir)
    for f in ("plan.md", "decisions.md", "result1.xlsx", "result2.xlsx", "result3.xlsx",
              "paper/main.pdf"):
        p = workdir / f
        print(f"  {f}: {'OK' if p.exists() else 'MISSING'}")
    if (workdir / "results").exists():
        print("  results:", [p.name for p in (workdir / "results").glob("*")])


if __name__ == "__main__":
    main()
