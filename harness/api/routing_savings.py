"""Actual-usage routing list-price value (frontier-equivalent counterfactual).

Measured path prices identical token categories for baseline vs chosen models.
Legacy snapshotted preflight deltas remain as ``estimated`` when usage/rates
are missing. ``harness.api.swarm_cost`` re-exports the historical names.
"""

from __future__ import annotations

import sys
from typing import Any

from .cost_accounting import CACHE_READ_MULTIPLIER

_COST_OPTIMIZING_POLICIES = frozenset({"balanced", "cheap"})


def _server_attr(name: str, fallback: Any) -> Any:
    """Prefer ``harness.server.<name>`` so test monkeypatches still land."""
    try:
        srv = sys.modules.get("harness.server")
        if srv is not None:
            return getattr(srv, name)
    except Exception:
        pass
    return fallback


def _diag(tag: str, err: BaseException, **kwargs) -> None:
    try:
        from .cost import _diag as cost_diag

        cost_diag(tag, err, **kwargs)
    except Exception:
        pass

ROUTING_SAVINGS_ACTUAL = "actual_usage"
ROUTING_SAVINGS_ESTIMATED = "estimated"
ROUTING_SAVINGS_UNKNOWN = "unknown"

_ROUTING_FINAL_RANK = {
    "router-escalation": 3,
    "router-fallback": 2,
    "router": 1,
}


def _normalize_model_id_for_price(model_id: str) -> str:
    """Normalize picker / stamped ids for pmharness live/catalog lookup."""
    mid = (model_id or "").strip()
    if not mid:
        return ""
    lower = mid.lower()
    for prefix in ("agentic/", "native/"):
        if lower.startswith(prefix):
            mid = mid[len(prefix) :]
            break
    if mid.startswith("cursor/") and not mid.startswith("cursor-cli:"):
        return "cursor-cli:" + mid[len("cursor/") :]
    return mid


