"""Swarm-job cost / routing / cache-savings helpers.

Owns registry-priced job+task accounting, router pre-flight estimates, and
routing/cache savings aggregation used by ``/api/usage`` and swarm live cards.
``harness.api.cost`` re-exports the historical ``_`` names.
"""

from __future__ import annotations

from .cost_accounting import CACHE_READ_MULTIPLIER
from .cost import _diag, _server_attr

def _swarm_registry() -> list:
    """Load the model registry for per-job actual-cost pricing. Best-effort."""
    try:
        from puppetmaster.model_registry import default_registry_path, load_registry
        return load_registry(default_registry_path())
    except Exception:
        return []


def _routing_estimate_by_task(artifacts) -> dict:
    """FINAL router pre-flight estimate per task_id (escalation > fallback > router).

    Untasked ROUTING rows are omitted -- they have no worker row to attach to.
    """
    try:
        from puppetmaster.models import ArtifactType
    except Exception:
        return {}
    rank = {
        "router-escalation": 3,
        "router-fallback": 2,
        "router": 1,
    }
    best: dict = {}  # task_id -> (rank, cost)
    for artifact in artifacts or []:
        if getattr(artifact, "type", None) != ArtifactType.ROUTING:
            continue
        created_by = getattr(artifact, "created_by", "") or ""
        r = rank.get(created_by, 0)
        if r == 0:
            continue
        payload = getattr(artifact, "payload", None) or {}
        cost = float(
            payload.get("estimated_cost_usd") or payload.get("nominal_cost_usd") or 0.0
        )
        task_id = getattr(artifact, "task_id", None)
        if not task_id:
            continue
        prev = best.get(task_id)
        if prev is None or r > prev[0]:
            best[task_id] = (r, cost)
    return {tid: cost for tid, (_r, cost) in best.items()}


def _routing_estimate_cost(artifacts) -> float:
    """Sum FINAL router pre-flight estimates; interim fallback before usage lands.

    Prefer escalation / fallback estimates over the initial ``router`` pick so a
    plan-billed first choice ($0) does not wipe the real fallback estimate.
    """
    try:
        from puppetmaster.models import ArtifactType
    except Exception:
        return 0.0
    rank = {
        "router-escalation": 3,
        "router-fallback": 2,
        "router": 1,
    }
    best: dict = {}  # task_id -> (rank, cost)
    untasked_total = 0.0
    for artifact in artifacts:
        if artifact.type != ArtifactType.ROUTING:
            continue
        created_by = getattr(artifact, "created_by", "") or ""
        r = rank.get(created_by, 0)
        if r == 0:
            continue
        payload = artifact.payload or {}
        cost = float(
            payload.get("estimated_cost_usd") or payload.get("nominal_cost_usd") or 0.0
        )
        task_id = getattr(artifact, "task_id", None)
        if not task_id:
            untasked_total += cost
            continue
        prev = best.get(task_id)
        if prev is None or r > prev[0]:
            best[task_id] = (r, cost)
    return untasked_total + sum(cost for (_r, cost) in best.values())


def _live_price_unpriced_tasks(job_cost) -> float:
    """Price usage records the registry could not price against the live
    OpenRouter price map (public /models feed, disk-cached). Worker model ids
    like 'z-ai/glm-5.2' are OpenRouter slugs, so this usually resolves exactly;
    unmatched models contribute nothing. Best-effort, never raises."""
    total = 0.0
    for task in getattr(job_cost, "tasks", []):
        total += _live_price_task(task)
    return total


def _job_swarm_accounting(raw_arts, registry: list) -> tuple[int, float]:
    """Return (tokens, cost_usd) for a swarm job.

    Prefers measured/estimated usage priced against the registry
    (:func:`puppetmaster.cost.price_job`), topped up with live OpenRouter rates
    for models the registry does not know. Falls back to the router's
    pre-flight estimate only while nothing could be priced from usage.
    When no ROUTING artifact exists (provider-native workers), usage is read
    from VERIFICATION payloads instead.
    """
    from puppetmaster.usage import aggregate_token_usage
    from puppetmaster.cost import price_job

    arts_for_usage = _arts_for_swarm_usage(raw_arts)

    tokens = 0
    try:
        token_usage_dict = aggregate_token_usage(arts_for_usage)
        tokens = int(token_usage_dict.get("total_tokens", 0) or 0)
    except Exception:
        pass

    est_cost_usd = 0.0
    try:
        job_cost = price_job(arts_for_usage, registry)
        usage_cost = job_cost.total_marginal_cost_usd + _live_price_unpriced_tasks(job_cost)
        # Only trust the usage-priced total when something actually priced.
        # Usage can land with models neither source can price (cost 0.0);
        # treating that as authoritative made finished jobs snap from the
        # routing estimate back to $0. Keep the estimate instead.
        if usage_cost > 0:
            est_cost_usd = usage_cost
        else:
            est_cost_usd = _routing_estimate_cost(raw_arts)
    except Exception:
        est_cost_usd = _routing_estimate_cost(raw_arts)
    return tokens, round(est_cost_usd, 6)


