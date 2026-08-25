"""Regression checks for cache-aware token accounting and conversation cost."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from mathmodel.dashboard.server import _conversation_usage
from mathmodel.providers.base import usage_from_api
from mathmodel.providers.deepseek import (
    DeepSeekProvider,
    deepseek_request_pricing,
    deepseek_usage_cost_cny,
)


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
    provider.reasoning_effort = "high"
    provider._capability_key = ("https://api.deepseek.com", provider.model)
    provider.pricing = {
        "deepseek_cny_per_million": {
            "deepseek-v4-flash": {
                "cached_input": 0.02,
                "uncached_input": 1.0,
                "output": 2.0,
            },
            "deepseek-v4-pro": {
                "cached_input": 0.025,
                "uncached_input": 3.0,
                "output": 6.0,
            },
        },
        "deepseek_peak": {"enabled": False},
    }
    captured: dict = {}

    def create(**kwargs):
        captured.update(kwargs)
        return response

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create),
        ),
    )
    priced = provider.chat([{"role": "user", "content": "test"}]).usage
    assert priced.priced_tokens == 120
    assert priced.unpriced_tokens == 0
    assert priced.estimated_cost_cny == 0.000196875
    assert captured["reasoning_effort"] == "high"

    peak_pricing = {
        **provider.pricing,
        "deepseek_peak": {
            "enabled": True,
            "timezone": "Asia/Shanghai",
            "multiplier": 2.0,
            "windows": (("09:00", "12:00"), ("14:00", "18:00")),
        },
    }
    shanghai = ZoneInfo("Asia/Shanghai")
    flash_rates, morning_peak = deepseek_request_pricing(
        peak_pricing,
        "deepseek-v4-flash",
        datetime(2026, 8, 1, 9, 0, tzinfo=shanghai).timestamp(),
    )
    assert flash_rates == {
        "cached_input": 0.02,
        "uncached_input": 1.0,
        "output": 2.0,
    }
    assert morning_peak == 2.0
    assert deepseek_usage_cost_cny(
        deepseek,
        peak_pricing,
        "deepseek-v4-flash",
        datetime(2026, 8, 1, 9, 0, tzinfo=shanghai).timestamp(),
    ) == 0.000133
    _, noon_off_peak = deepseek_request_pricing(
        peak_pricing,
        "deepseek-v4-pro",
        datetime(2026, 8, 1, 12, 0, tzinfo=shanghai).timestamp(),
    )
    assert noon_off_peak == 1.0
    assert deepseek_usage_cost_cny(
        deepseek,
        peak_pricing,
        "deepseek-v4-pro",
        datetime(2026, 8, 1, 12, 0, tzinfo=shanghai).timestamp(),
    ) == 0.000196875
    _, afternoon_peak = deepseek_request_pricing(
        peak_pricing,
        "DeepSeek-V4-Pro",
        datetime(2026, 8, 1, 14, 0, tzinfo=shanghai).timestamp(),
    )
    assert afternoon_peak == 2.0
    _, evening_off_peak = deepseek_request_pricing(
        peak_pricing,
        "deepseek-v4-pro",
        datetime(2026, 8, 1, 18, 0, tzinfo=shanghai).timestamp(),
    )
    assert evening_off_peak == 1.0

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
                "kind": "external_model_usage",
                "tool": "describe_image",
                "provider": "moonshot",
                "model": "kimi-k3",
                # This call is already included in the lead state. The event is
                # provider-level detail and must not be counted a second time.
                "usage": _usage(
                    prompt=30,
                    completion=5,
                    cached=0,
                    uncached=30,
                    cost=0.005,
                ),
            },
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
        assert len(summary["external_model_usage"]) == 1
        kimi = summary["external_model_usage"][0]
        assert kimi["model"] == "kimi-k3"
        assert kimi["total_tokens"] == 35
        assert kimi["estimated_cost_cny"] == 0.005

    print("OK: cache-aware usage and per-conversation pricing")


if __name__ == "__main__":
    main()
