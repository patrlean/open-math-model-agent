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

DEEPSEEK_FLASH_MODEL = "deepseek-v4-flash"
DEEPSEEK_PRO_MODEL = "deepseek-v4-pro"
DEEPSEEK_REASONING_EFFORTS = ("low", "high", "max")
DEFAULT_DEEPSEEK_REASONING_EFFORT = "high"

DEEPSEEK_MODEL_OPTIONS: tuple[dict[str, Any], ...] = (
    {
        "id": DEEPSEEK_FLASH_MODEL,
        "label": "Flash Model",
        "description": "更快的默认模型",
        "is_default": True,
    },
    {
        "id": DEEPSEEK_PRO_MODEL,
        "label": "Pro Model",
        "description": "更强的复杂任务模型",
        "is_default": False,
    },
)

PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek",
        "default_model": DEEPSEEK_FLASH_MODEL,
        "default_base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model_options": DEEPSEEK_MODEL_OPTIONS,
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


def _normalize_model(provider: str, model: str) -> str:
    """Normalize known legacy DeepSeek names while preserving custom providers."""
    normalized = model.strip()
    if provider != "deepseek":
        return normalized
    aliases = {
        "deepseek-v4-flash": DEEPSEEK_FLASH_MODEL,
        "deepseek-v4-flash-0731": DEEPSEEK_FLASH_MODEL,
        DEEPSEEK_PRO_MODEL.lower(): DEEPSEEK_PRO_MODEL,
    }
    return aliases.get(normalized.lower(), normalized)


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
    model = _normalize_model(provider, str(data.get("model", "")))
    base_url = str(data.get("base_url", "")).strip().rstrip("/")
    reasoning_effort = str(
        data.get("reasoning_effort", DEFAULT_DEEPSEEK_REASONING_EFFORT)
    ).strip().lower()
    if reasoning_effort not in DEEPSEEK_REASONING_EFFORTS:
        reasoning_effort = DEFAULT_DEEPSEEK_REASONING_EFFORT
    if not model or not base_url:
        return {}
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "reasoning_effort": reasoning_effort,
    }


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


def validate_provider_selection(
    *,
    provider: str,
    model: str,
    base_url: str,
    reasoning_effort: str | None = None,
) -> dict[str, str]:
    """Validate non-secret provider metadata without persisting anything."""
    provider_id = provider.strip().lower()
    if provider_id not in PROVIDER_PRESETS:
        raise ValueError("不支持这个模型供应商。")
    normalized_model = _normalize_model(provider_id, model)
    if not normalized_model:
        raise ValueError("模型名称不能为空。")
    model_options = PROVIDER_PRESETS[provider_id].get("model_options", ())
    if model_options and normalized_model not in {
        str(option["id"]) for option in model_options
    }:
        raise ValueError("DeepSeek 模型只能选择 Flash Model 或 Pro Model。")
    normalized_base_url = _validate_base_url(base_url)
    normalized_effort = str(
        reasoning_effort or DEFAULT_DEEPSEEK_REASONING_EFFORT
    ).strip().lower()
    if normalized_effort not in DEEPSEEK_REASONING_EFFORTS:
        raise ValueError("思考强度只能选择 low、high 或 max。")
    return {
        "provider": provider_id,
        "model": normalized_model,
        "base_url": normalized_base_url,
        "reasoning_effort": normalized_effort,
    }


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
        "model": _normalize_model(provider, str(cfg.get("model", ""))),
        "base_url": str(cfg.get("base_url", "")),
        "reasoning_effort": str(
            cfg.get("reasoning_effort", DEFAULT_DEEPSEEK_REASONING_EFFORT)
        ),
        "api_key_configured": bool(key),
        "api_key_hint": f"••••{key[-4:]}" if key else None,
        "presets": [
            {
                "id": preset_id,
                "label": values["label"],
                "default_model": values["default_model"],
                "default_base_url": values["default_base_url"],
                "model_options": [
                    dict(option) for option in values.get("model_options", ())
                ],
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
    reasoning_effort: str | None = None,
    settings_path: Path = SETTINGS_PATH,
    env_path: Path = ENV_PATH,
) -> dict[str, Any]:
    """Validate and atomically persist a provider selection and optional key."""
    state = validate_provider_selection(
        provider=provider,
        model=model,
        base_url=base_url,
        reasoning_effort=(
            reasoning_effort or str(current_cfg.get(
                "reasoning_effort", DEFAULT_DEEPSEEK_REASONING_EFFORT
            ))
        ),
    )
    provider_id = state["provider"]
    preset = PROVIDER_PRESETS[provider_id]
    key_name = preset["api_key_env"]
    submitted_key = (api_key or "").strip()
    existing_key = os.environ.get(key_name, "").strip()
    if not submitted_key and not existing_key:
        raise ValueError(f"请先填写 {preset['label']} 的 API Key。")

    if submitted_key:
        _write_env_value(env_path, key_name, submitted_key)
        os.environ[key_name] = submitted_key

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings_path.with_name(f".{settings_path.name}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, settings_path)

    current_cfg.update(state)
    return provider_settings_payload(current_cfg)
