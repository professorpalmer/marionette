"""Jobs / swarm-tracker HTTP route bodies (peeled from ``harness.server``).

``post_swarm_cancel``, ``get_jobs``, ``get_artifacts``, and ``get_swarm_live``
take a :class:`JobServices` so this module never imports ``harness.server`` at
top level. ``server.Handler`` keeps thin path delegates that inject live
globals; auth/token gates stay in the Handler.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class JobServices:
    """Explicit deps for jobs/swarm HTTP handlers (injected by ``server.py``)."""

    cfg: Any
    sessions: Any
    get_pilot: Callable[[], Any]
    get_session: Callable[[], Any]
    diag: Callable[..., None]
    scoped_jobs_snapshot: Callable[..., list]
    scoped_jobs_with_stores: Callable[..., tuple]
    retry_on_locked: Callable[..., Any]
    swarm_registry: Callable[[], list]
    job_status_is_terminal: Callable[[str], bool]
    slim_swarm_list_artifacts: Callable[..., list]
    job_swarm_accounting: Callable[..., tuple]
    task_swarm_accounting: Callable[..., dict]
    routing_saved_usd: Callable[..., float]
    cache_saved_usd_swarm: Callable[..., float]
    tokens_cached_swarm: Callable[..., int]
    job_dead_run_failure: Callable[..., Any]
    job_savings_fields: Callable[[str], dict]
    repo_session_stamped_meters: Callable[[str], dict]
    session_cost_split: Callable[..., float]
    cache_savings: Callable[..., float]
    tool_output_savings_fields: Callable[..., dict]
    cost_source_label: Callable[..., str]
    # Optional: rich routing detail (basis + tokens). Older test stubs omit it.
    routing_saved_usd_detail: Callable[..., dict] | None = None
    delegation_saved_usd_detail: Callable[..., dict] | None = None


def post_swarm_cancel(body: dict, svc: JobServices) -> tuple[int, dict]:
    """Cooperative cancel for a swarm job. Best-effort and never raises.

    Local (provider-worker) jobs are cancelled via the per-job Event on the
    conversation; durable store jobs are marked cancelled in the store where
    possible. Shape: ``{ok, job_id}`` or ``{ok:false, error}``.
    """
    job_id = (body.get("job_id") or "").strip()
    if not job_id:
        return 400, {"ok": False, "error": "missing job_id"}

    # 0) Trip the in-process kill switch FIRST: inline agentic workers
    # check it mid-stream (per chunk) and per turn, so the swarm stops in
    # seconds instead of "cancelling..." until the workers run out of
    # turns naturally.
    try:
        from puppetmaster.cancellation import request_cancel
        request_cancel(job_id)
    except Exception as e:
        svc.diag("server.swarm_cancel_flag", e)

    # 1) Local provider-worker job on the live conversation.
    try:
        pilot = svc.get_pilot()
        if hasattr(pilot, "cancel_local_job") and pilot.cancel_local_job(job_id):
            return 200, {"ok": True, "job_id": job_id}
    except Exception as e:
        svc.diag("server.swarm_cancel_local", e)

    # 2) Durable Puppetmaster store job -- best-effort mark cancelled.
    # Canonical dual-store seam (harness then CLI): shared with
    # BusyControlMixin.interrupt so membership/cancel cannot diverge.
    try:
        from ..job_cancel import cancel_job_dual_store

        harness_store = None
        harness_list_jobs = None
        try:
            state_obj = svc.get_session().state()
            harness_store = getattr(state_obj, "store", None)
            harness_list_jobs = getattr(state_obj, "list_jobs", None)
        except Exception as e:
            svc.diag("server.swarm_cancel_harness_store", e)

        result = cancel_job_dual_store(
            job_id,
            harness_store=harness_store,
            harness_list_jobs=harness_list_jobs,
            repo_root=svc.cfg.repo or "",
        )
        if result is not None:
            return 200, result
    except Exception as e:
        svc.diag("server.swarm_cancel_durable", e)
    return 404, {"ok": False, "error": "unknown job_id", "job_id": job_id}


def get_jobs(repo_override: str | None, svc: JobServices) -> tuple[int, list]:
    """GET /api/jobs — workspace-scoped job list (harness + CLI merge)."""
    return 200, svc.scoped_jobs_snapshot(repo_root=repo_override or None)


def get_artifacts(job_id: str, svc: JobServices) -> tuple[int, list]:
    """GET /api/artifacts — dual-store resolve (harness, then CLI durable)."""
    artifacts: list = []
    state_obj = None
    try:
        state_obj = svc.get_session().state()
        artifacts = svc.retry_on_locked(lambda: state_obj.job_artifacts(job_id))
    except Exception:
        artifacts = []
    if not artifacts:
        try:
            from ..cli_job_merge import open_cli_durable_state
            cli_state = open_cli_durable_state(svc.cfg.repo or "")
            if cli_state is not None and hasattr(cli_state, "job_artifacts"):
                artifacts = svc.retry_on_locked(lambda: cli_state.job_artifacts(job_id))
            elif cli_state is not None and hasattr(cli_state, "store"):
                raw = svc.retry_on_locked(lambda: cli_state.store.list_artifacts(job_id))
                fmt = state_obj or svc.get_session().state()
                if hasattr(fmt, "format_artifacts"):
                    artifacts = fmt.format_artifacts(raw)
        except Exception:
            pass
    return 200, artifacts


def get_swarm_live(repo_override: str | None, svc: JobServices) -> tuple[int, dict]:
    """GET /api/swarm/live — swarm tracker JSON (auth already applied by Handler)."""
    from ..job_scoping import filter_local_jobs, resolve_job_model
    from ..cli_job_merge import (
        bulk_load_store_artifacts,
        bulk_load_store_tasks,
        cli_stores_by_job,
        partition_jobs_by_store,
    )

    scoped_repo = (repo_override or "").strip() or (svc.cfg.repo or "")
    res_jobs: list = []
    try:
        from pmharness.registry import resolve_price, price_with_source
        from .cost_accounting import _normalize_price_source

        price_in, price_out = resolve_price(svc.cfg.driver)
        raw_in, raw_out, _price_src = price_with_source(svc.cfg.driver)
        price_source = _normalize_price_source(
            None if raw_in is None or raw_out is None else _price_src
        )
    except Exception as exc:
        try:
            from .cost_accounting import _log_price_fallback

            _log_price_fallback("jobs", exc)
        except Exception:
            pass
        price_in, price_out, price_source = 0.5, 2.0, "default"
    try:
        state_obj = svc.get_session().state()
        registry = svc.swarm_registry()
        jobs, store, cli_store = svc.scoped_jobs_with_stores(repo_root=repo_override or None)

        harness_jids, cli_jids = partition_jobs_by_store(jobs)
        foreign_cli = cli_stores_by_job(jobs)
        # Batch all three per-job reads (the old N+1 read artifacts TWICE
        # plus tasks, per job): one bulk artifacts read + one bulk tasks
        # read, regrouped by job_id. Foreign CLI stores (sibling MCP cwd)
        # are loaded per job so tracker cost/savings are not blank.
        arts_by_job: dict = {}
        tasks_by_job: dict = {}
        try:
            harness_arts = bulk_load_store_artifacts(store, harness_jids)
            primary_cli_jids = [j for j in cli_jids if j not in foreign_cli]
            cli_arts = bulk_load_store_artifacts(cli_store, primary_cli_jids)
            arts_by_job = {**harness_arts, **cli_arts}
            for jid, fstore in foreign_cli.items():
                arts_by_job.update(bulk_load_store_artifacts(fstore, [jid]))
        except Exception:
            arts_by_job = None
        try:
            harness_tasks = bulk_load_store_tasks(store, harness_jids)
            primary_cli_jids = [j for j in cli_jids if j not in foreign_cli]
            cli_tasks = bulk_load_store_tasks(cli_store, primary_cli_jids)
            tasks_by_job = {**harness_tasks, **cli_tasks}
            for jid, fstore in foreign_cli.items():
                tasks_by_job.update(bulk_load_store_tasks(fstore, [jid]))
        except Exception:
            tasks_by_job = None

        for j in jobs:
            jid = j.get("id")
            if not jid:
                continue

            if j.get("source") == "cli":
                job_store = foreign_cli.get(jid) or cli_store or store
            else:
                job_store = store
            raw_arts = (arts_by_job.get(jid, []) if arts_by_job is not None
                        else svc.retry_on_locked(lambda: job_store.list_artifacts(jid)))
            # Live poll always ships slim artifacts (routing + verdicts).
            # Full FINDING/RISK streams land on expand via /api/artifacts
            # -- same for in-progress and terminal so StatusBar/SwarmPane
            # polls stay cheap while a swarm is still running.
            job_status = j.get("status", "")
            terminal = svc.job_status_is_terminal(str(job_status))
            try:
                artifacts_list = svc.slim_swarm_list_artifacts(raw_arts, state_obj)
                artifacts_complete = False
            except Exception:
                artifacts_list = []
                artifacts_complete = False

            tokens, est_cost_usd = svc.job_swarm_accounting(raw_arts, registry)
            job_detail = {
                "tokens": tokens,
                "est_cost_usd": est_cost_usd,
                "cost_provenance": "default",
                "estimated": True,
            }
            try:
                from .cost import _server_attr
                from .swarm_cost import _job_swarm_accounting_detail

                detail_fn = _server_attr(
                    "_job_swarm_accounting_detail", _job_swarm_accounting_detail
                )
                detail = detail_fn(raw_arts, registry)
                if (
                    int(detail.get("tokens") or 0) == int(tokens or 0)
                    and abs(
                        float(detail.get("est_cost_usd") or 0.0)
                        - float(est_cost_usd or 0.0)
                    )
                    < 1e-9
                ):
                    job_detail = detail
            except Exception:
                pass
            tokens = int(job_detail.get("tokens") or 0)
            est_cost_usd = float(job_detail.get("est_cost_usd") or 0.0)
            # Per-task meters from raw artifacts (before slim) so worker
            # rows keep tokens/cost even when the artifact list is slimmed.
            try:
                task_accounting = svc.task_swarm_accounting(raw_arts, registry)
            except Exception:
                task_accounting = {}
            # Per-job savings from raw artifacts (before slim). Terminal
            # rows still get these meters even when the artifact list is
            # slimmed -- expand must not be required to see savings.
            job_routing_basis = "unknown"
            job_routing_tokens = 0
            job_routing_counted = False
            job_delegation_saved = 0.0
            job_delegation_basis = "unknown"
            job_delegation_tokens = 0
            job_delegation_counted = False
            try:
                detail_fn = svc.routing_saved_usd_detail
                if detail_fn is not None:
                    try:
                        rdetail = detail_fn(
                            raw_arts,
                            registry,
                            active_price_in=price_in,
                            active_price_out=price_out,
                        )
                    except TypeError:
                        rdetail = detail_fn(raw_arts, registry)
                    job_routing_saved = round(
                        float(rdetail.get("routing_saved_usd") or 0.0), 6
                    )
                    job_routing_basis = str(
                        rdetail.get("routing_savings_basis") or "unknown"
                    )
                    job_routing_tokens = int(
                        rdetail.get("routing_tokens_compared") or 0
                    )
                    job_routing_counted = bool(
                        rdetail.get("routing_savings_counted")
                    )
                else:
                    raise TypeError("no routing detail helper")
            except Exception:
                try:
                    job_routing_saved = round(
                        svc.routing_saved_usd(
                            raw_arts,
                            registry,
                            active_price_in=price_in,
                            active_price_out=price_out,
                        ),
                        6,
                    )
                    # Float-only path (legacy / monkeypatch): treat positive
                    # savings as estimated so session copy stays honest.
                    job_routing_counted = job_routing_saved > 0
                    job_routing_basis = (
                        "estimated" if job_routing_counted else "unknown"
                    )
                except TypeError:
                    try:
                        job_routing_saved = round(svc.routing_saved_usd(raw_arts), 6)
                        job_routing_counted = job_routing_saved > 0
                        job_routing_basis = (
                            "estimated" if job_routing_counted else "unknown"
                        )
                    except Exception:
                        job_routing_saved = 0.0
                except Exception:
                    job_routing_saved = 0.0
            try:
                ddetail_fn = svc.delegation_saved_usd_detail
                if ddetail_fn is not None:
                    try:
                        ddetail = ddetail_fn(
                            raw_arts,
                            registry,
                            active_price_in=price_in,
                            active_price_out=price_out,
                        )
                    except TypeError:
                        ddetail = ddetail_fn(raw_arts, registry)
                    job_delegation_saved = round(
                        float(ddetail.get("delegation_saved_usd") or 0.0), 6
                    )
                    job_delegation_basis = str(
                        ddetail.get("delegation_savings_basis") or "unknown"
                    )
                    job_delegation_tokens = int(
                        ddetail.get("delegation_tokens_compared") or 0
                    )
                    job_delegation_counted = bool(
                        ddetail.get("delegation_savings_counted")
                    )
            except Exception:
                job_delegation_saved = 0.0
            try:
                job_cache_saved = round(
                    svc.cache_saved_usd_swarm(raw_arts, registry), 6
                )
            except Exception:
                job_cache_saved = 0.0
            try:
                job_tokens_cached = int(svc.tokens_cached_swarm(raw_arts) or 0)
            except Exception:
                job_tokens_cached = 0
            job_model = resolve_job_model(
                raw_arts,
                (tasks_by_job.get(jid, []) if tasks_by_job is not None else []),
                j.get("adapter", ""),
            )
            dead_run = svc.job_dead_run_failure(raw_arts, str(job_status))

            tasks_list = []
            try:
                raw_tasks = (tasks_by_job.get(jid, []) if tasks_by_job is not None
                             else svc.retry_on_locked(lambda: job_store.list_tasks(jid)))
                for t in raw_tasks:
                    # Finished cards only need role/status/adapter for the
                    # worker strip; skip long instructions until expand.
                    instr = "" if terminal else (getattr(t, "instruction", "") or "")
                    tid = getattr(t, "id", "") or ""
                    entry = {
                        "id": tid,
                        "role": getattr(t, "role", ""),
                        "instruction": instr,
                        "status": str(getattr(t, "status", "")),
                        "adapter": getattr(t, "adapter", ""),
                        "completed_at": getattr(t, "completed_at", None),
                    }
                    acct = task_accounting.get(tid) if tid else None
                    if acct:
                        t_tokens = int(acct.get("tokens") or 0)
                        t_cost = float(acct.get("est_cost_usd") or 0.0)
                        if t_tokens > 0:
                            entry["tokens"] = t_tokens
                        if t_cost > 0 or (
                            t_tokens == 0 and acct.get("cost_provenance") == "provider"
                        ):
                            entry["est_cost_usd"] = round(t_cost, 6)
                        if acct.get("cost_provenance"):
                            entry["cost_provenance"] = acct.get("cost_provenance")
                        if "estimated" in acct:
                            entry["estimated"] = bool(acct.get("estimated"))
                    tasks_list.append(entry)
            except Exception:
                pass

            row = {
                "id": jid,
                "goal": j.get("goal", ""),
                "status": job_status,
                "role": j.get("role", ""),
                "adapter": j.get("adapter", ""),
                "model": job_model,
                "created_at": j.get("created_at"),
                "task_count": j.get("task_count", 0),
                "tokens": tokens,
                "est_cost_usd": est_cost_usd,
                "cost_provenance": job_detail.get("cost_provenance") or "default",
                "estimated": bool(job_detail.get("estimated", True)),
                "tokens_cached": job_tokens_cached,
                "routing_saved_usd": job_routing_saved,
                "routing_savings_basis": job_routing_basis,
                "routing_tokens_compared": job_routing_tokens,
                "routing_savings_counted": job_routing_counted,
                "delegation_saved_usd": job_delegation_saved,
                "delegation_savings_basis": job_delegation_basis,
                "delegation_tokens_compared": job_delegation_tokens,
                "delegation_savings_counted": job_delegation_counted,
                "cache_saved_usd": job_cache_saved,
                "artifacts": artifacts_list,
                "artifacts_complete": artifacts_complete,
                "tasks": tasks_list,
                "source": j.get("source", "harness"),
                **svc.job_savings_fields(jid),
            }
            if dead_run:
                row["dead_run_failure"] = dead_run
            res_jobs.append(row)
    except Exception as e:
        svc.diag("server.jobs_list_aggregate", e)

    # Merge in-process provider-native worker jobs (job_id "local-*").
    # These run on the user's own key rather than a Puppetmaster adapter,
    # so they never enter the durable store above -- without this the panel
    # reads "No swarm jobs yet" while a worker is visibly running.
    try:
        pilot = svc.get_pilot()
        existing_ids = {j.get("id") for j in res_jobs}
        scoped_locals = filter_local_jobs(
            pilot.live_local_jobs(),
            active_session_id=svc.sessions.active or getattr(pilot, "harness_session_id", "") or "",
            repo_root=scoped_repo,
        )
        for lj in scoped_locals:
            if lj.get("id") not in existing_ids:
                # Local jobs already carry their (tiny) artifact list
                # inline; mark complete so the UI never lazy-fetches.
                if "artifacts_complete" not in lj:
                    lj = {**lj, "artifacts_complete": True}
                res_jobs.append(lj)
    except Exception as e:
        svc.diag("server.jobs_list_merge_local", e)

    # Explicit ?repo= scopes the session block to that workspace's swarm
    # jobs + its session-stamped meters. Never fold the active pilot's
    # process-global meters in -- those may belong to another workspace.
    # Unscoped polls (no repo query) keep active-workspace pilot + jobs.
    repo_scoped = bool((repo_override or "").strip())

    # Mid-run savings: sum per-job routing/cache meters so the live
    # session block matches /api/usage (pilot cache stays separate).
    live_routing_saved = 0.0
    live_delegation_saved = 0.0
    live_cache_saved = 0.0
    live_routing_tokens = 0
    live_delegation_tokens = 0
    saw_routing_actual = False
    saw_routing_estimated = False
    saw_routing_unknown = False
    saw_delegation_actual = False
    saw_delegation_unknown = False
    swarm_cached = 0
    job_tokens_sum = 0
    store_job_cost = 0.0
    try:
        for j in res_jobs:
            if str(j.get("id") or "").startswith("local-"):
                continue
            store_job_cost += float(j.get("est_cost_usd") or 0.0)
            live_routing_saved += float(j.get("routing_saved_usd") or 0.0)
            live_delegation_saved += float(j.get("delegation_saved_usd") or 0.0)
            live_cache_saved += float(j.get("cache_saved_usd") or 0.0)
            live_routing_tokens += int(j.get("routing_tokens_compared") or 0)
            live_delegation_tokens += int(j.get("delegation_tokens_compared") or 0)
            if j.get("routing_savings_counted"):
                basis = str(j.get("routing_savings_basis") or "")
                if basis == "actual_usage":
                    saw_routing_actual = True
                elif basis == "estimated":
                    saw_routing_estimated = True
                else:
                    saw_routing_unknown = True
            if j.get("delegation_savings_counted"):
                dbasis = str(j.get("delegation_savings_basis") or "")
                if dbasis == "actual_usage":
                    saw_delegation_actual = True
                else:
                    saw_delegation_unknown = True
            swarm_cached += int(j.get("tokens_cached") or 0)
            job_tokens_sum += int(j.get("tokens") or 0)
    except Exception:
        pass
    if saw_routing_actual:
        live_routing_basis = "actual_usage"
    elif saw_routing_estimated and not saw_routing_unknown:
        live_routing_basis = "estimated"
    else:
        live_routing_basis = "unknown"
    if saw_delegation_actual:
        live_delegation_basis = "actual_usage"
    else:
        live_delegation_basis = "unknown"

    pilot = svc.get_pilot()
    if repo_scoped:
        stamped = svc.repo_session_stamped_meters(scoped_repo)
        est_session_cost = float(stamped.get("est_cost_usd") or 0.0) + store_job_cost
        tokens_used = int(stamped.get("tokens_used") or 0) + job_tokens_sum
        # In-flight local jobs are not yet in persisted session meters;
        # fold their live row costs in. Terminal locals are already in
        # stamped meters (via _worker_cost_usd -> accumulate_meters).
        try:
            for j in res_jobs:
                if not str(j.get("id") or "").startswith("local-"):
                    continue
                status = str(j.get("status") or "").lower()
                if status in ("completed", "failed", "cancelled", "complete"):
                    continue
                est_session_cost += float(j.get("est_cost_usd") or 0.0)
                tokens_used += int(j.get("tokens") or 0)
        except Exception:
            pass
        tokens_cached = swarm_cached
        _cache_savings_usd = 0.0
        _cache_savings_gross_usd = 0.0
        tool_savings = {}
    else:
        tokens_used = int(getattr(pilot, "_tokens_used", 0) or 0)
        # Accurate split: input tokens at price_in, output at price_out, with
        # cached prompt tokens re-billed at the cache-read discount. Falls
        # back to a single-rate estimate if the in/out split isn't tracked.
        _t_cached = int(getattr(pilot, "_tokens_cached", 0) or 0)
        _w_in = int(getattr(pilot, "_worker_tokens_in", 0) or 0)
        _w_out = int(getattr(pilot, "_worker_tokens_out", 0) or 0)
        est_session_cost = svc.session_cost_split(pilot, price_in, price_out)
        # Add swarm store-job spend from the scoped job list only.
        # Local provider jobs are already inside _worker_cost_usd.
        est_session_cost += store_job_cost
        # Same token parity as /api/usage: pilot-only + store job tokens.
        tokens_used = max(0, tokens_used - _w_in - _w_out) + job_tokens_sum
        pilot_only_cached = max(0, _t_cached - min(_t_cached, swarm_cached))
        tokens_cached = pilot_only_cached + swarm_cached
        _provider_cost = float(getattr(pilot, "_provider_cost_usd", 0) or 0.0)
        tool_savings = svc.tool_output_savings_fields(price_in)
        try:
            from .cost_accounting import (
                _cache_savings_gross,
                _cache_savings_with_basis,
            )

            try:
                _src_for_cap = (
                    svc.cost_source_label(pilot) if pilot is not None else "estimated"
                )
            except Exception:
                _src_for_cap = "estimated"
            # Cap only on provider/mixed receipts — never against estimated spend.
            _cache_savings_usd, _cache_savings_basis = _cache_savings_with_basis(
                pilot_only_cached,
                price_in,
                provider_cost_usd=(
                    _provider_cost
                    if _src_for_cap in ("provider", "mixed")
                    else None
                ),
            )
            _cache_savings_gross_usd = _cache_savings_gross(pilot_only_cached, price_in)
        except Exception:
            _cache_savings_usd = svc.cache_savings(pilot_only_cached, price_in)
            _cache_savings_basis = "catalog"
            _cache_savings_gross_usd = float(_cache_savings_usd or 0.0)

    if repo_scoped:
        _live_cost_source = "estimated"
        _cache_savings_basis = "catalog"
    else:
        try:
            _live_cost_source = svc.cost_source_label(pilot) if pilot is not None else "estimated"
        except Exception:
            _live_cost_source = "estimated"
    try:
        from .cost_accounting import _spend_is_estimated

        _live_estimated = _spend_is_estimated(_live_cost_source, price_source)
    except Exception:
        _live_estimated = _live_cost_source != "provider"
    return 200, {
        "session": {
            "tokens_used": tokens_used,
            "est_cost_usd": round(est_session_cost, 6),
            "cost_source": _live_cost_source,
            "price_source": price_source,
            "estimated": bool(_live_estimated),
            "driver": svc.cfg.driver,
            # Prompt-cache hits (billed at the cache-read discount) so the
            # UI can show how much input was served near-free -- proof the
            # harness is not token-hungry -- plus the USD it saved.
            "tokens_cached": tokens_cached,
            "cache_savings_usd": round(_cache_savings_usd, 6),
            "cache_savings_gross_usd": round(_cache_savings_gross_usd, 6),
            "cache_savings_basis": _cache_savings_basis,
            "routing_saved_usd": round(live_routing_saved, 6),
            "routing_savings_basis": live_routing_basis,
            "routing_tokens_compared": int(live_routing_tokens),
            "delegation_saved_usd": round(live_delegation_saved, 6),
            "delegation_savings_basis": live_delegation_basis,
            "delegation_tokens_compared": int(live_delegation_tokens),
            "cache_saved_usd_swarm": round(live_cache_saved, 6),
            **tool_savings,
        },
        "jobs": res_jobs,
    }
