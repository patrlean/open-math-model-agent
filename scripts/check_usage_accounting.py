"""Regression checks for cache-aware token accounting and conversation cost."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from mathmodel.dashboard.server import _conversation_usage
from mathmodel.providers.base import usage_from_api
from mathmodel.providers.deepseek import DeepSeekProvider


def _usage(
    *,
    prompt: int,
    completion: int,
    cached: int,
    uncached: int,
    cost: float,
) -> dict:
    total = prompt + completion
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_input_tokens": cached,
        "uncached_input_tokens": uncached,
        "unclassified_input_tokens": 0,
        "estimated_cost_cny": cost,
        "priced_tokens": total,
        "unpriced_tokens": 0,
    }


def main() -> None:
    deepseek = usage_from_api(SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        prompt_cache_hit_tokens=75,
        prompt_cache_miss_tokens=25,
    ))
    assert deepseek.cached_input_tokens == 75
    assert deepseek.uncached_input_tokens == 25
    assert deepseek.unclassified_input_tokens == 0

    response = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(
                content="ok",
                reasoning_content=None,
                tool_calls=[],
            ),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            prompt_cache_hit_tokens=75,
            prompt_cache_miss_tokens=25,
        ),
    )
    provider = DeepSeekProvider.__new__(DeepSeekProvider)
    provider.model = "deepseek-v4-pro"
    provider._capability_key = ("https://api.deepseek.com", provider.model)
    provider.pricing = {
        "deepseek_cny_per_million": {
            "cached_input": 0.025,
            "uncached_input": 3.0,
            "output": 6.0,
        },
    }
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: response),
        ),
    )
    priced = provider.chat([{"role": "user", "content": "test"}]).usage
    assert priced.priced_tokens == 120
    assert priced.unpriced_tokens == 0
    assert priced.estimated_cost_cny == 0.000196875

    openai_style = usage_from_api(SimpleNamespace(
        prompt_tokens=90,
        completion_tokens=10,
        total_tokens=100,
        prompt_tokens_details=SimpleNamespace(cached_tokens=60),
    ))
    assert openai_style.cached_input_tokens == 60
    assert openai_style.uncached_input_tokens == 30

    unknown = usage_from_api(SimpleNamespace(
        prompt_tokens=40,
        completion_tokens=5,
        total_tokens=45,
    ))
    assert unknown.unclassified_input_tokens == 40
    assert unknown.unpriced_tokens == 45

    with tempfile.TemporaryDirectory() as temporary:
        workdir = Path(temporary)
        lead = _usage(
            prompt=100,
            completion=20,
            cached=75,
            uncached=25,
            cost=0.001,
        )
        (workdir / "session_state.json").write_text(json.dumps({
            "total_usage": lead,
        }))
        events = [
            {
                "kind": "routing_usage",
                "usage": _usage(
                    prompt=20,
                    completion=2,
                    cached=0,
                    uncached=20,
                    cost=0.0001,
                ),
            },
            {
                "kind": "subagent_end",
                "tokens": 55,
                "usage": _usage(
                    prompt=45,
                    completion=10,
                    cached=30,
                    uncached=15,
                    cost=0.0004,
                ),
            },
        ]
        verifier = _usage(
            prompt=70,
            completion=15,
            cached=50,
            uncached=20,
            cost=0.0007,
        )
        summary = _conversation_usage(
            workdir,
            events,
            {1: {"reported_total_tokens": 85, "usage": verifier}},
        )
        assert summary["cached_input_tokens"] == 155
        assert summary["uncached_input_tokens"] == 80
        assert summary["completion_tokens"] == 47
        assert summary["total_tokens"] == 282
        assert summary["estimated_cost_cny"] == 0.0022
        assert summary["cache_breakdown_complete"] is True
        assert summary["pricing_complete"] is True

    print("OK: cache-aware usage and per-conversation pricing")


if __name__ == "__main__":
    main()
