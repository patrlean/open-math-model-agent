"""Persistent, local-only settings for OpenAI-compatible model providers."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = PROJECT_ROOT / ".provider-settings.json"
ENV_PATH = PROJECT_ROOT / ".env"

PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "label": "DeepSeek",
        "default_model": "deepseek-v4-pro",
        "default_base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "kimi": {
        "label": "Kimi",
        "default_model": "kimi-k2.6",
        "default_base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "KIMI_API_KEY",
    },
    "minimax": {
        "label": "MiniMax",
        "default_model": "MiniMax-M2.7",
        "default_base_url": "https://api.minimaxi.com/v1",
        "api_key_env": "MINIMAX_API_KEY",
    },
    "openai_compatible": {
        "label": "其他兼容接口",
        "default_model": "",
        "default_base_url": "",
        "api_key_env": "OPENAI_COMPATIBLE_API_KEY",
    },
}


def read_provider_override(path: Path = SETTINGS_PATH) -> dict[str, str]:
    """Read non-secret provider metadata, ignoring malformed local state."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    provider = str(data.get("provider", "")).strip().lower()
    if provider not in PROVIDER_PRESETS:
        return {}
    model = str(data.get("model", "")).strip()
    base_url = str(data.get("base_url", "")).strip().rstrip("/")
    if not model or not base_url:
        return {}
    return {"provider": provider, "model": model, "base_url": base_url}


def _validate_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    parsed = urlparse(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Base URL 必须是有效的 HTTP(S) 地址，且不能包含查询参数。")
    return base_url


def _write_env_value(path: Path, name: str, value: str) -> None:
    """Update one .env value without disturbing unrelated local secrets."""
    lines = path.read_text().splitlines() if path.exists() else []
    encoded = json.dumps(value, ensure_ascii=False)
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=")
    replacement = f"{name}={encoded}"
    output: list[str] = []
    replaced = False
    for line in lines:
        if pattern.match(line):
            if not replaced:
                output.append(replacement)
                replaced = True
            continue
        output.append(line)
    if not replaced:
        output.append(replacement)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def provider_settings_payload(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return browser-safe settings; never serialize the API key itself."""
    provider = str(cfg.get("provider", "deepseek")).lower()
    preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["deepseek"])
    key = os.environ.get(preset["api_key_env"], "")
    return {
        "provider": provider,
        "model": str(cfg.get("model", "")),
        "base_url": str(cfg.get("base_url", "")),
        "api_key_configured": bool(key),
        "api_key_hint": f"••••{key[-4:]}" if key else None,
        "presets": [
            {
                "id": preset_id,
                "label": values["label"],
                "default_model": values["default_model"],
                "default_base_url": values["default_base_url"],
                "api_key_configured": bool(
                    os.environ.get(values["api_key_env"], "")
                ),
                "api_key_hint": (
                    f"••••{os.environ[values['api_key_env']][-4:]}"
                    if os.environ.get(values["api_key_env"], "")
                    else None
                ),
            }
            for preset_id, values in PROVIDER_PRESETS.items()
        ],
    }


def save_provider_settings(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key: str | None,
    current_cfg: dict[str, Any],
    settings_path: Path = SETTINGS_PATH,
    env_path: Path = ENV_PATH,
) -> dict[str, Any]:
    """Validate and atomically persist a provider selection and optional key."""
    provider_id = provider.strip().lower()
    if provider_id not in PROVIDER_PRESETS:
        raise ValueError("不支持这个模型供应商。")
    normalized_model = model.strip()
    if not normalized_model:
        raise ValueError("模型名称不能为空。")
    normalized_base_url = _validate_base_url(base_url)
    preset = PROVIDER_PRESETS[provider_id]
    key_name = preset["api_key_env"]
    submitted_key = (api_key or "").strip()
    existing_key = os.environ.get(key_name, "").strip()
    if not submitted_key and not existing_key:
        raise ValueError(f"请先填写 {preset['label']} 的 API Key。")

    if submitted_key:
        _write_env_value(env_path, key_name, submitted_key)
        os.environ[key_name] = submitted_key

    state = {
        "provider": provider_id,
        "model": normalized_model,
        "base_url": normalized_base_url,
    }
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings_path.with_name(f".{settings_path.name}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, settings_path)

    current_cfg.update(state)
    return provider_settings_payload(current_cfg)