def _registry_rates(model_id: str, registry: list) -> tuple:
    """Return ``(price_in, price_out)`` from the Puppetmaster registry, or zeros."""
    if not model_id:
        return 0.0, 0.0
    normalized = _normalize_model_id_for_price(model_id)
    candidates = {model_id, normalized}
    if normalized.startswith("cursor-cli:"):
        candidates.add("cursor/" + normalized[len("cursor-cli:") :])
    for spec in registry or []:
        sid = getattr(spec, "id", None)
        aname = getattr(spec, "adapter_model_name", None)
        if sid not in candidates and aname not in candidates:
            continue
        try:
            pin = float(getattr(spec, "input_per_mtok_usd", 0.0) or 0.0)
            pout = float(getattr(spec, "output_per_mtok_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0, 0.0
        if pin > 0:
            return pin, max(0.0, pout)
    return 0.0, 0.0


def _pmharness_positive_rates(model_id: str) -> tuple:
    """Positive live/catalog rates from pmharness; ``(0, 0)`` when unresolved."""
    if not model_id:
        return 0.0, 0.0
    try:
        from pmharness.registry import price_with_source
    except Exception:
        return 0.0, 0.0
    for candidate in (model_id, _normalize_model_id_for_price(model_id)):
        if not candidate:
            continue
        try:
            pin, pout, _src = price_with_source(candidate)
        except Exception:
            continue
        if pin is None or pout is None:
            continue
        try:
            pin_f = float(pin)
            pout_f = float(pout)
        except (TypeError, ValueError):
            continue
        if pin_f > 0:
            return pin_f, max(0.0, pout_f)
    return 0.0, 0.0


def _resolve_positive_rates(
    model_id: str,
    registry: list,
    *,
    active_fallback: tuple | None = None,
) -> tuple | None:
    """Resolve positive list rates: registry → pmharness → optional active pilot."""
    pin, pout = _registry_rates(model_id, registry or [])
    if pin > 0:
        return pin, pout
    pin, pout = _pmharness_positive_rates(model_id)
    if pin > 0:
        return pin, pout
    if active_fallback is not None:
        try:
            apin = float(active_fallback[0] or 0.0)
            apout = float(active_fallback[1] or 0.0)
        except (TypeError, ValueError, IndexError):
            return None
        if apin > 0:
            return apin, max(0.0, apout)
    return None


def _counterfactual_list_cost(
    tokens_in: int,
    tokens_out: int,
    tokens_cached: int,
    price_in: float,
    price_out: float,
) -> float:
    """List-price cost for identical token categories (routing counterfactual)."""
    tin = max(0, int(tokens_in or 0))
    tout = max(0, int(tokens_out or 0))
    cached = max(0, min(int(tokens_cached or 0), tin))
    uncached = max(tin - cached, 0)
    return (
        (uncached / 1.0e6) * float(price_in or 0.0)
        + (cached / 1.0e6) * float(price_in or 0.0) * CACHE_READ_MULTIPLIER
        + (tout / 1.0e6) * float(price_out or 0.0)
    )


def _final_routing_by_task(raw_arts) -> dict:
    """task_id -> final ROUTING payload (escalation > fallback > router)."""
    try:
        from puppetmaster.models import ArtifactType
    except Exception:
        return {}
    best: dict = {}
    try:
        for artifact in raw_arts or []:
            if getattr(artifact, "type", None) != ArtifactType.ROUTING:
                continue
            created_by = getattr(artifact, "created_by", "") or ""
            rank = _ROUTING_FINAL_RANK.get(created_by, 0)
            if rank == 0:
                continue
            task_id = getattr(artifact, "task_id", None)
            if not task_id:
                continue
            payload = getattr(artifact, "payload", None) or {}
            if not isinstance(payload, dict):
                continue
            prev = best.get(task_id)
            if prev is None or rank > prev[0]:
                best[task_id] = (rank, payload)
    except Exception:
        return {}
    return {tid: payload for tid, (_rank, payload) in best.items()}


def _routing_saved_usd_detail(
    raw_arts,
    registry: list | None = None,
    *,
    active_price_in: float | None = None,
    active_price_out: float | None = None,
) -> dict:
    """Routing list-price value vs frontier-equivalent baseline."""
    empty = {
        "routing_saved_usd": 0.0,
        "routing_savings_basis": ROUTING_SAVINGS_UNKNOWN,
        "routing_tokens_compared": 0,
        "routing_savings_counted": False,
    }
    try:
        from puppetmaster.usage import select_usage_records
    except Exception:
        select_usage_records = None  # type: ignore[assignment]

    # Use raw artifacts directly — when ROUTING rows exist (the only case this
    # helper credits), select_usage_records already sees VERIFICATION usage.
    # Avoid importing swarm_cost here (circular with cost → swarm_cost → here).
    registry = list(registry or [])
    active_fallback = None
    if active_price_in is not None and float(active_price_in or 0.0) > 0:
        active_fallback = (
            float(active_price_in or 0.0),
            float(active_price_out or 0.0),
        )

    usage_by_task: dict = {}
    if select_usage_records is not None:
        try:
            usage_by_task = select_usage_records(list(raw_arts or [])) or {}
        except Exception:
            usage_by_task = {}

    routing_by_task = _final_routing_by_task(raw_arts)
    total = 0.0
    tokens_compared = 0
    saw_actual = False
    saw_estimated = False
    saw_unknown = False
    counted = False

    try:
        for task_id, payload in routing_by_task.items():
            policy = str(payload.get("policy") or "")
            if policy not in _COST_OPTIMIZING_POLICIES:
                continue
            usage = usage_by_task.get(task_id)
            if usage is not None:
                try:
                    tin = int(usage.get("tokens_in") or 0)
                    tout = int(usage.get("tokens_out") or 0)
                    cached = int(usage.get("tokens_cached") or 0)
                except (TypeError, ValueError):
                    tin = tout = cached = 0
                if tin > 0 or tout > 0:
                    # The usage record is the post-run truth. Routing payloads
                    # are only a fallback for older records that omitted model.
                    chosen_id = (
                        str(usage.get("model") or "")
                        or str(payload.get("model_id") or "")
                        or str(payload.get("adapter_model_name") or "")
                    )
                    baseline_id = str(payload.get("baseline_model_id") or "")
                    chosen_rates = _resolve_positive_rates(chosen_id, registry)
                    if chosen_rates is None:
                        # Measured usage present — never fall back to preflight.
                        saw_unknown = True
                        counted = True
                        continue
                    if not baseline_id:
                        # Measured usage without a baseline model cannot be
                        # priced — do not silently keep the preflight delta.
                        saw_unknown = True
                        counted = True
                        continue
                    baseline_rates = _resolve_positive_rates(
                        baseline_id,
                        registry,
                        active_fallback=active_fallback,
                    )
                    if baseline_rates is None:
                        saw_unknown = True
                        counted = True
                        continue
                    baseline_cost = _counterfactual_list_cost(
                        tin,
                        tout,
                        cached,
                        baseline_rates[0],
                        baseline_rates[1],
                    )
                    chosen_cost = _counterfactual_list_cost(
                        tin,
                        tout,
                        cached,
                        chosen_rates[0],
                        chosen_rates[1],
                    )
                    total += max(0.0, baseline_cost - chosen_cost)
                    tokens_compared += max(0, tin) + max(0, tout)
                    saw_actual = True
                    counted = True
                    continue

            try:
                baseline = float(payload.get("baseline_cost_usd") or 0.0)
                estimated = float(
                    payload.get("estimated_cost_usd")
                    or payload.get("nominal_cost_usd")
                    or 0.0
                )
            except (TypeError, ValueError):
                continue
            if baseline <= 0:
                continue
            delta = max(0.0, baseline - estimated)
            if delta <= 0:
                continue
            total += delta
            saw_estimated = True
            counted = True
    except Exception:
        return empty

    if saw_actual:
        basis = ROUTING_SAVINGS_ACTUAL
    elif saw_estimated and not saw_unknown:
        basis = ROUTING_SAVINGS_ESTIMATED
    else:
        basis = ROUTING_SAVINGS_UNKNOWN
    return {
        "routing_saved_usd": total,
        "routing_savings_basis": basis,
        "routing_tokens_compared": int(tokens_compared),
        "routing_savings_counted": bool(counted),
    }


def _routing_saved_usd(
    raw_arts,
    registry: list | None = None,
    *,
    active_price_in: float | None = None,
    active_price_out: float | None = None,
) -> float:
    """Frontier-equivalent list-price value from cost-optimizing routes."""
    try:
        detail = _routing_saved_usd_detail(
            raw_arts,
            registry,
            active_price_in=active_price_in,
            active_price_out=active_price_out,
        )
        return float(detail.get("routing_saved_usd") or 0.0)
    except Exception:
        return 0.0


def _sum_job_set_savings_detail(
    job_ids,
    arts_getter,
    registry: list,
    *,
    active_price_in: float | None = None,
    active_price_out: float | None = None,
    cache_saved_usd_swarm=None,
    routing_saved_usd=None,
) -> dict:
    """Sum routing + swarm-cache savings with aggregate basis / token counts."""
    from .swarm_cost import _cache_saved_usd_swarm as _default_cache_fn

    routing = 0.0
    cache = 0.0
    tokens_compared = 0
    saw_actual = False
    saw_estimated = False
    saw_unknown = False
    routing_detail_fn = _server_attr(
        "_routing_saved_usd_detail", _routing_saved_usd_detail
    )
    routing_fn = _server_attr(
        "_routing_saved_usd", routing_saved_usd or _routing_saved_usd
    )
    cache_fn = _server_attr(
        "_cache_saved_usd_swarm", cache_saved_usd_swarm or _default_cache_fn
    )
    routing_fn_patched = routing_fn is not _routing_saved_usd
    for jid in job_ids or []:
        try:
            arts = arts_getter(jid)
        except Exception as e:
            _diag("server.usage_savings_arts", e, msg=f"job={jid}")
            continue
        try:
            if routing_fn_patched:
                routing += float(routing_fn(arts) or 0.0)
                saw_estimated = True
            else:
                detail = routing_detail_fn(
                    arts,
                    registry,
                    active_price_in=active_price_in,
                    active_price_out=active_price_out,
                )
                routing += float(detail.get("routing_saved_usd") or 0.0)
                tokens_compared += int(detail.get("routing_tokens_compared") or 0)
                if detail.get("routing_savings_counted"):
                    basis = str(detail.get("routing_savings_basis") or "")
                    if basis == ROUTING_SAVINGS_ACTUAL:
                        saw_actual = True
                    elif basis == ROUTING_SAVINGS_ESTIMATED:
                        saw_estimated = True
                    else:
                        saw_unknown = True
        except Exception as e:
            _diag("server.usage_routing_saved", e, msg=f"job={jid}")
        try:
            cache += cache_fn(arts, registry)
        except Exception as e:
            _diag("server.usage_cache_saved_swarm", e, msg=f"job={jid}")
    # actual_usage if any measured value; estimated only when every counted
    # job is estimated; otherwise unknown.
    if saw_actual:
        basis = ROUTING_SAVINGS_ACTUAL
    elif saw_estimated and not saw_unknown:
        basis = ROUTING_SAVINGS_ESTIMATED
    else:
        basis = ROUTING_SAVINGS_UNKNOWN
    return {
        "routing_saved_usd": routing,
        "cache_saved_usd_swarm": cache,
        "routing_savings_basis": basis,
        "routing_tokens_compared": int(tokens_compared),
    }


def _sum_job_set_savings(
    job_ids,
    arts_getter,
    registry: list,
    *,
    active_price_in: float | None = None,
    active_price_out: float | None = None,
) -> tuple[float, float]:
    """Historical ``(routing, cache)`` 2-tuple over a job id set."""
    detail = _sum_job_set_savings_detail(
        job_ids,
        arts_getter,
        registry,
        active_price_in=active_price_in,
        active_price_out=active_price_out,
    )
    return (
        float(detail.get("routing_saved_usd") or 0.0),
        float(detail.get("cache_saved_usd_swarm") or 0.0),
    )
