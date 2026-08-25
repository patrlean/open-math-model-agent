"""Deterministic checks for read_file and results_get pagination."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from mathmodel.tools.base import ToolContext
from mathmodel.tools.read_file import read_file_tool
from mathmodel.tools.results_store import results_get_tool


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        ctx = ToolContext(workdir=workdir, sandbox=None)
        source = workdir / "source.txt"
        source.write_text("\n".join(f"source line {number}" for number in range(1, 206)))

        first = read_file_tool.handler(ctx, {"path": "source.txt"})
        assert "total_lines: 205" in first
        assert "returned_lines: 1-160" in first
        assert "has_more_before: false" in first
        assert "has_more_after: true" in first
        assert "next_start_line: 161" in first
        assert "    1: source line 1" in first
        assert "source line 205" not in first
        assert "truncated; full output in logs" not in first

        second = read_file_tool.handler(ctx, {
            "path": "source.txt",
            "start_line": 161,
            "end_line": 205,
        })
        assert "returned_lines: 161-205" in second
        assert "has_more_before: true" in second
        assert "has_more_after: false" in second
        assert "next_start_line: null" in second
        assert "  205: source line 205" in second

        results = workdir / "results"
        results.mkdir()
        (results / "large.json").write_text(json.dumps({
            "values": list(range(220)),
            "status": "complete",
        }))
        result_first = results_get_tool.handler(ctx, {
            "path": "results/large.json",
        })
        assert "view: pretty_json" in result_first
        assert "returned_lines: 1-160" in result_first
        assert "has_more_after: true" in result_first
        assert "next_start_line: 161" in result_first

        result_second = results_get_tool.handler(ctx, {
            "path": "results/large.json",
            "start_line": 161,
        })
        assert "returned_lines: 161-" in result_second
        assert "has_more_before: true" in result_second
        assert "\"status\": \"complete\"" in result_second

        properties = results_get_tool.parameters["properties"]
        assert {"path", "start_line", "end_line"} <= set(properties)

    print("file pagination checks: passed")


if __name__ == "__main__":
    main()
