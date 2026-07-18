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
        partition_jobs_by_store,
    )

    scoped_repo = (repo_override or "").strip() or (svc.cfg.repo or "")
    res_jobs: list = []
    try:
        from pmharness.registry import resolve_price
        price_in, price_out = resolve_price(svc.cfg.driver)
    except Exception:
        price_in, price_out = 0.5, 2.0
    try:
        state_obj = svc.get_session().state()
        registry = svc.swarm_registry()
        jobs, store, cli_store = svc.scoped_jobs_with_stores(repo_root=repo_override or None)

        harness_jids, cli_jids = partition_jobs_by_store(jobs)
        # Batch all three per-job reads (the old N+1 read artifacts TWICE
        # plus tasks, per job): one bulk artifacts read + one bulk tasks
        # read, regrouped by job_id.
        arts_by_job: dict = {}
        tasks_by_job: dict = {}
        try:
            harness_arts = bulk_load_store_artifacts(store, harness_jids)
            cli_arts = bulk_load_store_artifacts(cli_store, cli_jids)
            arts_by_job = {**harness_arts, **cli_arts}
        except Exception:
            arts_by_job = None
        try:
            harness_tasks = bulk_load_store_tasks(store, harness_jids)
            cli_tasks = bulk_load_store_tasks(cli_store, cli_jids)
            tasks_by_job = {**harness_tasks, **cli_tasks}
        except Exception:
            tasks_by_job = None

        for j in jobs:
            jid = j.get("id")
            if not jid:
                continue

            job_store = cli_store if j.get("source") == "cli" and cli_store else store
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
            # Per-task meters from raw artifacts (before slim) so worker
            # rows keep tokens/cost even when the artifact list is slimmed.
            try:
                task_accounting = svc.task_swarm_accounting(raw_arts, registry)
            except Exception:
                task_accounting = {}
            # Per-job savings from raw artifacts (before slim). Terminal
            # rows still get these meters even when the artifact list is
            # slimmed -- expand must not be required to see savings.
            try:
                job_routing_saved = round(svc.routing_saved_usd(raw_arts), 6)
            except Exception:
                job_routing_saved = 0.0
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
                        if t_cost > 0:
                            entry["est_cost_usd"] = round(t_cost, 6)
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
                "tokens_cached": job_tokens_cached,
                "routing_saved_usd": job_routing_saved,
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
    live_cache_saved = 0.0
    swarm_cached = 0
    job_tokens_sum = 0
    store_job_cost = 0.0
    try:
        for j in res_jobs:
            if str(j.get("id") or "").startswith("local-"):
                continue
            store_job_cost += float(j.get("est_cost_usd") or 0.0)
            live_routing_saved += float(j.get("routing_saved_usd") or 0.0)
            live_cache_saved += float(j.get("cache_saved_usd") or 0.0)
            swarm_cached += int(j.get("tokens_cached") or 0)
            job_tokens_sum += int(j.get("tokens") or 0)
    except Exception:
        pass

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
        _cache_savings_usd = svc.cache_savings(pilot_only_cached, price_in)
        tool_savings = svc.tool_output_savings_fields(price_in)

    if repo_scoped:
        _live_cost_source = "estimated"
    else:
        try:
            _live_cost_source = svc.cost_source_label(pilot) if pilot is not None else "estimated"
        except Exception:
            _live_cost_source = "estimated"
    return 200, {
        "session": {
            "tokens_used": tokens_used,
            "est_cost_usd": round(est_session_cost, 6),
            "cost_source": _live_cost_source,
            "driver": svc.cfg.driver,
            # Prompt-cache hits (billed at the cache-read discount) so the
            # UI can show how much input was served near-free -- proof the
            # harness is not token-hungry -- plus the USD it saved.
            "tokens_cached": tokens_cached,
            "cache_savings_usd": round(_cache_savings_usd, 6),
            "routing_saved_usd": round(live_routing_saved, 6),
            "cache_saved_usd_swarm": round(live_cache_saved, 6),
            **tool_savings,
        },
        "jobs": res_jobs,
    }
