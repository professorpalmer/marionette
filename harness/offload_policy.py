"""Shared savings gate for tool-output offload (OMP Round 10).

One pure function decides every spill/compaction of a tool result. Results below
the token floor are never touched; replacements must save at least the configured
margin. Never raises.
"""
from __future__ import annotations

import os

from harness.tool_output_savings import estimate_tokens, tokens_avoided

MIN_TOOL_RESULT_TOKENS = 3000
SAVINGS_MARGIN = 0.9


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _min_tokens() -> int:
    value = _env_int("HARNESS_OFFLOAD_MIN_TOKENS", MIN_TOOL_RESULT_TOKENS)
    return max(0, value)


def _margin() -> float:
    value = _env_float("HARNESS_OFFLOAD_MARGIN", SAVINGS_MARGIN)
    if value <= 0:
        return SAVINGS_MARGIN
    return value


def _safe_chars(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except Exception:
        return 0


def gate_decision(original_chars: int, replacement_chars: int) -> dict:
    """Return offload decision with reason and estimated tokens saved."""
    original = _safe_chars(original_chars)
    replacement = _safe_chars(replacement_chars)
    min_tokens = _min_tokens()
    margin = _margin()

    original_tokens = estimate_tokens(original)
    if original_tokens < min_tokens:
        return {
            "offload": False,
            "reason": f"below floor ({min_tokens} tokens)",
            "estimated_tokens_saved": 0,
        }

    if replacement > int(original * margin):
        return {
            "offload": False,
            "reason": f"replacement above margin ({margin})",
            "estimated_tokens_saved": 0,
        }

    saved = tokens_avoided(original, replacement)
    return {
        "offload": True,
        "reason": "passed gate",
        "estimated_tokens_saved": saved,
    }


def should_offload(original_chars: int, replacement_chars: int) -> bool:
    """True when offload would provably save tokens under the configured policy."""
    return bool(gate_decision(original_chars, replacement_chars)["offload"])
