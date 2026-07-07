"""Per-turn output token budget directive (+Nk / +Nk!).

Ports the OMP parser from packages/coding-agent/src/modes/turn-budget.ts.
Never raises.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

_TURN_BUDGET = re.compile(
    r"(?:^|\s)\+(\d+(?:\.\d+)?)([km])?(!)?(?=\s|$)",
    re.IGNORECASE,
)


def _env_enabled(name: str, default_on: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default_on
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    return default_on


def turn_budget_enabled() -> bool:
    """Feature toggle for parsing and advisory notes; default ON."""
    return _env_enabled("HARNESS_TURN_BUDGET", True)


def parse_turn_budget(text: str) -> Optional[dict[str, Any]]:
    """Parse a +Nk/+Nm(+!) directive from user text, or None when absent."""
    if not isinstance(text, str) or not text:
        return None
    try:
        match = _TURN_BUDGET.search(text)
        if not match:
            return None
        value = float(match.group(1))
        if not (value > 0 and value < float("inf")):
            return None
        unit = (match.group(2) or "").lower()
        multiplier = 1_000 if unit == "k" else 1_000_000 if unit == "m" else 1
        total = int(round(value * multiplier))
        if total <= 0:
            return None
        return {"total": total, "hard": match.group(3) == "!"}
    except Exception:
        return None
