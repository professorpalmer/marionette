"""Append-only context mode resolution for prefix KV-cache reuse.

Mirrors OMP's append-only-context-mode.ts auto-detection. Never raises.
"""
from __future__ import annotations

import os
import re
from urllib.parse import urlparse

_PROVIDER_MARKERS = (
    "ollama",
    "lm-studio",
    "lmstudio",
    "llama.cpp",
    "llamacpp",
    "vllm",
    "sglang",
    "deepseek",
)

_RFC1918_172 = re.compile(r"^172\.(1[6-9]|2[0-9]|3[01])\.")


def append_only_setting() -> str:
    """Read HARNESS_APPEND_ONLY_CONTEXT; default auto."""
    raw = os.environ.get("HARNESS_APPEND_ONLY_CONTEXT", "").strip().lower()
    if not raw:
        return "auto"
    if raw in ("on", "1", "true", "yes"):
        return "on"
    if raw in ("off", "0", "false", "no"):
        return "off"
    if raw == "auto":
        return "auto"
    return "auto"


def _hostname_local(hostname: str) -> bool:
    host = (hostname or "").lower()
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"):
        return True
    if host.startswith("10."):
        return True
    if host.startswith("192.168."):
        return True
    if _RFC1918_172.match(host):
        return True
    if host.endswith(".local"):
        return True
    return False


def _base_url_local(base_url: str) -> bool:
    if not base_url:
        return False
    try:
        hostname = urlparse(base_url).hostname or ""
    except Exception:
        return False
    return _hostname_local(hostname)


def _driver_name_local(driver_name: str) -> bool:
    name = (driver_name or "").lower()
    if not name:
        return False
    return any(marker in name for marker in _PROVIDER_MARKERS)


def _auto_enable(base_url: str, driver_name: str) -> bool:
    if _driver_name_local(driver_name):
        return True
    if _base_url_local(base_url):
        return True
    return False


def should_enable_append_only(setting: str, base_url: str, driver_name: str) -> bool:
    """Resolve append-only mode from setting, base URL, and driver name."""
    try:
        mode = (setting or "auto").strip().lower()
        if mode == "on":
            return True
        if mode == "off":
            return False
        return _auto_enable(base_url, driver_name)
    except Exception:
        return False
