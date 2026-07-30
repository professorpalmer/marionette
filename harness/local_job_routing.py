from __future__ import annotations

"""Pre-route agentic local implement jobs so the swarm tracker shows model + cost.

In-process ``local-*`` workers never enter Puppetmaster's durable store, so they
historically registered with ``model="agentic"`` and ``est_cost_usd=0`` until
finish. The tracker then looked empty of metrics while the job ran. Dry-run the
same router the agentic engine will use and stamp a UI-shaped ROUTING card at
register time.

ROUTING rows must carry the same ``policy`` / baseline fields that
``DurableState.format_artifacts`` forwards for store jobs so after reload the
SwarmPane basis labels match live jobs (never "Pin attribution unknown" for a
balanced router pick).
"""

import os
from typing import Any, Optional

from harness.model_identity import (
    collapse_engine_prefixes,
    filter_rejected_excluding_selected,
)


def preview_agentic_route(goal: str, *, role: str = "implement") -> dict[str, Any]:
    """Best-effort dry-run of the agentic implement router.

    Returns a dict with ``model_id``, ``est_cost_usd``, ``tokens_in``,
    ``tokens_out``, ``reason``, ``rejected``, ``baseline_cost_usd``,
    ``baseline_model_id``, ``routing_saved_usd``, ``routing_savings_basis``,
    and a UI-ready ``artifact``. Empty dict on any failure (never raises onto
    the dispatch path).
    """
    goal = (goal or "").strip()
    if not goal:
        return {}
    try:
        from puppetmaster.model_registry import default_registry_path, load_registry
        from puppetmaster.platform_lock import active_allowlist
        from puppetmaster.router import NoEligibleModelError, TaskSignals, route_task
    except Exception:
        return {}

    provider = (os.environ.get("HARNESS_IMPLEMENT_PROVIDER", "") or "").strip().lower()
    pinned_model = (os.environ.get("HARNESS_IMPLEMENT_MODEL", "") or "").strip()
    if provider and pinned_model:
        pinned_model = collapse_engine_prefixes(pinned_model) or pinned_model
        return {
            "model_id": pinned_model,
            "est_cost_usd": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
            "reason": f"Pinned via HARNESS_IMPLEMENT_MODEL ({provider})",
            "rejected": [],
            "baseline_cost_usd": 0.0,
            "baseline_model_id": "",
            "routing_saved_usd": 0.0,
            "routing_savings_basis": "estimated",
            "artifact": _routing_artifact(
                pinned_model,
                0.0,
                role=role,
                reason=f"Pinned via HARNESS_IMPLEMENT_MODEL ({provider})",
                rejected=[],
                policy="explicit_pin",
                provider=provider or None,
                adapter="agentic",
            ),
        }

    try:
        specs = load_registry(default_registry_path())
    except Exception:
        return {}
    if not specs:
        return {}

    max_cap: Optional[int] = None
    if os.environ.get("HARNESS_IMPLEMENT_DEEP", "").strip() not in ("1", "true", "yes"):
        try:
            max_cap = int(os.environ.get("HARNESS_IMPLEMENT_MAX_CAPABILITY", "86"))
        except (TypeError, ValueError):
            max_cap = 86

    allow = active_allowlist()
    # Local agentic edits always run the agentic adapter; intersect with the
    # platform lock when one is set.
    if allow is None:
        allowed = frozenset({"agentic"})
    else:
        allowed = frozenset(a for a in allow if a == "agentic")
        if not allowed:
            # Platform lock excludes agentic -- preview cannot help; leave blank.
            return {}

    signals_kwargs: dict[str, Any] = {
        "instruction": goal,
        "role": (role or "implement").strip() or "implement",
        "allowed_adapters": allowed,
    }
    if max_cap is not None:
        try:
            from pmharness.bridge import _router_supports_max_capability
            if _router_supports_max_capability():
                signals_kwargs["explicit_max_capability"] = max_cap
            else:
                signals_kwargs["explicit_min_capability"] = max_cap
        except Exception:
            signals_kwargs["explicit_max_capability"] = max_cap

    try:
        decision = route_task(
            TaskSignals(**signals_kwargs),
            specs,
            policy="balanced",
        )
    except (NoEligibleModelError, ValueError, TypeError):
        return {}
    except Exception:
        return {}

    model_id = ""
    try:
        model_id = str(getattr(decision.model, "id", "") or "")
    except Exception:
        model_id = ""
    model_id = collapse_engine_prefixes(model_id) or model_id
    if not model_id:
        return {}

    est = float(getattr(decision, "estimated_cost_usd", 0.0) or 0.0)
    tokens_in = int(getattr(decision, "estimated_tokens_in", 0) or 0)
    tokens_out = int(getattr(decision, "estimated_tokens_out", 0) or 0)
    reason = str(getattr(decision, "reason", "") or "")
    baseline = float(getattr(decision, "baseline_cost_usd", 0.0) or 0.0)
    baseline_model_id = collapse_engine_prefixes(
        str(getattr(decision, "baseline_model_id", "") or "")
    ) or str(getattr(decision, "baseline_model_id", "") or "")
    policy = str(getattr(decision, "policy", "") or "balanced")
    rejected: list[dict[str, str]] = []
    for spec, why in getattr(decision, "rejected", []) or []:
        try:
            mid = collapse_engine_prefixes(
                str(getattr(spec, "id", "") or spec)
            ) or str(getattr(spec, "id", "") or spec)
            rejected.append({
                "model": mid,
                "reason": str(why or ""),
            })
        except Exception:
            continue
    # Invariant: selected model is never also listed as rejected.
    rejected = filter_rejected_excluding_selected(rejected, model_id)

    routing_saved = max(0.0, baseline - est) if baseline > 0 else 0.0
    return {
        "model_id": model_id,
        "est_cost_usd": round(est, 6),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "reason": reason,
        "rejected": rejected,
        "baseline_cost_usd": round(baseline, 6),
        "baseline_model_id": baseline_model_id,
        "routing_saved_usd": round(routing_saved, 6),
        # Preflight preview — never claim actual_usage.
        "routing_savings_basis": "estimated",
        "artifact": _routing_artifact(
            model_id,
            est,
            role=role,
            reason=reason,
            rejected=rejected,
            policy=policy or "balanced",
            adapter="agentic",
            baseline_cost_usd=baseline,
            baseline_model_id=baseline_model_id,
        ),
    }


