"""Skill discovery + on-demand loading (lazy context, Claude-Skills-style).

A "skill" is a directory under `skills/<name>/` with a `SKILL.md` whose
frontmatter carries `name` + `description`. Only that name+description pair is
ever resident in the system prompt (see `render_skill_index`) -- the full
SKILL.md body, and any supporting file under the skill's own directory
(templates/, checklists/, ...), is read from disk only when the agent calls
`load_skill` / `load_skill_file`, so an unused competition's writing guide
never occupies context.

README.md inside a skill directory is documentation for humans installing the
skill, not runtime content -- it is never loaded into context.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .base import Tool, ToolContext, tail

SKILLS_ROOT = Path(__file__).resolve().parent.parent.parent / "skills"

_FRONTMATTER_DELIM = "---"


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    path: Path


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a SKILL.md into (frontmatter dict, body). Empty dict if none."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_DELIM:
            meta = yaml.safe_load("\n".join(lines[1:i])) or {}
            body = "\n".join(lines[i + 1:]).lstrip("\n")
            return meta, body
    return {}, text


def discover_skills(root: Path = SKILLS_ROOT) -> list[SkillMeta]:
    """Scan `root` for `*/SKILL.md` and return their (name, description) index.

    A skill with a missing/unparseable frontmatter is skipped rather than
    crashing discovery for the rest.
    """
    if not root.is_dir():
        return []
    out: list[SkillMeta] = []
    for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue
        meta, _ = _parse_frontmatter(skill_file.read_text(errors="replace"))
        name = meta.get("name")
        description = meta.get("description")
        if not name or not description:
            continue
        out.append(SkillMeta(name=name, description=description, path=skill_dir))
    return out


def render_skill_index(root: Path = SKILLS_ROOT) -> str:
    """Render the compact index to embed in a system prompt: name + one-liner."""
    skills = discover_skills(root)
    if not skills:
        return ""
    lines = [f"- {s.name}: {s.description}" for s in skills]
    return "\n".join(lines)


def _find_skill(root: Path, name: str) -> Path | None:
    for s in discover_skills(root):
        if s.name == name:
            return s.path
    return None


def _list_other_files(skill_dir: Path) -> list[str]:
    """Files inside a skill dir worth pointing the model at via load_skill_file."""
    skip = {"SKILL.md", "README.md"}
    return sorted(
        str(p.relative_to(skill_dir))
        for p in skill_dir.rglob("*")
        if p.is_file() and p.name not in skip
    )


def make_load_skill_tool(root: Path = SKILLS_ROOT) -> Tool:
    def _load_skill(ctx: ToolContext, args: dict) -> str:
        name = args["name"]
        skill_dir = _find_skill(root, name)
        if skill_dir is None:
            available = ", ".join(s.name for s in discover_skills(root)) or "(none)"
            return f"[error] unknown skill '{name}'. Available: {available}"
        _, body = _parse_frontmatter((skill_dir / "SKILL.md").read_text(errors="replace"))
        others = _list_other_files(skill_dir)
        out = body
        if others:
            out += (
                "\n\n---\n[other files in this skill, fetch with "
                f"load_skill_file(skill='{name}', path=...) if referenced above]\n"
                + "\n".join(others)
            )
        return out

    return Tool(
        name="load_skill",
        description="Load the full guide for a named skill (see the skill index "
        "in the system prompt for available names + descriptions). Call this "
        "once, when you reach the stage that skill covers (e.g. right before "
        "writing the paper) -- not earlier, so unused skills never enter context.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name, e.g. "
                          "'cumcm-excellent-paper-writer'."}
            },
            "required": ["name"],
        },
        handler=_load_skill,
    )


def make_load_skill_file_tool(root: Path = SKILLS_ROOT) -> Tool:
    def _load_skill_file(ctx: ToolContext, args: dict) -> str:
        name = args["skill"]
        rel = args["path"]
        skill_dir = _find_skill(root, name)
        if skill_dir is None:
            available = ", ".join(s.name for s in discover_skills(root)) or "(none)"
            return f"[error] unknown skill '{name}'. Available: {available}"
        p = (skill_dir / rel).resolve()
        if not str(p).startswith(str(skill_dir.resolve())):
            return f"[error] path escapes skill directory: {rel}"
        if not p.is_file():
            return f"[error] not found in skill '{name}': {rel}"
        return f"{name}/{rel}:\n{tail(p.read_text(errors='replace'), max_lines=400, max_chars=16000)}"

    return Tool(
        name="load_skill_file",
        description="Load one supporting file (a template or checklist) referenced "
        "by a skill you already loaded with load_skill, e.g. path='templates/"
        "summary-sheet-template.md'.",
        parameters={
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "Skill name."},
                "path": {"type": "string", "description": "Path relative to the "
                          "skill's own directory, as listed by load_skill."},
            },
            "required": ["skill", "path"],
        },
        handler=_load_skill_file,
    )
