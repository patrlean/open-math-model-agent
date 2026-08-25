"""Deterministic checks for Kimi image inspection without making a paid call."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mathmodel.tools.base import ToolContext, ToolRegistry
from mathmodel.tools.describe_image import describe_image_tool


class _EstimateResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"data": {"total_tokens": 119}}


def main() -> None:
    captured: dict = {}

    def create_completion(**kwargs):
        captured.update(kwargs)
        usage = SimpleNamespace(
            prompt_tokens=120,
            completion_tokens=30,
            total_tokens=150,
            prompt_cache_hit_tokens=20,
            prompt_cache_miss_tokens=100,
        )
        message = SimpleNamespace(content="图中是一张包含坐标轴的折线图。")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=usage,
        )

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create_completion),
        )
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "assets").mkdir()
        (root / "assets" / "chart.png").write_bytes(b"not-decoded-by-mock")
        settings = {
            "vision": {
                "model": "kimi-k3",
                "base_url": "https://api.moonshot.cn/v1",
                "api_key_env": "MOONSHOT_API_KEY",
                "max_completion_tokens": 512,
                "pricing_cny_per_million": {
                    "cached_input": 2.0,
                    "uncached_input": 20.0,
                    "output": 100.0,
                },
            }
        }
        ctx = ToolContext(workdir=root, sandbox=SimpleNamespace(), settings=settings)
        registry = ToolRegistry()
        registry.register(describe_image_tool())

        with (
            patch.dict(os.environ, {"MOONSHOT_API_KEY": "test-only"}),
            patch(
                "mathmodel.tools.describe_image.httpx.post",
                return_value=_EstimateResponse(),
            ),
            patch("mathmodel.tools.describe_image.OpenAI", return_value=fake_client),
        ):
            observation = registry.dispatch(
                ctx,
                "describe_image",
                {"path": "assets/chart.png", "instruction": "描述图表。"},
            )

        payload = json.loads(observation)
        assert payload["description"] == "图中是一张包含坐标轴的折线图。"
        assert payload["input_token_estimate"] == 119
        assert captured["model"] == "kimi-k3"
        assert captured["max_completion_tokens"] == 512
        image_url = captured["messages"][1]["content"][0]["image_url"]["url"]
        assert image_url.startswith("data:image/png;base64,")

        records = ctx.take_model_usage_records()
        assert len(records) == 1
        usage, metadata = records[0]
        assert usage.cached_input_tokens == 20
        assert usage.uncached_input_tokens == 100
        assert usage.completion_tokens == 30
        assert abs(usage.estimated_cost_cny - 0.00504) < 1e-12
        assert metadata["provider"] == "moonshot"
        assert metadata["model"] == "kimi-k3"

        with patch.dict(os.environ, {"MOONSHOT_API_KEY": "test-only"}):
            blocked = registry.dispatch(
                ctx,
                "describe_image",
                {"path": "../outside.png"},
            )
        assert blocked.startswith("[error]") and "inside" in blocked

    print("OK: describe_image safety, request shape, usage, and CNY pricing")


if __name__ == "__main__":
    main()
