"""Regression checks for local provider switching and secret redaction."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from mathmodel.provider_settings import (
    DEEPSEEK_FLASH_MODEL,
    DEEPSEEK_PRO_MODEL,
    provider_settings_payload,
    read_provider_override,
    save_provider_settings,
)


def main() -> None:
    original = os.environ.get("KIMI_API_KEY")
    original_deepseek = os.environ.get("DEEPSEEK_API_KEY")
    try:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / ".provider-settings.json"
            env_path = root / ".env"
            env_path.write_text("DEEPSEEK_API_KEY=keep-me\nUNRELATED=value\n")
            cfg = {
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "base_url": "https://api.deepseek.com",
            }
            secret = "sk-kimi-regression-secret"
            payload = save_provider_settings(
                provider="kimi",
                model="kimi-k2.6",
                base_url="https://api.moonshot.cn/v1/",
                api_key=secret,
                current_cfg=cfg,
                settings_path=state_path,
                env_path=env_path,
            )

            assert payload["provider"] == "kimi"
            assert payload["api_key_configured"] is True
            assert payload["api_key_hint"] == "••••cret"
            assert secret not in json.dumps(payload, ensure_ascii=False)
            assert read_provider_override(state_path) == {
                "provider": "kimi",
                "model": "kimi-k2.6",
                "base_url": "https://api.moonshot.cn/v1",
                "reasoning_effort": "high",
            }
            env_text = env_path.read_text()
            assert "DEEPSEEK_API_KEY=keep-me" in env_text
            assert "UNRELATED=value" in env_text
            assert secret in env_text
            assert secret not in state_path.read_text()

            safe = provider_settings_payload(cfg)
            assert secret not in json.dumps(safe, ensure_ascii=False)
            deepseek_preset = next(
                preset for preset in safe["presets"] if preset["id"] == "deepseek"
            )
            assert deepseek_preset["default_model"] == DEEPSEEK_FLASH_MODEL
            assert [option["id"] for option in deepseek_preset["model_options"]] == [
                DEEPSEEK_FLASH_MODEL,
                DEEPSEEK_PRO_MODEL,
            ]

            flash_payload = save_provider_settings(
                provider="deepseek",
                model="DeepSeek-V4-Flash-0731",
                base_url="https://api.deepseek.com",
                api_key="sk-deepseek-regression-secret",
                reasoning_effort="max",
                current_cfg=cfg,
                settings_path=state_path,
                env_path=env_path,
            )
            assert flash_payload["model"] == DEEPSEEK_FLASH_MODEL
            assert flash_payload["reasoning_effort"] == "max"
            assert read_provider_override(state_path)["model"] == DEEPSEEK_FLASH_MODEL
    finally:
        if original is None:
            os.environ.pop("KIMI_API_KEY", None)
        else:
            os.environ["KIMI_API_KEY"] = original
        if original_deepseek is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = original_deepseek

    print("provider switching checks passed")


if __name__ == "__main__":
    main()
