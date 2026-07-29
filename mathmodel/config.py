"""Configuration loading and the provider factory.

Reads `.env` (secrets) and `config.yaml` (model choice, thresholds, sandbox,
template). Keeping all of this data-driven is what makes the backend swappable.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .provider_settings import PROVIDER_PRESETS, read_provider_override
from .providers.base import Provider
from .providers.deepseek import DeepSeekProvider
from .providers.openai_compatible import OpenAICompatibleProvider
from .sandbox.base import Sandbox
from .sandbox.docker import DockerSandbox
from .sandbox.local import LocalSandbox

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "provider": "deepseek",
    "model": "DeepSeek-V4-Pro",
    "base_url": "https://api.deepseek.com",
    "context": {
        "compact_threshold_tokens": 1_000_000,
        "token_source": "api_usage",
    },
    "pricing": {
        # Rates supplied for the active DeepSeek plan, in CNY per 1M tokens.
        "deepseek_cny_per_million": {
            "cached_input": 0.025,
            "uncached_input": 3.0,
            "output": 6.0,
        },
    },
    "web_search": {
        "provider": "auto",
        "max_results": 8,
        "timeout_seconds": 20,
    },
    "verification": {
        "enabled": True,
        "max_attempts": 3,
        "max_steps": 80,
        "repair_steps_per_attempt": 48,
        "parallel_workers": 4,
        "triage_max_steps": 2,
        "synthesis_max_steps": 2,
        "provider_config": {},
    },
    "paper": {
        "target_pages": 20,
        "min_pages": 17,
        "max_pages": 20,
        "abstract_cjk_min_chars": 800,
        "abstract_english_min_words": 450,
        "abstract_fill_min_ratio": 0.72,
        "min_display_equations": 12,
        "figures_english_only": True,
    },
    "sandbox": "docker",
    "template": "generic",
}

_PROVIDERS = {
    "deepseek": DeepSeekProvider,
    "kimi": OpenAICompatibleProvider,
    "minimax": OpenAICompatibleProvider,
    "openai_compatible": OpenAICompatibleProvider,
}


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load config.yaml merged over DEFAULTS, and load .env into the environment."""
    load_dotenv(PROJECT_ROOT / ".env")

    cfg: dict[str, Any] = {
        **DEFAULTS,
        "context": dict(DEFAULTS["context"]),
        "pricing": {
            **DEFAULTS["pricing"],
            "deepseek_cny_per_million": dict(
                DEFAULTS["pricing"]["deepseek_cny_per_million"]
            ),
        },
        "web_search": dict(DEFAULTS["web_search"]),
        "verification": {
            **DEFAULTS["verification"],
            "provider_config": dict(DEFAULTS["verification"]["provider_config"]),
        },
        "paper": dict(DEFAULTS["paper"]),
    }
    cfg_path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    if cfg_path.exists():
        loaded = yaml.safe_load(cfg_path.read_text()) or {}
        for k, v in loaded.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    if path is None:
        cfg.update(read_provider_override())
    return cfg


def build_provider(cfg: dict[str, Any]) -> Provider:
    """Instantiate the provider named in the config."""
    name = cfg["provider"]
    if name not in _PROVIDERS:
        raise ValueError(f"Unknown provider '{name}'. Known: {list(_PROVIDERS)}")
    preset = PROVIDER_PRESETS[name]
    api_key = os.environ.get(preset["api_key_env"])
    return _PROVIDERS[name](
        model=cfg["model"],
        base_url=cfg.get("base_url"),
        api_key=api_key,
        pricing=cfg.get("pricing") if name == "deepseek" else None,
        request_observer=cfg.get("_context_request_observer"),
    )


def build_sandbox(cfg: dict[str, Any], workdir: str | Path) -> Sandbox:
    """Instantiate the sandbox named in the config, rooted at `workdir`."""
    name = cfg.get("sandbox", "docker")
    if name == "docker":
        return DockerSandbox(workdir)
    if name == "local":
        # Optional config: sandbox_python -> interpreter with the scientific stack.
        return LocalSandbox(workdir, python=cfg.get("sandbox_python"))
    raise ValueError(f"Unknown sandbox '{name}'. Known: docker, local")
