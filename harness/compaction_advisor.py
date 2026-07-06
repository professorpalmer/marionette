"""Layer-pressure compaction advisor (Wave 8, v1).

Pure arithmetic assessment from L0-L3 memory layer snapshots. Never raises;
best-effort only. No LLM in the advice path.
"""
from __future__ import annotations

import os
from typing import Any

_HOT_NOW_RATIO = 0.70
_HOT_SOON_RATIO = 0.55
_HOT_L1_COMBO_RATIO = 0.40
_L1_PRESSURE_BYTES = 5 * 1024 * 1024


def _env_enabled(name: str, default_on: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default_on
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    return default_on


def advisor_enabled() -> bool:
    """Measurement/surfacing toggle; default ON."""
    return _env_enabled("HARNESS_COMPACTION_ADVISOR", True)


def advisor_compaction_enabled() -> bool:
    """Behavior-changing early-compaction toggle; default OFF."""
    return _env_enabled("HARNESS_ADVISOR_COMPACTION", False)


def _none_advice() -> dict[str, Any]:
    return {
        "level": "none",
        "hot_ratio": 0.0,
        "l1_bytes": 0,
        "l3_reclaimed_bytes": 0,
        "reasons": [],
    }


def _layer_bytes(snapshot: dict, layer_id: str) -> int:
    try:
        layer = snapshot.get(layer_id)
        if not isinstance(layer, dict):
            return 0
        return max(0, int(layer.get("bytes") or 0))
    except Exception:
        return 0


def _l3_reclaimed_bytes(snapshot: dict) -> int:
    try:
        l3 = snapshot.get("L3")
        if not isinstance(l3, dict):
            return 0
        components = l3.get("components")
        if not isinstance(components, dict):
            return 0
        before = int(components.get("compaction_chars_before") or 0)
        after = int(components.get("compaction_chars_after") or 0)
        return max(0, before - after)
    except Exception:
        return 0


def assess_layer_pressure(snapshot: dict, max_context_tokens: int) -> dict[str, Any]:
    """Return compaction advice from a layer snapshot. Never raises."""
    if not isinstance(snapshot, dict) or not snapshot:
        return _none_advice()
    try:
        budget = int(max_context_tokens)
    except Exception:
        return _none_advice()
    if budget <= 0:
        return _none_advice()

    l0_bytes = _layer_bytes(snapshot, "L0")
    l1_bytes = _layer_bytes(snapshot, "L1")
    l3_reclaimed = _l3_reclaimed_bytes(snapshot)

    budget_chars = budget * 4
    hot_ratio = float(l0_bytes) / float(budget_chars)
    hot_ratio = max(0.0, min(2.0, hot_ratio))

    reasons: list[str] = []
    level = "none"

    if hot_ratio >= _HOT_NOW_RATIO:
        level = "now"
        pct = int(round(hot_ratio * 100))
        reasons.append(f"hot context at {pct} percent of budget")
    elif hot_ratio >= _HOT_SOON_RATIO:
        level = "soon"
        pct = int(round(hot_ratio * 100))
        reasons.append(f"hot context at {pct} percent of budget")
    elif hot_ratio >= _HOT_L1_COMBO_RATIO and l1_bytes > _L1_PRESSURE_BYTES:
        level = "soon"
        pct = int(round(hot_ratio * 100))
        reasons.append(
            f"session state exceeds 5 MB with warm context at {pct} percent of budget"
        )

    return {
        "level": level,
        "hot_ratio": hot_ratio,
        "l1_bytes": l1_bytes,
        "l3_reclaimed_bytes": l3_reclaimed,
        "reasons": reasons,
    }


def advice_payload(
    state_dir: str,
    session_id: str,
    max_context_tokens: int,
) -> dict[str, Any]:
    """Load latest snapshot and return compaction advice fields. Never raises."""
    if not advisor_enabled():
        return {}
    try:
        from .memory_layers import latest_layer_snapshot

        snapshot = latest_layer_snapshot(state_dir, session_id)
        if not snapshot:
            return {}
        advice = assess_layer_pressure(snapshot, max_context_tokens)
        return {"compaction_advice": advice}
    except Exception:
        return {}
