"""Build a lightweight, deterministic index for verifier artifact bundles."""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MANIFEST_FILENAME = "artifact_manifest.json"
_TEXT_SUFFIXES = {
    ".c", ".cc", ".cpp", ".csv", ".h", ".hpp", ".jl", ".js", ".jsx",
    ".json", ".m", ".md", ".py", ".r", ".tex", ".toml", ".ts", ".tsx",
    ".txt", ".yaml", ".yml",
}
_LATEX_HEADING = re.compile(
    r"\\(section|subsection|subsubsection)\*?\{([^{}]+)\}"
)


def _role(relative: Path) -> str:
    path = relative.as_posix()
    if path == "problem.md":
        return "problem_statement"
    if path == ".paper-profile.json":
        return "paper_acceptance_profile"
    if relative.parts[:1] == ("src",):
        return "final_source"
    if relative.parts[:1] == ("results",):
        return "computed_result"
    if relative.parts[:1] == ("figures",):
        return "final_figure"
    if path == "paper/main.tex":
        return "paper_source"
    if path == "paper/main.pdf":
        return "final_paper"
    if relative.parts[:1] == ("paper",):
        return "paper_artifact"
    if relative.parts[:1] == ("data",):
        return "normalized_input"
    if relative.parts[:1] == ("assets",):
        return "input_asset"
    return "supporting_artifact"


def _python_symbols(text: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    return [
        {
            "kind": "class" if isinstance(node, ast.ClassDef) else "function",
            "name": node.name,
            "line": int(node.lineno),
        }
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ][:200]


def _text_metadata(path: Path, text: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {"line_count": len(text.splitlines())}
    suffix = path.suffix.lower()
    if suffix == ".py":
        metadata["symbols"] = _python_symbols(text)
    elif suffix == ".tex":
        metadata["headings"] = [
            {"level": match.group(1), "title": match.group(2),
             "line": text.count("\n", 0, match.start()) + 1}
            for match in _LATEX_HEADING.finditer(text)
        ][:200]
    elif suffix == ".json":
        try:
            value = json.loads(text)
            if isinstance(value, dict):
                metadata["top_level_keys"] = list(value)[:200]
            elif isinstance(value, list):
                metadata["top_level_type"] = "array"
                metadata["item_count"] = len(value)
        except json.JSONDecodeError:
            metadata["json_valid"] = False
    elif suffix in {".csv", ".tsv"}:
        try:
            delimiter = "\t" if suffix == ".tsv" else ","
            metadata["columns"] = next(csv.reader(
                text.splitlines()[:1], delimiter=delimiter
            ))[:200]
        except (csv.Error, StopIteration):
            metadata["columns"] = []
    return metadata


def build_artifact_manifest(workdir: Path) -> dict[str, Any]:
    """Index the immutable verification bundle without exposing source paths."""
    artifacts: list[dict[str, Any]] = []
    for path in sorted(workdir.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_FILENAME:
            continue
        relative = path.relative_to(workdir)
        data = path.read_bytes()
        record: dict[str, Any] = {
            "path": relative.as_posix(),
            "role": _role(relative),
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if path.suffix.lower() in _TEXT_SUFFIXES or path.name == "problem.md":
            record.update(_text_metadata(path, data.decode("utf-8", errors="replace")))
        artifacts.append(record)

    counts: dict[str, int] = {}
    for artifact in artifacts:
        role = str(artifact["role"])
        counts[role] = counts.get(role, 0) + 1
    return {
        "schema_version": 1,
        "purpose": "Final candidate verification bundle",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact_count": len(artifacts),
        "counts_by_role": counts,
        "artifacts": artifacts,
    }


def write_artifact_manifest(workdir: Path) -> Path:
    path = workdir / MANIFEST_FILENAME
    path.write_text(
        json.dumps(build_artifact_manifest(workdir), ensure_ascii=False, indent=2)
        + "\n"
    )
    return path