def _arts_for_swarm_usage(raw_arts):
    """Artifacts used for usage pricing: VERIFICATION-only when no ROUTING."""
    arts_for_usage = list(raw_arts or [])
    try:
        from puppetmaster.models import ArtifactType
    except Exception:
        return arts_for_usage
    has_routing = any(
        getattr(a, "type", None) == ArtifactType.ROUTING for a in arts_for_usage
    )
    if not has_routing:
        verification = [
            a for a in arts_for_usage if getattr(a, "type", None) == ArtifactType.VERIFICATION
        ]
        if verification:
            return verification
    return arts_for_usage


def _live_price_task(task) -> float:
    """Price one unpriced TaskCost against the live OpenRouter map. Best-effort."""
    if getattr(task, "priced", False) or (
        not getattr(task, "tokens_in", 0) and not getattr(task, "tokens_out", 0)
    ):
        return 0.0
    try:
        from pmharness.registry import price
        price_in, price_out = price(task.model_id)
    except Exception:
        return 0.0
    if price_in is None or price_out is None:
        return 0.0
    return ((task.tokens_in / 1.0e6) * price_in
            + (task.tokens_out / 1.0e6) * price_out)


def _task_swarm_accounting(raw_arts, registry: list) -> dict:
    """Per-task ``{tokens, est_cost_usd}`` for /api/swarm/live worker rows.

    Prefers measured/estimated usage priced like :func:`_job_swarm_accounting`;
    falls back to the FINAL routing estimate per task while usage is absent or
    unpriceable. Keys are task_ids present on usage or ROUTING artifacts.
    """
    from puppetmaster.usage import select_usage_records
    from puppetmaster.cost import price_job

    by_task: dict = {}
    for task_id, cost in _routing_estimate_by_task(raw_arts).items():
        by_task[task_id] = {"tokens": 0, "est_cost_usd": round(float(cost or 0.0), 6)}

    arts_for_usage = _arts_for_swarm_usage(raw_arts)
    try:
        usage = select_usage_records(arts_for_usage)
        job_cost = price_job(arts_for_usage, registry)
        priced = {t.task_id: t for t in getattr(job_cost, "tasks", [])}
        for task_id, record in usage.items():
            if str(task_id).startswith("__untasked_"):
                continue
            tokens = int(record.get("tokens_in") or 0) + int(record.get("tokens_out") or 0)
            cost = 0.0
            tc = priced.get(task_id)
            if tc is not None:
                if tc.priced and tc.marginal_cost_usd > 0:
                    cost = float(tc.marginal_cost_usd)
                else:
                    cost = _live_price_task(tc)
            prev = by_task.get(task_id) or {"tokens": 0, "est_cost_usd": 0.0}
            by_task[task_id] = {
                "tokens": tokens,
                "est_cost_usd": (
                    round(cost, 6) if cost > 0 else float(prev.get("est_cost_usd") or 0.0)
                ),
            }
    except Exception:
        pass
    return by_task


_COST_OPTIMIZING_POLICIES = frozenset({"balanced", "cheap"})


def _routing_saved_usd(raw_arts) -> float:
    """Dollars saved by cost-optimizing router picks vs the snapshotted baseline.

    Only ``balanced`` / ``cheap`` policies count; ``quality`` / ``escalating`` are
    deliberate spend and contribute 0. Best-effort -- never raises.
    """
    try:
        from puppetmaster.models import ArtifactType
    except Exception:
        return 0.0
    seen_tasks: set = set()
    total = 0.0
    try:
        for artifact in raw_arts or []:
            if getattr(artifact, "type", None) != ArtifactType.ROUTING:
                continue
            if getattr(artifact, "created_by", "") != "router":
                continue
            task_id = getattr(artifact, "task_id", None)
            if task_id:
                if task_id in seen_tasks:
                    continue
                seen_tasks.add(task_id)
            payload = getattr(artifact, "payload", None) or {}
            if not isinstance(payload, dict):
                continue
            policy = str(payload.get("policy") or "")
            if policy not in _COST_OPTIMIZING_POLICIES:
                continue
            try:
                baseline = float(payload.get("baseline_cost_usd") or 0.0)
                estimated = float(payload.get("estimated_cost_usd") or 0.0)
            except (TypeError, ValueError):
                continue
            if baseline <= 0:
                continue
            total += max(0.0, baseline - estimated)
    except Exception:
        return 0.0
    return total