def _routing_artifact(
    model_id: str,
    est_cost_usd: float,
    *,
    role: str,
    reason: str,
    rejected: list[dict[str, str]],
    policy: str = "balanced",
    provider: Optional[str] = None,
    adapter: str = "agentic",
    baseline_cost_usd: float = 0.0,
    baseline_model_id: str = "",
) -> dict[str, Any]:
    """UI-shaped ROUTING row (same fields DurableState.format_artifacts emits)."""
    canonical = collapse_engine_prefixes(model_id) or (model_id or "").strip()
    clean_rejected = filter_rejected_excluding_selected(rejected, canonical)
    row: dict[str, Any] = {
        "type": "ROUTING",
        "headline": f"Routed to {canonical}",
        "created_by": "router",
        "model": canonical,
        "est_cost_usd": round(float(est_cost_usd or 0.0), 6),
        "role": (role or "implement").strip() or "implement",
        "rejected": clean_rejected,
        "detail": reason or "",
        # Attested policy so SwarmPane basis labels survive reload.
        "policy": (policy or "").strip() or "balanced",
        "adapter": (adapter or "").strip() or "agentic",
    }
    if provider:
        row["provider"] = provider
    if baseline_cost_usd and float(baseline_cost_usd) > 0:
        row["baseline_cost_usd"] = round(float(baseline_cost_usd), 6)
    if baseline_model_id:
        row["baseline_model_id"] = baseline_model_id
    return row
