"""Hermes-style reasoning effort levels for Codex Responses (and settings UI).

UI labels: None / Low / Medium / High / Extra High / Max
Codex API: omit reasoning for None; otherwise low|medium|high|xhigh|max.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

# Canonical stored values (also used in env_settings.json via HARNESS_CODEX_REASONING_EFFORT).
REASONING_EFFORT_LEVELS: Tuple[str, ...] = (
    "none", "low", "medium", "high", "xhigh", "max",
)

DEFAULT_CODEX_REASONING_EFFORT = "low"

REASONING_EFFORT_LABELS = {
    "none": "None",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra High",
    "max": "Max",
}

# Aliases users or older builds might send.
_ALIASES = {
    "": DEFAULT_CODEX_REASONING_EFFORT,
    "off": "none",
    "0": "none",
    "minimal": "none",
    "extra_high": "xhigh",
    "extra-high": "xhigh",
    "extra high": "xhigh",
    "extrahigh": "xhigh",
    "x-high": "xhigh",
}


def normalize_reasoning_effort(raw: object, *, default: str = DEFAULT_CODEX_REASONING_EFFORT) -> str:
    """Return a canonical effort level, falling back to *default* when unknown."""
    if raw is None:
        return default
    s = str(raw).strip().lower()
    if not s:
        return default
    s = _ALIASES.get(s, s)
    if s in REASONING_EFFORT_LEVELS:
        return s
    return default


def current_reasoning_effort() -> str:
    return normalize_reasoning_effort(
        os.environ.get("HARNESS_CODEX_REASONING_EFFORT"),
        default=DEFAULT_CODEX_REASONING_EFFORT,
    )


def codex_api_effort(ui_effort: str) -> Optional[str]:
    """Map a UI effort to the Codex Responses API value, or None to omit reasoning."""
    level = normalize_reasoning_effort(ui_effort)
    if level == "none":
        return None
    return level


def reasoning_effort_label(level: str) -> str:
    return REASONING_EFFORT_LABELS.get(
        normalize_reasoning_effort(level),
        REASONING_EFFORT_LABELS[DEFAULT_CODEX_REASONING_EFFORT],
    )
