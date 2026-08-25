"""Estimate Tool Call and Tool Result token distributions from experiment logs.

The estimator deliberately matches the Context Inspector's display-only token
estimate. Provider-level usage is authoritative for whole requests, but it
cannot attribute tokens to individual tool messages.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from mathmodel.contextlog import _estimate_tokens


def _percentile(values: list[int], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _summary(values: list[int]) -> dict[str, int | float]:
    return {
        "count": len(values),
        "min": min(values),
        "q1": round(_percentile(values, 0.25), 2),
        "median": round(statistics.median(values), 2),
        "mean": round(statistics.fmean(values), 2),
        "q3": round(_percentile(values, 0.75), 2),
        "max": max(values),
        "total": sum(values),
    }


def _call_payload(name: str, arguments: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments,
        },
    }


def collect(root: Path) -> dict[str, Any]:
    calls: dict[str, list[int]] = defaultdict(list)
    results: dict[str, list[int]] = defaultdict(list)
    event_files = sorted(root.glob("**/workspace/events.jsonl"))
    malformed_lines = 0

    for path in event_files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed_lines += 1
                continue
            if event.get("kind") == "assistant":
                for raw_call in event.get("tool_calls") or []:
                    if not isinstance(raw_call, (list, tuple)) or len(raw_call) < 2:
                        continue
                    name = str(raw_call[0] or "unknown")
                    calls[name].append(
                        _estimate_tokens(_call_payload(name, raw_call[1]))
                    )
            elif event.get("kind") == "tool_result":
                name = str(event.get("name") or "unknown")
                results[name].append(_estimate_tokens(event.get("observation") or ""))

    tools = []
    for name in sorted(set(calls) | set(results)):
        call_values = calls.get(name, [])
        result_values = results.get(name, [])
        tools.append({
            "tool": name,
            "call": _summary(call_values) if call_values else None,
            "result": _summary(result_values) if result_values else None,
        })
    return {
        "schema_version": 1,
        "estimator": "context_inspector_ascii_div4_non_ascii_div1_5",
        "scope": str(root.resolve()),
        "event_files": len(event_files),
        "malformed_lines": malformed_lines,
        "tool_call_count": sum(len(values) for values in calls.values()),
        "tool_result_count": sum(len(values) for values in results.values()),
        "tools": tools,
    }


_FIELDS = ("count", "min", "q1", "median", "mean", "q3", "max", "total")


def _rows(report: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for tool in report["tools"]:
        row: dict[str, Any] = {"tool": tool["tool"]}
        for side in ("call", "result"):
            stats = tool[side] or {}
            for field in _FIELDS:
                row[f"{side}_{field}"] = stats.get(field, "")
        yield row


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Tool token distribution",
        "",
        (
            f"Estimated from {report['event_files']} event logs; "
            f"{report['tool_call_count']} calls and "
            f"{report['tool_result_count']} results."
        ),
        "",
        "The figures are message-level estimates, not provider-billed usage.",
        "",
        "| Tool | Side | N | Min | Q1 | Median | Mean | Q3 | Max | Total |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for tool in report["tools"]:
        for side in ("call", "result"):
            stats = tool[side]
            if not stats:
                continue
            values = " | ".join(str(stats[field]) for field in _FIELDS)
            lines.append(f"| `{tool['tool']}` | {side} | {values} |")
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "tool-token-stats.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    rows = list(_rows(report))
    fieldnames = ["tool", *(
        f"{side}_{field}"
        for side in ("call", "result")
        for field in _FIELDS
    )]
    with (output / "tool-token-stats.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    (output / "tool-token-stats.md").write_text(
        _markdown(report),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiments-root", default="experiments")
    parser.add_argument("--output", default="experiments/tool-token-stats")
    args = parser.parse_args()
    report = collect(Path(args.experiments_root))
    write_report(report, Path(args.output))
    print(json.dumps({
        "event_files": report["event_files"],
        "tool_call_count": report["tool_call_count"],
        "tool_result_count": report["tool_result_count"],
        "output": str(Path(args.output).resolve()),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
