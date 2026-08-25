"""Lazy workspace ingestion controlled by the lead Agent.

The dashboard keeps the exact user text and uploaded file paths outside the tool
arguments.  When the lead decides that a conversation is a concrete modeling
task, one parameter-free call normalizes that captured input into ``problem.md``
and the usual ``data/`` / ``assets/`` directories.  Keeping the schema static is
important for provider prompt caching, and keeping the source payload server-side
prevents the model from accidentally paraphrasing or truncating the problem.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Iterable

from ..ingest.ingest import IngestReport, ingest
from ..project_state import project_view
from .base import Tool, ToolContext

PENDING_MATERIALS_DIR = ".pending_materials"


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    os.replace(temporary, path)


def make_ingest_problem_tool(
    *,
    problem_text: str = "",
    upload_paths: Iterable[str | Path] = (),
    on_ingested: Callable[[IngestReport], None] | None = None,
) -> Tool:
    """Bind one dashboard turn's immutable source inputs to an ingest tool."""

    captured_text = problem_text
    captured_paths = tuple(Path(path) for path in upload_paths)
    lock = threading.Lock()
    consumed = False

    def _ingest_problem(ctx: ToolContext, args: dict) -> str:
        nonlocal consumed
        del args
        with lock:
            if consumed:
                return (
                    "Current-turn materials were already ingested. Read "
                    "problem.md instead of calling ingest_problem again."
                )
            if not captured_text.strip() and not captured_paths:
                if (ctx.workdir / "problem.md").is_file():
                    return (
                        "No new current-turn materials need ingestion; the modeling "
                        "workspace already contains problem.md."
                    )
                return "[error] no user text or uploaded materials are available to ingest"

            is_supplement = (ctx.workdir / "problem.md").is_file()
            staging_id = f"mat_{uuid.uuid4().hex[:12]}"
            target_workdir = (
                ctx.workdir / PENDING_MATERIALS_DIR / staging_id
                if is_supplement else ctx.workdir
            )
            report = ingest(
                list(captured_paths),
                target_workdir,
                problem_text=captured_text,
            )
            consumed = True
            if on_ingested is not None and not is_supplement:
                on_ingested(report)

            if is_supplement:
                manifest = {
                    "id": staging_id,
                    "status": "staged",
                    "created_at": time.time(),
                    "source_files": [path.name for path in captured_paths],
                    "has_user_text": bool(captured_text.strip()),
                    "data_files": report.data_files,
                    "asset_files": report.asset_files,
                }
                _atomic_json(target_workdir / "manifest.json", manifest)
                details = [
                    f"Supplemental materials staged as {staging_id}; canonical "
                    "problem.md/data/assets are unchanged.",
                    f"Inspect {PENDING_MATERIALS_DIR}/{staging_id}/problem.md and "
                    "its staged data/assets to judge impact.",
                ]
            else:
                details = ["Modeling workspace activated; generated problem.md."]
            if report.data_files:
                details.append("data: " + ", ".join(report.data_files))
            if report.asset_files:
                details.append("assets: " + ", ".join(report.asset_files))
            if report.skipped:
                details.append(
                    "skipped unsupported uploads: "
                    + ", ".join(Path(path).name for path in report.skipped)
                )
            if is_supplement:
                details.append(
                    "If these materials should change the project, obtain a "
                    "change_confirmation first; after confirmation call "
                    f"promote_materials with staging_id={staging_id}."
                )
            else:
                details.append("Read problem.md next, then create the modeling plan.")
            return "\n".join(details)

    return Tool(
        name="ingest_problem",
        description=(
            "Activate the modeling workflow for the current conversation turn. "
            "It uses the exact user message and uploaded materials already held "
            "by the backend. For a new project it creates problem.md/data/assets. "
            "For an existing project it only stages supplemental materials for "
            "inspection and does not mutate canonical files; promote them only "
            "after the user confirms a revision. Do not call it for ordinary conversation."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=_ingest_problem,
    )


def make_promote_materials_tool() -> Tool:
    """Promote one staged material package into a confirmed revision."""

    def _promote(ctx: ToolContext, args: dict) -> str:
        staging_id = str(args.get("staging_id") or "").strip()
        if not staging_id or not staging_id.startswith("mat_"):
            return "[error] invalid staging_id"
        staged = ctx.workdir / PENDING_MATERIALS_DIR / staging_id
        manifest_path = staged / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            return "[error] staged material package not found"
        if manifest.get("status") == "promoted":
            return (
                "Materials were already promoted into revision "
                f"{manifest.get('revision_id', 'unknown')}."
            )
        if manifest.get("status") != "staged":
            return "[error] staged material package is not promotable"

        project = project_view(ctx.workdir)
        active = project.get("active_revision") or {}
        if project.get("active_revision_id") == project.get("current_revision_id"):
            return "[error] no confirmed draft revision exists; ask the user first"
        request_id = active.get("change_request_id")
        request = next(
            (
                item for item in project.get("change_requests", [])
                if item.get("id") == request_id
            ),
            None,
        )
        if not request or request.get("status") != "confirmed":
            return "[error] the active revision has not been confirmed by the user"

        revision_id = str(active["id"])
        replacements: dict[str, str] = {}
        promoted_files: list[str] = []
        for folder in ("data", "assets"):
            source_root = staged / folder
            if not source_root.is_dir():
                continue
            target_root = ctx.workdir / folder
            target_root.mkdir(exist_ok=True)
            for source in sorted(source_root.rglob("*")):
                if not source.is_file():
                    continue
                relative = source.relative_to(source_root)
                target = target_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target = target.with_name(
                        f"{target.stem}_{revision_id}{target.suffix}"
                    )
                shutil.copy2(source, target)
                old_reference = f"{folder}/{relative.as_posix()}"
                new_reference = target.relative_to(ctx.workdir).as_posix()
                replacements[old_reference] = new_reference
                promoted_files.append(new_reference)

        staged_problem = (staged / "problem.md").read_text(errors="replace")
        if staged_problem.startswith("# Problem Materials"):
            staged_problem = staged_problem[len("# Problem Materials"):].lstrip()
        for before, after in replacements.items():
            staged_problem = staged_problem.replace(before, after)
        canonical_problem = ctx.workdir / "problem.md"
        existing = canonical_problem.read_text(errors="replace")
        canonical_problem.write_text(
            existing.rstrip()
            + "\n\n---\n\n"
            + f"## Supplemental materials promoted in {revision_id}\n\n"
            + staged_problem.rstrip()
            + "\n"
        )

        manifest.update({
            "status": "promoted",
            "promoted_at": time.time(),
            "revision_id": revision_id,
            "promoted_files": promoted_files,
            "path_replacements": replacements,
        })
        _atomic_json(manifest_path, manifest)
        return (
            f"Promoted {staging_id} into {revision_id}; canonical problem.md "
            f"updated and {len(promoted_files)} data/asset file(s) copied."
        )

    return Tool(
        name="promote_materials",
        description=(
            "Promote an already-inspected supplemental-material staging package "
            "into canonical problem.md/data/assets. This tool enforces that the "
            "user has confirmed a new draft revision. Call it only after a "
            "change_confirmation returns confirm."
        ),
        parameters={
            "type": "object",
            "properties": {
                "staging_id": {
                    "type": "string",
                    "description": "The mat_... id returned by ingest_problem.",
                },
            },
            "required": ["staging_id"],
            "additionalProperties": False,
        },
        handler=_promote,
    )
