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
    """Behavior-changing early-compaction toggle; default ON.

    When on, ``CompactionContextMixin._maybe_compact_history`` proactively
    compacts once advice reaches level ``now`` (before the hard 75% trigger),
    so users never have to compact by hand -- matching Hermes, which
    auto-compresses at threshold with only a quiet receipt. Set
    ``HARNESS_ADVISOR_COMPACTION=0`` to fall back to the hard trigger only.
    """
    return _env_enabled("HARNESS_ADVISOR_COMPACTION", True)


def _none_advice() -> dict[str, Any]:
    return {
        "level": "none",
        "hot_ratio": 0.0,
        "l1_bytes": 0,
        "l3_reclaimed_bytes": 0,
        "reasons": [],
        "needs_intervention": False,
        "warning_reason": "",
        "budget_kind": "",
        "budget_tokens": 0,
    }


# Only a successful Compact Now (history shrank) may latch calm.
_ACK_CALM_REASONS = frozenset({"ok", "manual"})


def history_fingerprint(
    history: Any, *, token_estimate: int | None = None
) -> int:
    """Fingerprint history length + content size (+ optional token estimate).

    Length alone is dishonest when a dense keepRecent tail stays at the same
    message count after a failed Compact Now (pressure still soon/now).
    Content chars are always mixed in so in-place tool-result growth re-arms
    Needs attention even when a stale provider token count is unchanged.
    """
    try:
        length = len(history or [])
    except Exception:
        length = 0
    try:
        content_weight = sum(
            len(str(m.get("content") or ""))
            for m in (history or [])
            if isinstance(m, dict)
        )
    except Exception:
        content_weight = 0
    token_weight = 0
    if token_estimate is not None:
        try:
            token_weight = max(0, int(token_estimate))
        except Exception:
            token_weight = 0
    return (length * 1_000_003) ^ (content_weight * 1009) ^ token_weight


def _pilot_token_estimate(pilot: Any) -> int | None:
    try:
        if hasattr(pilot, "_estimate_context_tokens"):
            return int(pilot._estimate_context_tokens())
    except Exception:
        return None
    return None


def ack_manual_compaction(pilot: Any, reason: str = "manual") -> None:
    """Latch calm only after Compact Now actually succeeded.

    Failed / no-op attempts (below_min_compactable, no_compactable_history,
    summary_rejected / aborted) must NOT clear Needs attention while layer
    pressure remains soon/now.
    """
    try:
        reason_s = str(reason or "manual").strip() or "manual"
        if reason_s not in _ACK_CALM_REASONS:
            return
        history = getattr(pilot, "_history", None) or []
        pilot._compaction_advice_ack = {
            "history_len": history_fingerprint(
                history, token_estimate=_pilot_token_estimate(pilot)
            ),
            "reason": reason_s,
        }
    except Exception:
        pass


def apply_manual_compaction_ack(
    advice: dict[str, Any],
    pilot: Any,
) -> dict[str, Any]:
    """Clear intervention only after a successful Compact Now latch."""
    if not isinstance(advice, dict) or not advice:
        return advice
    try:
        ack = getattr(pilot, "_compaction_advice_ack", None)
        if not isinstance(ack, dict):
            return advice
        ack_reason = str(ack.get("reason") or "").strip()
        if ack_reason not in _ACK_CALM_REASONS:
            return advice
        history = getattr(pilot, "_history", None) or []
        fingerprint = history_fingerprint(
            history, token_estimate=_pilot_token_estimate(pilot)
        )
        if int(ack.get("history_len") or -1) != fingerprint:
            return advice
        body = advice.get("compaction_advice")
        if not isinstance(body, dict):
            return advice
        if not body.get("needs_intervention") and body.get("level") not in ("soon", "now"):
            return advice
        cleared = dict(body)
        cleared["level"] = "none"
        cleared["needs_intervention"] = False
        cleared["warning_reason"] = ""
        cleared["reasons"] = []
        cleared["budget_kind"] = ""
        cleared["budget_tokens"] = 0
        cleared["acked_manual_compact"] = True
        out = dict(advice)
        out["compaction_advice"] = cleared
        return out
    except Exception:
        return advice


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
    budget_kind = ""
    budget_tokens = 0

    now_threshold, soon_threshold, binding_now, binding_soon = _effective_thresholds(budget)

    if hot_ratio >= now_threshold:
        level = "now"
        if binding_now:
            budget_kind = "absolute"
            budget_tokens = binding_now
            reasons.append(f"hot context above {binding_now} tokens on a large window")
        else:
            budget_kind = "percent"
            pct = int(round(hot_ratio * 100))
            reasons.append(f"hot context at {pct} percent of budget")
    elif hot_ratio >= soon_threshold:
        level = "soon"
        if binding_soon:
            budget_kind = "absolute"
            budget_tokens = binding_soon
            reasons.append(f"hot context above {binding_soon} tokens on a large window")
        else:
            budget_kind = "percent"
            pct = int(round(hot_ratio * 100))
            reasons.append(f"hot context at {pct} percent of budget")
    elif hot_ratio >= _HOT_L1_COMBO_RATIO and l1_bytes > _L1_PRESSURE_BYTES:
        level = "soon"
        budget_kind = "l1"
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
        "budget_kind": budget_kind,
        "budget_tokens": budget_tokens,
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
