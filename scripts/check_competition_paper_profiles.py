"""Regression checks for competition-specific paper page profiles."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from mathmodel.paper_profile import active_paper_profile, resolve_paper_config
from mathmodel.tools.base import ToolContext
from mathmodel.tools.skills import make_load_skill_tool
from mathmodel.tools.write_paper import resolve_paper_template


def main() -> None:
    defaults = {
        "paper": {
            "target_pages": 20,
            "min_pages": 18,
            "max_pages": 20,
            "abstract_fill_min_ratio": 0.72,
        }
    }
    load_skill = make_load_skill_tool()

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        ctx = ToolContext(
            workdir=workdir,
            sandbox=None,  # type: ignore[arg-type]
            settings=defaults,
        )

        mcm_result = load_skill.handler(
            ctx,
            {"name": "mcm-icm-excellent-paper-writer"},
        )
        assert "target=25 pages" in mcm_result
        assert "accepted range=24-25 pages" in mcm_result
        assert "template=mcm-icm" in mcm_result
        assert "page_count_metric=counted_pages" in mcm_result
        assert resolve_paper_config(defaults, workdir) == {
            **defaults["paper"],
            "target_pages": 25,
            "min_pages": 24,
            "max_pages": 25,
            "template": "mcm-icm",
            "page_count_metric": "counted_pages",
        }
        assert active_paper_profile(workdir) == {
            "skill": "mcm-icm-excellent-paper-writer",
            "paper": {
                "target_pages": 25,
                "min_pages": 24,
                "max_pages": 25,
                "template": "mcm-icm",
                "page_count_metric": "counted_pages",
            },
        }
        assert resolve_paper_template(ctx, {"template": "generic"}) == "mcm-icm"

        # A fresh ToolContext models a stopped and resumed conversation. The
        # persisted workspace profile must still override global defaults.
        resumed = ToolContext(
            workdir=workdir,
            sandbox=None,  # type: ignore[arg-type]
            settings=defaults,
        )
        assert resolve_paper_config(resumed.settings, resumed.workdir)["min_pages"] == 24

        cumcm_result = load_skill.handler(
            resumed,
            {"name": "cumcm-excellent-paper-writer"},
        )
        assert "target=20 pages" in cumcm_result
        assert "accepted range=18-20 pages" in cumcm_result
        assert "template=cumcm" in cumcm_result
        assert resolve_paper_config(defaults, workdir)["min_pages"] == 18
        assert resolve_paper_config(defaults, workdir)["max_pages"] == 20
        assert resolve_paper_config(defaults, workdir)["template"] == "cumcm"
        assert (
            resolve_paper_config(defaults, workdir)["page_count_metric"]
            == "counted_pages"
        )
        assert resolve_paper_template(resumed, {"template": "mcm-icm"}) == "cumcm"
        assert defaults["paper"]["target_pages"] == 20

        legacy = workdir / "legacy"
        legacy.mkdir()
        (legacy / ".paper-profile.json").write_text(json.dumps({
            "skill": "cumcm-excellent-paper-writer",
            "paper": {
                "target_pages": 20,
                "min_pages": 18,
                "max_pages": 20,
            },
        }))
        migrated = active_paper_profile(legacy)
        assert migrated is not None
        assert migrated["paper"]["template"] == "cumcm"
        assert migrated["paper"]["page_count_metric"] == "counted_pages"

    print("competition paper profile checks: passed")


if __name__ == "__main__":
    main()