def _registry_input_per_mtok(model_id: str, registry: list) -> float:
    """Resolve a model's input $/MTok from the registry; 0 when unknown."""
    if not model_id:
        return 0.0
    for spec in registry or []:
        if getattr(spec, "id", None) == model_id:
            try:
                return float(getattr(spec, "input_per_mtok_usd", 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0
        if getattr(spec, "adapter_model_name", None) == model_id:
            try:
                return float(getattr(spec, "input_per_mtok_usd", 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _tokens_cached_swarm(raw_arts) -> int:
    """Sum ``tokens_cached`` across usage-bearing artifacts (one per task).

    Same task-dedupe as :func:`_cache_saved_usd_swarm` so the token count and
    USD figure stay aligned on /api/swarm/live job rows. Best-effort.
    """
    seen_tasks: set = set()
    total = 0
    try:
        for artifact in raw_arts or []:
            payload = getattr(artifact, "payload", None) or {}
            if not isinstance(payload, dict):
                continue
            if "tokens_in" not in payload and "tokens_out" not in payload:
                continue
            task_id = getattr(artifact, "task_id", None)
            if task_id:
                if task_id in seen_tasks:
                    continue
                seen_tasks.add(task_id)
            try:
                tokens_cached = int(payload.get("tokens_cached") or 0)
            except (TypeError, ValueError):
                continue
            if tokens_cached > 0:
                total += tokens_cached
    except Exception:
        return 0
    return total


def _cache_saved_usd_swarm(raw_arts, registry: list) -> float:
    """Store-job swarm prompt-cache savings for display (not spend).

    Per task with ``tokens_cached > 0`` and a known registry input price:
    tokens_cached/1e6 * input_per_mtok * (1 - CACHE_READ_MULTIPLIER).

    Always credits cache hits even when ``real_cost_usd`` is set — that field
    is the provider spend total (already cache-discounted); suppressing the
    savings *display* left the status bar at $0 for agentic workers.

    Store-job savings belong only here (``cache_saved_usd_swarm``). Harness-
    attributed worker cache hits already land in pilot ``_tokens_cached`` /
    ``cache_savings_usd``; do not fold store-job cache into those meters for
    this figure. Spend math is unchanged. Best-effort.
    """
    seen_tasks: set = set()
    total = 0.0
    try:
        for artifact in raw_arts or []:
            payload = getattr(artifact, "payload", None) or {}
            if not isinstance(payload, dict):
                continue
            if "tokens_in" not in payload and "tokens_out" not in payload:
                continue
            task_id = getattr(artifact, "task_id", None)
            if task_id:
                if task_id in seen_tasks:
                    continue
                seen_tasks.add(task_id)
            try:
                tokens_cached = int(payload.get("tokens_cached") or 0)
            except (TypeError, ValueError):
                continue
            if tokens_cached <= 0:
                continue
            model = (
                payload.get("model")
                or payload.get("model_id")
                or ""
            )
            price_in = _registry_input_per_mtok(str(model), registry)
            if price_in <= 0:
                continue
            total += (tokens_cached / 1.0e6) * price_in * (1.0 - CACHE_READ_MULTIPLIER)
    except Exception:
        return 0.0
    return total


def _sum_job_set_savings(job_ids, arts_getter, registry: list) -> tuple[float, float]:
    """Sum routing + swarm-cache savings over a job id set. Never raises."""
    routing = 0.0
    cache = 0.0
    routing_fn = _server_attr("_routing_saved_usd", _routing_saved_usd)
    cache_fn = _server_attr("_cache_saved_usd_swarm", _cache_saved_usd_swarm)
    for jid in job_ids or []:
        try:
            arts = arts_getter(jid)
        except Exception as e:
            _diag("server.usage_savings_arts", e, msg=f"job={jid}")
            continue
        try:
            routing += routing_fn(arts)
        except Exception as e:
            _diag("server.usage_routing_saved", e, msg=f"job={jid}")
        try:
            cache += cache_fn(arts, registry)
        except Exception as e:
            _diag("server.usage_cache_saved_swarm", e, msg=f"job={jid}")
    return routing, cache
