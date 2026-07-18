"""Calm full-auto operator receipt copy (CLI / tests).

Mirrors the webapp StatusBar/CostBreakdown quiet language: budget meters and
halt/block wording that never imply a command ran or a compaction occurred
unless the halt reason itself says the objective was met.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional


def _fmt_tokens(num: Any) -> str:
    try:
        n = float(num)
    except (TypeError, ValueError):
        return "0"
    if n < 0 or n != n:  # NaN
        return "0"
    if n >= 1_000_000:
        s = f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{s}M"
    if n >= 1000:
        s = f"{n / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{s}k"
    return str(int(round(n)))


def format_budget_meters(snapshot: Optional[Mapping[str, Any]]) -> str:
    """Compact AutoBudget meters: ``2/20 swarms · 4.1k/50k tok · 45s``."""
    if not isinstance(snapshot, Mapping):
        return ""
    parts = []
    swarms_used = snapshot.get("swarms_used")
    max_swarms = snapshot.get("max_swarms")
    if isinstance(swarms_used, (int, float)) and isinstance(max_swarms, (int, float)):
        parts.append(f"{int(swarms_used)}/{int(max_swarms)} swarms")
    tokens_used = snapshot.get("tokens_used")
    if isinstance(tokens_used, (int, float)):
        max_tokens = snapshot.get("max_tokens")
        used = _fmt_tokens(tokens_used)
        if isinstance(max_tokens, (int, float)):
            parts.append(f"{used}/{_fmt_tokens(max_tokens)} tok")
        else:
            parts.append(f"{used} tok")
    elapsed = snapshot.get("elapsed_s")
    if isinstance(elapsed, (int, float)):
        parts.append(f"{max(0, int(elapsed))}s")
    return " · ".join(parts)


def format_auto_status_receipt(
    cycle: Any,
    snapshot: Optional[Mapping[str, Any]] = None,
) -> str:
    try:
        n = int(cycle)
    except (TypeError, ValueError):
        n = 0
    meters = format_budget_meters(snapshot)
    label = f"Full-auto · cycle {max(0, n)}"
    return f"{label} · {meters}" if meters else label


def format_auto_halt_receipt(
    reason: str,
    snapshot: Optional[Mapping[str, Any]] = None,
) -> str:
    raw = (reason or "").strip() or "Budget or policy ended the run"
    low = raw.lower()
    if "objective met" in low:
        label = "Full-auto finished"
    elif "cancel" in low:
        label = "Full-auto cancelled"
    elif any(k in low for k in (
        "ceiling", "stall", "killswitch", "token", "swarm", "idle", "seconds",
    )):
        label = "Full-auto halted"
    else:
        label = "Full-auto stopped"
    meters = format_budget_meters(snapshot)
    body = f"{label}: {raw}"
    return f"{body} · {meters}" if meters else body


def format_command_blocked_receipt(
    reason: str = "",
    category: str = "",
) -> str:
    detail = (reason or "").strip() or (category or "").strip() or (
        "Full-auto safety policy blocked this command"
    )
    return f"Command not run: {detail}"
