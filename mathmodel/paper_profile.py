"""Competition-specific paper acceptance profiles.

Writing skills carry a small machine-readable ``paper`` block in their YAML
frontmatter.  Loading a skill persists that block in the conversation
workspace so paper tools and the verifier keep using the same limits after a
stop/resume cycle.  The global ``config.yaml`` paper section remains the
fallback when no competition writing skill has been selected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

PAPER_PROFILE_FILENAME = ".paper-profile.json"
_PAGE_KEYS = ("target_pages", "min_pages", "max_pages")
_TEMPLATE_NAMES = {"generic", "cumcm", "mcm-icm"}
_PAGE_COUNT_METRICS = {"total_pages", "counted_pages"}
_SKILL_PROFILE_DEFAULTS: dict[str, dict[str, str]] = {
    "cumcm-excellent-paper-writer": {
        "template": "cumcm",
        "page_count_metric": "counted_pages",
    },
    "mcm-icm-excellent-paper-writer": {
        "template": "mcm-icm",
        "page_count_metric": "counted_pages",
    },
}


def normalize_page_profile(value: object) -> dict[str, Any] | None:
    """Validate and normalize the paper fields declared by a writing skill.

    The historical function name is kept for compatibility.  In addition to
    the page range, a competition profile may lock an executable LaTeX template
    and choose whether acceptance counts the whole PDF or only the abstract and
    main body before appendices/references.
    """
    if not isinstance(value, Mapping):
        return None
    try:
        profile: dict[str, Any] = {key: int(value[key]) for key in _PAGE_KEYS}
    except (KeyError, TypeError, ValueError):
        return None
    target = profile["target_pages"]
    minimum = profile["min_pages"]
    maximum = profile["max_pages"]
    if minimum < 1 or not minimum <= target <= maximum:
        return None
    template = str(value.get("template") or "").strip()
    if template:
        if template not in _TEMPLATE_NAMES:
            return None
        profile["template"] = template

    page_count_metric = str(value.get("page_count_metric") or "").strip()
    if page_count_metric:
        if page_count_metric not in _PAGE_COUNT_METRICS:
            return None
        profile["page_count_metric"] = page_count_metric
    return profile


def activate_paper_profile(
    workdir: str | Path,
    *,
    skill: str,
    paper: object,
) -> dict[str, Any] | None:
    """Persist one skill's valid page profile and return the normalized values."""
    profile = normalize_page_profile(paper)
    if profile is None:
        return None
    destination = Path(workdir) / PAPER_PROFILE_FILENAME
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    payload = {"skill": skill, "paper": profile}
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(destination)
    return profile


def active_paper_profile(workdir: str | Path) -> dict[str, Any] | None:
    """Read the persisted competition profile, ignoring malformed state."""
    path = Path(workdir) / PAPER_PROFILE_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    profile = normalize_page_profile(payload.get("paper"))
    if profile is None:
        return None
    skill = str(payload.get("skill") or "")
    for key, value in _SKILL_PROFILE_DEFAULTS.get(skill, {}).items():
        profile.setdefault(key, value)
    return {"skill": skill, "paper": profile}


def resolve_paper_config(
    settings: Mapping[str, Any] | None,
    workdir: str | Path,
) -> dict[str, Any]:
    """Overlay the active competition page profile on global paper defaults."""
    configured = (settings or {}).get("paper", {})
    resolved = dict(configured) if isinstance(configured, Mapping) else {}
    active = active_paper_profile(workdir)
    if active is not None:
        resolved.update(active["paper"])
    return resolved
