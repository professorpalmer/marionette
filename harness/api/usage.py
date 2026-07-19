"""Usage / cost HTTP route bodies (peeled from ``harness.server``).

Owns ``GET /api/usage`` (StatusBar boot pill) and ``GET /api/context/usage``.
Auth/token gates stay on ``server.Handler``; this module never imports
``harness.server`` at top level.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union


@dataclass
class UsageServices:
    """Explicit deps for usage HTTP handlers (injected by ``server.py``)."""

    cfg: Any
    boot_repos: Callable[[], set]
    boot_usage_meters: Callable[[], dict]
    usage_cache_get: Callable[[str], Optional[dict]]
    usage_cache_put: Callable[[str, dict], None]
    boot_session_cost: Callable[[float, float], float]
    scoped_jobs_with_stores: Callable[..., tuple]
    job_in_cost_window: Callable[[Any], bool]
    swarm_registry: Callable[[], list]
    job_swarm_accounting: Callable[..., tuple]
    tokens_cached_swarm: Callable[..., int]
    job_savings_fields: Callable[[str], dict]
    active_session_total: Callable[..., Any]
    sum_job_set_savings: Callable[..., tuple]
    sum_job_set_savings_detail: Callable[..., dict]
    cache_savings: Callable[..., float]
    cache_savings_gross: Callable[..., float]
    boot_cost_source: Callable[[], str]
    tool_output_savings_fields: Callable[..., dict]
    persist_boot_usage: Callable[..., None]
    retry_on_locked: Callable[..., Any]
    diag: Callable[..., Any]
    get_pilot: Callable[[], Any]


JsonPayload = Union[dict, list]


def get_context_usage(svc: UsageServices) -> tuple[int, JsonPayload]:
    """GET /api/context/usage."""
    try:
        return 200, svc.get_pilot().get_context_usage()
    except Exception as e:
        return 500, {"error": str(e)}


def get_usage(repo_override: str, svc: UsageServices) -> tuple[int, JsonPayload]:
    """GET /api/usage — process-lifetime StatusBar cost pill."""
    # Resolve real per-Mtok pricing for the active driver via resolve_price
    # (historical seam: live → catalog → defaults). Surface price_source so
    # the UI can mark silent default fallbacks without bypassing that seam.
    try:
        from pmharness.registry import resolve_price, price_with_source
        from .cost_accounting import _normalize_price_source

        price_in, price_out = resolve_price(svc.cfg.driver)
        raw_in, raw_out, _price_src = price_with_source(svc.cfg.driver)
        price_source = _normalize_price_source(
            None if raw_in is None or raw_out is None else _price_src
        )
    except Exception:
        price_in, price_out, price_source = 0.5, 2.0, "default"
    # Boot pill: process-lifetime meters (carry + ALL live runners), not
    # just the active view -- so attaching another session never drops
    # spend that already happened on a background runner.
    boot_meters = svc.boot_usage_meters()
    boot_repos_set = svc.boot_repos()
    # Cache key includes cheap meter fingerprints so a turn that burns
    # tokens invalidates the burst cache without waiting for TTL.
    usage_cache_key = "%s|%s|%s|%s|%s|%s" % (
        (repo_override or "").strip() or (svc.cfg.repo or ""),
        ",".join(sorted(boot_repos_set)) if boot_repos_set else "",
        int(boot_meters.get("_tokens_used", 0) or 0),
        int(boot_meters.get("_tokens_cached", 0) or 0),
        round(float(boot_meters.get("_worker_cost_usd", 0) or 0.0), 6),
        round(float(boot_meters.get("_provider_cost_usd", 0) or 0.0), 6),
    )
    cached_usage = svc.usage_cache_get(usage_cache_key)
    if cached_usage is not None:
        return 200, cached_usage
    tokens_used = int(boot_meters.get("_tokens_used", 0) or 0)
    t_cached = int(boot_meters.get("_tokens_cached", 0) or 0)
    w_in = int(boot_meters.get("_worker_tokens_in", 0) or 0)
    w_out = int(boot_meters.get("_worker_tokens_out", 0) or 0)
    # Price each live runner (and carry) via _session_cost_split so
    # worker dollars stay at each worker's own model rate.
    est_session_cost = svc.boot_session_cost(price_in, price_out)
    jobs_list = []
    session_total = None
    routing_saved_usd = 0.0
    delegation_saved_usd = 0.0
    cache_saved_usd_swarm = 0.0
    routing_savings_basis = "unknown"
    delegation_savings_basis = "unknown"
    routing_tokens_compared = 0
    delegation_tokens_compared = 0
    swarm_cached = 0
    try:
        # Same merged, workspace-scoped job set the tracker uses
        # (/api/swarm/live): harness store + per-project CLI store, so
        # MCP/CLI-dispatched swarm spend reaches the status bar.
        from ..cli_job_merge import (
            bulk_load_store_artifacts,
            partition_jobs_by_store,
        )

        # Boot-pill swarm dollars: merge epoch-windowed jobs across every
        # workspace opened this process (not only active _cfg.repo).
        # session_total below still uses the active-workspace set.
        boot_repos = set(boot_repos_set)
        active_repo = (repo_override or "").strip() or (svc.cfg.repo or "")
        if active_repo:
            boot_repos.add(
                os.path.abspath(active_repo) if os.path.isdir(active_repo) else active_repo
            )
        if not boot_repos and active_repo:
            boot_repos.add(active_repo)

        all_jobs_by_id: dict = {}
        store = None
        cli_store = None
        for repo_path in sorted(boot_repos) or [active_repo or None]:
            scoped, st, cli_st = svc.scoped_jobs_with_stores(
                repo_root=repo_path or None
            )
            if store is None:
                store = st
            if cli_store is None and cli_st is not None:
                cli_store = cli_st
            for j in scoped:
                jid = j.get("id")
                if jid and jid not in all_jobs_by_id:
                    all_jobs_by_id[jid] = j
        all_jobs = list(all_jobs_by_id.values())

        # Active-workspace set for session_total (unchanged semantics).
        active_jobs, active_store, active_cli = svc.scoped_jobs_with_stores(
            repo_root=repo_override or None
        )
        if store is None:
            store = active_store
        if cli_store is None:
            cli_store = active_cli

        # Boot pill: only jobs created during THIS app run (epoch window).
        jobs = [j for j in all_jobs if svc.job_in_cost_window(j.get("created_at"))]
        registry = svc.swarm_registry()
        jids = [j.get("id") for j in jobs if j.get("id")]
        # Artifacts may live in harness or CLI stores; load from the
        # union of boot-scoped + active-scoped job ids.
        arts_source_jobs = list(all_jobs_by_id.values())
        for j in active_jobs:
            jid = j.get("id")
            if jid and jid not in all_jobs_by_id:
                arts_source_jobs.append(j)
        harness_jids, cli_jids = partition_jobs_by_store(arts_source_jobs)

        arts_by_job: dict = {}
        try:
            harness_arts = bulk_load_store_artifacts(store, harness_jids)
            cli_arts = bulk_load_store_artifacts(cli_store, cli_jids)
            arts_by_job = {**harness_arts, **cli_arts}
        except Exception:
            arts_by_job = None  # fall back to per-job reads

        # Owning-store lookup: each job is priced from its own store.
        job_by_id = {j.get("id"): j for j in arts_source_jobs if j.get("id")}

        def _owning_store(jid):
            job = job_by_id.get(jid) or {}
            if job.get("source") == "cli" and cli_store is not None:
                return cli_store
            return store

        def _job_arts(jid):
            if arts_by_job is not None:
                return arts_by_job.get(jid, [])
            owning = _owning_store(jid)
            if owning is None:
                return []
            try:
                return svc.retry_on_locked(lambda: owning.list_artifacts(jid))
            except Exception:
                return []

        # Lifetime session_total: active-workspace visible set only
        # (filter_store_jobs + CLI merge). Dedupe by job id; harness
        # wins via merge_scoped_cli_jobs order.
        session_jids: list = []
        seen_session: set = set()
        for j in active_jobs:
            jid = j.get("id")
            if not jid or jid in seen_session:
                continue
            seen_session.add(jid)
            session_jids.append(jid)

        for jid in jids:
            try:
                raw_arts = _job_arts(jid)
                # Spend always goes through the injected 2-tuple helper so
                # hermetic tests can monkeypatch harness.server._job_swarm_accounting.
                tokens, est_cost_usd = svc.job_swarm_accounting(raw_arts, registry)
                provenance = "default"
                job_estimated = True
                try:
                    from .cost import _server_attr
                    from .swarm_cost import _job_swarm_accounting_detail

                    detail_fn = _server_attr(
                        "_job_swarm_accounting_detail", _job_swarm_accounting_detail
                    )
                    detail = detail_fn(raw_arts, registry)
                    # Only trust provenance when it agrees with the spend helper
                    # (detects monkeypatched stubs that return fixed dollars).
                    if (
                        int(detail.get("tokens") or 0) == int(tokens or 0)
                        and abs(
                            float(detail.get("est_cost_usd") or 0.0)
                            - float(est_cost_usd or 0.0)
                        )
                        < 1e-9
                    ):
                        provenance = detail.get("cost_provenance") or "default"
                        job_estimated = bool(detail.get("estimated", True))
                except Exception:
                    pass
                try:
                    swarm_cached += int(svc.tokens_cached_swarm(raw_arts) or 0)
                except Exception:
                    pass
                jobs_list.append({
                    "job_id": jid,
                    "tokens": tokens,
                    "est_cost_usd": est_cost_usd,
                    "cost_provenance": provenance,
                    "estimated": bool(job_estimated),
                    **svc.job_savings_fields(jid),
                })
            except Exception as e:
                svc.diag("server.usage_job_cost", e, msg=f"job={jid}")
        session_total = svc.active_session_total(session_jids, _job_arts, registry)
        # Boot-pill savings: epoch job set across boot repos (jids), not
        # active-workspace-only session_jids -- so dir/session swaps keep
        # routing/cache saved meters process-lifetime.
        try:
            savings_detail = svc.sum_job_set_savings_detail(
                jids,
                _job_arts,
                registry,
                active_price_in=price_in,
                active_price_out=price_out,
            )
            routing_saved_usd = float(savings_detail.get("routing_saved_usd") or 0.0)
            delegation_saved_usd = float(
                savings_detail.get("delegation_saved_usd") or 0.0
            )
            cache_saved_usd_swarm = float(
                savings_detail.get("cache_saved_usd_swarm") or 0.0
            )
            routing_savings_basis = str(
                savings_detail.get("routing_savings_basis") or "unknown"
            )
            delegation_savings_basis = str(
                savings_detail.get("delegation_savings_basis") or "unknown"
            )
            routing_tokens_compared = int(
                savings_detail.get("routing_tokens_compared") or 0
            )
            delegation_tokens_compared = int(
                savings_detail.get("delegation_tokens_compared") or 0
            )
        except Exception:
            # Compatible with monkeypatched 3-arg sum_job_set_savings stubs.
            try:
                routing_saved_usd, cache_saved_usd_swarm = svc.sum_job_set_savings(
                    jids,
                    _job_arts,
                    registry,
                    active_price_in=price_in,
                    active_price_out=price_out,
                )
            except TypeError:
                routing_saved_usd, cache_saved_usd_swarm = svc.sum_job_set_savings(
                    jids, _job_arts, registry
                )
    except Exception as e:
        svc.diag("server.usage_jobs_aggregate", e)
    # Swarm store jobs: dollars come ONLY from here (usage artifacts x
    # registry). Token display = pilot-only meters (boot total minus
    # worker in/out already folded into pilot) + authoritative store
    # job token sums -- mirrors SwarmPane job.tokens without undercount.
    swarm_cost = sum(float(j.get("est_cost_usd") or 0.0) for j in jobs_list)
    est_session_cost += swarm_cost
    pilot_only_tokens = max(0, tokens_used - w_in - w_out)
    job_tokens_sum = sum(int(j.get("tokens") or 0) for j in jobs_list)
    tokens_used = pilot_only_tokens + job_tokens_sum
    # Cache tokens: subtract overlapping swarm attribution from pilot
    # meters, then add authoritative store-job cache (avoids double
    # count when harness workers were folded into _tokens_cached).
    pilot_only_cached = max(0, t_cached - min(t_cached, swarm_cached))
    tokens_cached = pilot_only_cached + swarm_cached
    usage_cost_source = svc.boot_cost_source()
    # Swarm store dollars are still catalog/usage-priced; do not claim
    # the whole pill is provider-billed when those are folded in.
    if swarm_cost > 0 and usage_cost_source == "provider":
        usage_cost_source = "mixed"
    # Cap catalog cache-savings only by known provider-billed spend.
    # Estimated / plan_estimated paths keep uncapped catalog savings
    # (still labeled estimated) — never clamp against estimated totals.
    provider_cost = float(boot_meters.get("_provider_cost_usd", 0) or 0.0)
    cache_provider_cap = (
        provider_cost if usage_cost_source in ("provider", "mixed") else None
    )
    try:
        from .cost_accounting import (
            _cache_savings_gross,
            _cache_savings_with_basis,
            _spend_is_estimated,
        )

        cache_savings_usd, cache_savings_basis = _cache_savings_with_basis(
            pilot_only_cached, price_in, provider_cost_usd=cache_provider_cap
        )
        cache_savings_gross_usd = _cache_savings_gross(pilot_only_cached, price_in)
        spend_estimated = _spend_is_estimated(usage_cost_source, price_source)
    except Exception:
        cache_savings_usd = svc.cache_savings(pilot_only_cached, price_in)
        cache_savings_basis = "catalog"
        try:
            cache_savings_gross_usd = float(
                svc.cache_savings_gross(pilot_only_cached, price_in) or 0.0
            )
        except Exception:
            cache_savings_gross_usd = float(cache_savings_usd or 0.0)
        spend_estimated = usage_cost_source != "provider"
    response_data = {
        "session": {
            "tokens_used": tokens_used,
            "est_cost_usd": round(est_session_cost, 6),
            # provider = OpenRouter usage.cost (etc.); estimated =
            # token*catalog fallback; mixed = both slices present.
            "cost_source": usage_cost_source,
            # live | static | default — how display rates were resolved.
            "price_source": price_source,
            # True when spend is not a full provider receipt (or rates defaulted).
            "estimated": bool(spend_estimated),
            "driver": svc.cfg.driver,
            "price_in": price_in,
            "price_out": price_out,
            # Prompt-cache hits (billed at the cache-read discount) and
            # the USD that discount saved vs full input price (pilot-
            # only; store-job cache USD is cache_saved_usd_swarm).
            "tokens_cached": tokens_cached,
            "cache_savings_usd": round(cache_savings_usd, 6),
            "cache_savings_gross_usd": round(cache_savings_gross_usd, 6),
            # catalog = uncapped estimate; capped = limited to provider
            # spend; unknown = provider path present but net spend ≤ 0.
            "cache_savings_basis": cache_savings_basis,
            # Routing + delegation + swarm-cache savings over the boot-repo
            # epoch job set (additive to the pilot cache/compaction figures).
            "routing_saved_usd": round(routing_saved_usd, 6),
            "routing_savings_basis": routing_savings_basis,
            "routing_tokens_compared": int(routing_tokens_compared),
            "delegation_saved_usd": round(delegation_saved_usd, 6),
            "delegation_savings_basis": delegation_savings_basis,
            "delegation_tokens_compared": int(delegation_tokens_compared),
            "cache_saved_usd_swarm": round(cache_saved_usd_swarm, 6),
            **svc.tool_output_savings_fields(price_in, process_wide=True),
        },
        # Lifetime running total for the active chat session
        # (persisted meters + all-time session-stamped / workspace-
        # visible swarm jobs); unlike "session" above, it survives
        # restarts and updates.
        "session_total": session_total,
        "jobs": jobs_list,
    }
    try:
        svc.persist_boot_usage(fold_live=False)
    except Exception:
        pass
    try:
        svc.usage_cache_put(usage_cache_key, response_data)
    except Exception:
        pass
    return 200, response_data
