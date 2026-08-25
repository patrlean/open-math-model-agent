"""Encrypted, account-scoped provider credentials for public workspaces."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

from .provider_settings import (
    PROVIDER_PRESETS,
    provider_settings_payload,
    validate_provider_selection,
)

SETTINGS_FILENAME = ".provider-settings.json"
DEFAULT_KEY_PATH = Path(__file__).resolve().parent.parent / ".credential-key"


def _credential_key_path() -> Path:
    configured = os.environ.get("MATHMODEL_CREDENTIAL_KEY_FILE", "").strip()
    return Path(configured) if configured else DEFAULT_KEY_PATH


def _fernet() -> Fernet:
    """Load or atomically create the server-only encryption key."""
    path = _credential_key_path()
    try:
        key = path.read_bytes().strip()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key = path.read_bytes().strip()
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(key + b"\n")
    os.chmod(path, 0o600)
    return Fernet(key)


def _read_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _safe_public_base_url(provider: str, base_url: str) -> None:
    """Reject local/private endpoints before a public user can trigger SSRF."""
    if provider != "openai_compatible":
        expected = str(PROVIDER_PRESETS[provider]["default_base_url"]).rstrip("/")
        if base_url.rstrip("/") != expected:
            raise ValueError("公共工作区仅允许使用该供应商的官方 API 地址。")
        return

    parsed = urlparse(base_url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("兼容接口必须使用不含账号信息的 HTTPS 地址。")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname or hostname == "localhost" or hostname.endswith(".local"):
        raise ValueError("兼容接口不能指向本机或内网地址。")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ValueError("兼容接口域名当前无法解析。") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("兼容接口不能解析到本机、内网或保留地址。")


def settings_payload(global_cfg: dict[str, Any], account_root: Path) -> dict[str, Any]:
    """Return browser-safe server/user mode state without decrypting a key."""
    state = _read_state(account_root / SETTINGS_FILENAME)
    mode = "user" if state.get("credential_mode") == "user" else "server"
    user = state.get("user") if isinstance(state.get("user"), dict) else {}
    selected = global_cfg if mode == "server" else user
    payload = provider_settings_payload(dict(selected or global_cfg))
    user_provider = str(user.get("provider", ""))
    ciphertext = str(user.get("api_key_ciphertext", ""))
    key_hint = str(user.get("api_key_hint", "")) or None
    for preset in payload["presets"]:
        owns_key = bool(ciphertext and preset["id"] == user_provider)
        preset["api_key_configured"] = owns_key
        preset["api_key_hint"] = key_hint if owns_key else None
    payload.update({
        "credential_mode": mode,
        "api_key_configured": (
            bool(payload.get("api_key_configured"))
            if mode == "server" else bool(ciphertext)
        ),
        "api_key_hint": None if mode == "server" else key_hint,
        "server_provider": str(global_cfg.get("provider", "deepseek")),
        "server_model": str(global_cfg.get("model", "")),
        "user_provider": str(user.get("provider", "")) or None,
        "user_model": str(user.get("model", "")) or None,
        "user_base_url": str(user.get("base_url", "")) or None,
        "user_reasoning_effort": str(user.get("reasoning_effort", "")) or None,
    })
    return payload


def save_account_settings(
    body: dict[str, Any],
    global_cfg: dict[str, Any],
    account_root: Path,
) -> dict[str, Any]:
    """Save a platform/user credential choice for exactly one account."""
    path = account_root / SETTINGS_FILENAME
    state = _read_state(path)
    mode = str(body.get("credential_mode", "")).strip().lower()
    if mode not in {"server", "user"}:
        raise ValueError("API 使用方式只能选择平台 API 或我的 API。")

    if mode == "server":
        state["credential_mode"] = "server"
        _write_state(path, state)
        return settings_payload(global_cfg, account_root)

    selection = validate_provider_selection(
        provider=str(body.get("provider", "")),
        model=str(body.get("model", "")),
        base_url=str(body.get("base_url", "")),
        reasoning_effort=(
            str(body["reasoning_effort"])
            if body.get("reasoning_effort") is not None else None
        ),
    )
    _safe_public_base_url(selection["provider"], selection["base_url"])
    previous = state.get("user") if isinstance(state.get("user"), dict) else {}
    submitted_key = str(body.get("api_key", "")).strip()
    same_provider = previous.get("provider") == selection["provider"]
    previous_ciphertext = str(previous.get("api_key_ciphertext", "")) if same_provider else ""
    if not submitted_key and not previous_ciphertext:
        raise ValueError("请填写这个供应商的 API Key。")
    if submitted_key:
        ciphertext = _fernet().encrypt(submitted_key.encode()).decode()
        key_hint = f"••••{submitted_key[-4:]}"
    else:
        ciphertext = previous_ciphertext
        key_hint = str(previous.get("api_key_hint", ""))
    state.update({
        "credential_mode": "user",
        "user": {
            **selection,
            "api_key_ciphertext": ciphertext,
            "api_key_hint": key_hint,
        },
    })
    _write_state(path, state)
    return settings_payload(global_cfg, account_root)


def runtime_override(account_root: Path) -> dict[str, Any]:
    """Decrypt an account key only while preparing that account's agent run."""
    state = _read_state(account_root / SETTINGS_FILENAME)
    if state.get("credential_mode") != "user":
        return {}
    user = state.get("user") if isinstance(state.get("user"), dict) else {}
    ciphertext = str(user.get("api_key_ciphertext", ""))
    if not ciphertext:
        raise ValueError("你的 API Key 尚未配置，请先在设置中保存。")
    try:
        api_key = _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise ValueError("你的 API Key 无法解密，请在设置中重新填写。") from exc
    selection = validate_provider_selection(
        provider=str(user.get("provider", "")),
        model=str(user.get("model", "")),
        base_url=str(user.get("base_url", "")),
        reasoning_effort=str(user.get("reasoning_effort", "high")),
    )
    return {**selection, "_api_key": api_key}
