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
_HOT_NOW_TOKENS = 270_000
_HOT_SOON_TOKENS = 150_000


def _env_enabled(name: str, default_on: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default_on
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    return default_on


def _env_token_threshold(name: str, default: int) -> int:
    """Parse an absolute token threshold. Invalid falls back; zero/negative disables."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value <= 0:
        return 0
    return value


def _effective_thresholds(budget: int) -> tuple[float, float, int, int]:
    """Return (now_ratio, soon_ratio, binding_now_tokens, binding_soon_tokens).

    binding_* is the absolute token count when that rule is the binding constraint,
    else 0.
    """
    now_tokens = _env_token_threshold("HARNESS_ADVISOR_NOW_TOKENS", _HOT_NOW_TOKENS)
    soon_tokens = _env_token_threshold("HARNESS_ADVISOR_SOON_TOKENS", _HOT_SOON_TOKENS)

    now_ratio = _HOT_NOW_RATIO
    soon_ratio = _HOT_SOON_RATIO
    binding_now = 0
    binding_soon = 0

    if now_tokens > 0:
        absolute_now = float(now_tokens) / float(budget)
        if absolute_now < now_ratio:
            now_ratio = absolute_now
            binding_now = now_tokens

    if soon_tokens > 0:
        absolute_soon = float(soon_tokens) / float(budget)
        if absolute_soon < soon_ratio:
            soon_ratio = absolute_soon
            binding_soon = soon_tokens

    return now_ratio, soon_ratio, binding_now, binding_soon


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
        "needs_intervention": False,
        "warning_reason": "",
    }


def _intervention_fields(level: str, reasons: list[str], l3_reclaimed: int) -> dict[str, Any]:
    """Durable UI badge fields when pressure or reclaim needs attention.

    ``soon`` / ``now`` always flag intervention. High L3 reclaim with an
    attention level means compaction already ran under pressure -- keep the
    warning so the UI stays honest after the event.
    """
    needs = level in ("soon", "now")
    warning = ""
    if needs:
        if reasons:
            warning = reasons[0]
        elif l3_reclaimed > 0:
            warning = "history compaction ran under context pressure"
        else:
            warning = "context pressure needs attention"
    return {
        "needs_intervention": needs,
        "warning_reason": warning,
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

    now_threshold, soon_threshold, binding_now, binding_soon = _effective_thresholds(budget)

    if hot_ratio >= now_threshold:
        level = "now"
        if binding_now:
            reasons.append(f"hot context above {binding_now} tokens on a large window")
        else:
            pct = int(round(hot_ratio * 100))
            reasons.append(f"hot context at {pct} percent of budget")
    elif hot_ratio >= soon_threshold:
        level = "soon"
        if binding_soon:
            reasons.append(f"hot context above {binding_soon} tokens on a large window")
        else:
            pct = int(round(hot_ratio * 100))
            reasons.append(f"hot context at {pct} percent of budget")
    elif hot_ratio >= _HOT_L1_COMBO_RATIO and l1_bytes > _L1_PRESSURE_BYTES:
        level = "soon"
        pct = int(round(hot_ratio * 100))
        reasons.append(
            f"session state exceeds 5 MB with warm context at {pct} percent of budget"
        )

    advice = {
        "level": level,
        "hot_ratio": hot_ratio,
        "l1_bytes": l1_bytes,
        "l3_reclaimed_bytes": l3_reclaimed,
        "reasons": reasons,
    }
    advice.update(_intervention_fields(level, reasons, l3_reclaimed))
    return advice


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
