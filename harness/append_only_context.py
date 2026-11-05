"""Append-only context mode resolution for prefix-cache hygiene.

Under ``auto``, append-only is preferred for *all* agentic BYOK backends —
local KV-cache hosts *and* cloud APIs (OpenRouter, OpenAI, Anthropic, Gemini,
xAI, etc.). Mutating the system prompt each pilot step busts automatic prefix
cache and explicit Claude/Qwen breakpoints; append-only keeps the system
prefix stable and routes turn-varying context into the user trailer instead.

``on`` forces enable; ``off`` disables. Never raises.
"""
from __future__ import annotations

import os


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


def should_enable_append_only(setting: str) -> bool:
    """Resolve append-only mode from the setting. ``auto`` enables it for every backend."""
    try:
        mode = (setting or "auto").strip().lower()
        if mode == "on":
            return True
        if mode == "off":
            return False
        return True
    except Exception:
        return False
