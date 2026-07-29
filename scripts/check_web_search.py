"""Deterministic and live smoke checks for the general web_search tool.

Run with: ./.venv/bin/python -m scripts.check_web_search
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from mathmodel.config import build_sandbox, load_config
from mathmodel.tools.base import ToolContext
from mathmodel.tools.web import (
    _direct_result_url,
    _parse_duckduckgo_results,
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

    with tempfile.TemporaryDirectory() as tmp:
        cfg = load_config()
        cfg["sandbox"] = "local"
        ctx = ToolContext(
            workdir=Path(tmp),
            sandbox=build_sandbox(cfg, tmp),
            settings=cfg,
        )
        output = web_search_tool.handler(ctx, {
            "query": "DeepSeek API documentation",
            "max_results": 2,
        })
        assert "[error]" not in output, output
        assert "URL: http" in output, output
        assert output.count("\n   URL: ") <= 2
        assert "untrusted" in output

    print("web search checks: passed")


if __name__ == "__main__":
    main()
