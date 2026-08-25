"""Deterministic checks for web search/fetch and the experiment date cutoff.

Run with: ./.venv/bin/python -m scripts.check_web_search
Set MATHMODEL_LIVE_WEB_TEST=1 to additionally exercise the configured provider.
"""

from __future__ import annotations

from datetime import date
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from mathmodel.config import build_sandbox, load_config
from mathmodel.tools.base import ToolContext
from mathmodel.tools.web import (
    _cutoff_query,
    _direct_result_url,
    _extract_publication_date,
    _parse_duckduckgo_results,
    web_fetch_tool,
    web_search_tool,
)


_HTML_FIXTURE = """
<div class="result results_links">
  <h2><a class="result__a"
    href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fguide">
    Example Guide
  </a></h2>
  <a class="result__snippet">A concise result summary.</a>
</div>
"""


def main() -> None:
    assert web_search_tool.name == "web_search"
    assert _direct_result_url(
        "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fguide"
    ) == "https://example.com/guide"
    parsed = _parse_duckduckgo_results(_HTML_FIXTURE, 5)
    assert parsed == [{
        "title": "Example Guide",
        "url": "https://example.com/guide",
        "snippet": "A concise result summary.",
    }]
    assert _cutoff_query(
        "modeling after:2026-01-01 before:2099-01-01",
        date(2025, 9, 1),
    ) == "modeling before:2025-09-01"
    assert _extract_publication_date(
        '<meta property="article:published_time" content="2025-08-31T10:00:00Z">'
    ) == date(2025, 8, 31)

    class FakeResponse:
        def __init__(self, html: str, content_type: str = "text/html") -> None:
            self.text = html
            self.headers = {"content-type": content_type}

        def raise_for_status(self) -> None:
            return None

    with tempfile.TemporaryDirectory() as tmp:
        cfg = load_config()
        cfg["sandbox"] = "local"
        ctx = ToolContext(
            workdir=Path(tmp),
            sandbox=build_sandbox(cfg, tmp),
            settings=cfg,
        )
        captured_query: list[str] = []

        def fake_search(query: str, limit: int, timeout: float) -> list[dict[str, str]]:
            captured_query.append(query)
            return [
                {
                    "title": "Allowed old page",
                    "url": "https://example.com/old",
                    "snippet": "old",
                    "published": "2025-08-31",
                },
                {
                    "title": "Blocked boundary page",
                    "url": "https://example.com/boundary",
                    "snippet": "must not leak",
                    "published": "2025-09-01",
                },
                {
                    "title": "Allowed metadata page",
                    "url": "https://example.com/metadata",
                    "snippet": "metadata inspected",
                },
                {
                    "title": "Blocked undated page",
                    "url": "https://example.com/undated",
                    "snippet": "must not leak either",
                },
            ]

        def fake_publication_date(url: str, timeout: float) -> date | None:
            if url.endswith("/metadata"):
                return date(2024, 2, 3)
            return None

        with (
            patch("mathmodel.tools.web._duckduckgo_search", fake_search),
            patch("mathmodel.tools.web._fetch_publication_date", fake_publication_date),
        ):
            output = web_search_tool.handler(ctx, {
                "query": "Problem A solution after:2026-01-01",
                "max_results": 8,
            })
        assert captured_query == [
            "Problem A solution before:2025-09-01"
        ], captured_query
        assert "Allowed old page" in output, output
        assert "Allowed metadata page" in output, output
        assert "Blocked boundary page" not in output, output
        assert "Blocked undated page" not in output, output
        assert "before 2025-09-01" in output, output
        assert "Blocked 2 candidate result(s)" in output, output

        old_html = (
            '<meta property="article:published_time" content="2025-08-31">'
            "<main>safe old material</main>"
        )
        with patch("mathmodel.tools.web.httpx.get", return_value=FakeResponse(old_html)):
            fetched = web_fetch_tool.handler(ctx, {"url": "https://example.com/old"})
        assert "safe old material" in fetched, fetched
        assert "Published: 2025-08-31" in fetched, fetched

        new_html = (
            '<meta property="article:published_time" content="2025-09-01">'
            "<main>secret future solution</main>"
        )
        with patch("mathmodel.tools.web.httpx.get", return_value=FakeResponse(new_html)):
            blocked = web_fetch_tool.handler(ctx, {"url": "https://example.com/new"})
        assert blocked.startswith("[error] web content blocked"), blocked
        assert "secret future solution" not in blocked, blocked

        with patch(
            "mathmodel.tools.web.httpx.get",
            return_value=FakeResponse("<main>undated secret</main>"),
        ):
            undated = web_fetch_tool.handler(ctx, {"url": "https://example.com/unknown"})
        assert undated.startswith("[error] web content blocked"), undated
        assert "undated secret" not in undated, undated

        if os.environ.get("MATHMODEL_LIVE_WEB_TEST") == "1":
            live_cfg = load_config()
            live_cfg["web_search"] = {
                **live_cfg["web_search"],
                "content_cutoff": None,
                "require_verified_publication_date": False,
            }
            live_ctx = ToolContext(
                workdir=Path(tmp),
                sandbox=build_sandbox(live_cfg, tmp),
                settings=live_cfg,
            )
            live_output = web_search_tool.handler(live_ctx, {
                "query": "DeepSeek API documentation",
                "max_results": 2,
            })
            assert "[error]" not in live_output, live_output
            assert "URL: http" in live_output, live_output

    print("web search checks: passed")


if __name__ == "__main__":
    main()
