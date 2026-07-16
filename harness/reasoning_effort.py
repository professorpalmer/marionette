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


# Anthropic / Bedrock Claude extended-thinking budgets (tokens). ``none`` omits
# the thinking block entirely. Tuned for interactive pilots, not max-out research.
_ANTHROPIC_THINKING_BUDGETS = {
    "none": None,
    "low": 4_096,
    "medium": 10_000,
    "high": 16_000,
    "xhigh": 24_000,
    "max": 32_000,
}


def model_supports_anthropic_thinking(model_id: str) -> bool:
    """True for Claude opus/sonnet families that accept Messages ``thinking``."""
    mid = (model_id or "").strip().lower()
    if not mid:
        return False
    if "haiku" in mid:
        return False
    # Bare ids and Bedrock profiles (e.g. us.anthropic.claude-sonnet-4-…).
    return ("opus" in mid) or ("sonnet" in mid)


def anthropic_thinking_budget(ui_effort: object = None) -> Optional[int]:
    """Map effort → Anthropic ``budget_tokens``, or None to omit thinking."""
    level = normalize_reasoning_effort(
        ui_effort if ui_effort is not None else current_reasoning_effort()
    )
    return _ANTHROPIC_THINKING_BUDGETS.get(level)


def apply_anthropic_thinking(body: dict, model_id: str, *, max_tokens: int) -> dict:
    """Mutate a Messages-style body to enable extended thinking when appropriate.

    Ensures ``max_tokens`` is strictly greater than ``budget_tokens`` (Anthropic
    requirement). Returns the same dict for chaining.
    """
    if not isinstance(body, dict):
        return body
    if not model_supports_anthropic_thinking(model_id):
        return body
    budget = anthropic_thinking_budget()
    if budget is None or budget <= 0:
        return body
    # Thinking budget must be below output ceiling.
    ceiling = int(max_tokens or body.get("max_tokens") or 0)
    if ceiling <= budget:
        ceiling = budget + 1024
        body["max_tokens"] = ceiling
    body["thinking"] = {"type": "enabled", "budget_tokens": int(budget)}
    # Extended thinking rejects non-default temperature.
    body.pop("temperature", None)
    return body
