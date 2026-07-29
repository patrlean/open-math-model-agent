"""Regression check: parallel plan updates must preserve valid JSON and every status."""

from __future__ import annotations

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mathmodel.tools.base import ToolContext
from mathmodel.tools.plan import plan_write_tool, set_task_status_tool


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        workdir = Path(temp)
        # Plan tools do not access the sandbox; the test only exercises plan state.
        ctx = ToolContext(workdir=workdir, sandbox=None)  # type: ignore[arg-type]
        ids = ["q1", "q2", "q3", "q4", "paper"]
        plan_write_tool.handler(ctx, {"tasks": [{"id": task_id, "title": task_id} for task_id in ids]})

        with ThreadPoolExecutor(max_workers=len(ids)) as pool:
            results = list(pool.map(
                lambda task_id: set_task_status_tool.handler(
                    ctx, {"id": task_id, "status": "done", "result": f"{task_id} complete"}
                ),
                ids,
            ))

        data = json.loads((workdir / "plan.json").read_text())
        tasks = {task["id"]: task for task in data["tasks"]}
        assert all("-> done." in result for result in results)
        assert set(tasks) == set(ids)
        assert all(task["status"] == "done" for task in tasks.values())
        print("parallel plan updates: OK")


if __name__ == "__main__":
    main()
