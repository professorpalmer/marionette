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
    """Explicit deps for jobs/swarm HTTP handlers (injected by ``server.py``).

    Prefer :func:`make_job_services` in tests — it fills accounting/cost callables
    with inert defaults so handlers can be exercised without a 20-field stub.
    """

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
    job_savings_fields: Callable[[str], dict]
    repo_session_stamped_meters: Callable[[str], dict]
    session_cost_split: Callable[..., float]
    cache_savings: Callable[..., float]
    tool_output_savings_fields: Callable[..., dict]
    cost_source_label: Callable[..., str]
    # Optional: rich routing detail (basis + tokens). Older test stubs omit it.
    routing_saved_usd_detail: Callable[..., dict] | None = None
    delegation_saved_usd_detail: Callable[..., dict] | None = None
    cache_saved_usd_swarm_detail: Callable[..., dict] | None = None


def make_job_services(**overrides: Any) -> JobServices:
    """Build a :class:`JobServices` with inert defaults for unit tests.

    Pass only the deps a test cares about (e.g. ``get_pilot``, ``get_session``);
    accounting and cost callables default to zero/empty no-ops.
    """

    def _noop(*_a: Any, **_k: Any) -> None:
        return None

    defaults: dict = {
        "cfg": None,
        "sessions": None,
        "get_pilot": lambda: None,
        "get_session": lambda: None,
        "diag": _noop,
        "scoped_jobs_snapshot": lambda **_k: [],
        "scoped_jobs_with_stores": lambda **_k: ([], None, None),
        "retry_on_locked": lambda fn: fn(),
        "swarm_registry": lambda: [],
        "job_status_is_terminal": lambda _s: False,
        "slim_swarm_list_artifacts": lambda *_a, **_k: [],
        "job_swarm_accounting": lambda *_a, **_k: (0, 0, 0),
        "task_swarm_accounting": lambda *_a, **_k: {},
        "routing_saved_usd": lambda *_a, **_k: 0.0,
        "cache_saved_usd_swarm": lambda *_a, **_k: 0.0,
        "tokens_cached_swarm": lambda *_a, **_k: 0,
        "job_savings_fields": lambda *_a, **_k: {},
        "repo_session_stamped_meters": lambda *_a, **_k: {},
        "session_cost_split": lambda *_a, **_k: 0.0,
        "cache_savings": lambda *_a, **_k: 0.0,
        "tool_output_savings_fields": lambda *_a, **_k: {},
        "cost_source_label": lambda *_a, **_k: "",
        "routing_saved_usd_detail": None,
        "delegation_saved_usd_detail": None,
        "cache_saved_usd_swarm_detail": None,
    }
    defaults.update(overrides)
    return JobServices(**defaults)


def canonical_job_outcome(raw_artifacts: list[Any]) -> dict[str, Any]:
    """Return Puppetmaster's artifact-quality verdict, fail-closed on kernel errors.

    Lifecycle stays in ``job.status``. This is chrome only — never invent a
    status string. If the kernel is missing or assess throws, treat the run as
    untrustworthy rather than painting a green default.
    """
    try:
        from puppetmaster.quality import assess_run_quality

        return assess_run_quality(raw_artifacts or [])
    except Exception:
        return {
            "quality": "empty",
            "reasons": ["run quality could not be assessed"],
            "trustworthy": False,
            "blocking_failures": [],
        }


def _job_access_owned(job_id: str, svc: JobServices) -> bool | None:
    """True if owned, False if known-unowned, None if not found in known stores.

    Harness/local registered-id healing applies only to the harness store.
    A colliding id in a CLI store cannot inherit that registry.
    """
    from ..job_scoping import inspect_store_job_ownership

    registered: list = []
    try:
        registered = list(getattr(svc.get_pilot(), "_session_job_ids", []) or [])
    except Exception:
        registered = []

    harness_store = None
    try:
        harness_store = svc.get_session().state().store
    except Exception:
        harness_store = None
    found = inspect_store_job_ownership(
        harness_store,
        job_id,
        source="harness",
        registered_job_ids=registered,
        allow_registered_heal=True,
    )
    if found is not None:
        return found

    cli_store = None
    try:
        from ..cli_job_merge import open_cli_durable_state

        cli_state = open_cli_durable_state(svc.cfg.repo or "")
        cli_store = getattr(cli_state, "store", None) if cli_state is not None else None
    except Exception:
        cli_store = None
    return inspect_store_job_ownership(
        cli_store,
        job_id,
        source="cli",
        registered_job_ids=None,
        allow_registered_heal=False,
    )


def _inspect_sibling_job(job_id: str) -> tuple[bool | None, Any]:
    """Inspect sibling-store ownership after a primary miss.

    Returns ``(owned, durable)``. ``owned`` is True/False when the job is in
    that store, else None. ``durable`` is the already-open sibling state so
    cancel/artifacts can reuse it. Registered-id healing stays off.
    """
    from ..cli_job_merge import cross_project_scan_enabled, open_cli_durable_at
    from ..job_scoping import inspect_store_job_ownership

    if not cross_project_scan_enabled():
        return None, None
    try:
        from puppetmaster.state import find_state_dir_for_job

        state_dir = find_state_dir_for_job(job_id)
    except Exception:
        state_dir = None
    if state_dir is None:
        return None, None
    durable = open_cli_durable_at(str(state_dir))
    store = getattr(durable, "store", None) if durable is not None else None
    if store is None:
        return None, None
    owned = inspect_store_job_ownership(
        store,
        job_id,
        source="cli",
        registered_job_ids=None,
        allow_registered_heal=False,
    )
    return owned, durable


def _inspect_local_job_ownership(job_id: str, svc: JobServices) -> bool | None:
    """True if this session may cancel the local job, False if foreign, None if absent.

    Cancel is session/registered-gated even though the tracker lists all
    Marionette-owned locals. A row from another session is unknown.
    """
    try:
        pilot = svc.get_pilot()
    except Exception:
        return None
    if pilot is None:
        return None

    job = None
    getter = getattr(pilot, "get_local_job", None)
    if callable(getter):
        try:
            job = getter(job_id)
        except Exception:
            job = None
    if not isinstance(job, dict):
        live = getattr(pilot, "live_local_jobs", None)
        if callable(live):
            try:
                for row in live() or []:
                    if str((row or {}).get("id") or "") == job_id:
                        job = row
                        break
            except Exception:
                job = None
    if not isinstance(job, dict):
        return None

    registered: list = []
    try:
        registered = list(getattr(pilot, "_session_job_ids", []) or [])
    except Exception:
        registered = []
    if job_id in {str(x).strip() for x in registered if x}:
        return True

    active_session_id = ""
    try:
        active_session_id = (
            getattr(svc.sessions, "active", None)
            or getattr(pilot, "harness_session_id", "")
            or ""
        )
    except Exception:
        active_session_id = getattr(pilot, "harness_session_id", "") or ""
    job_sid = str(job.get("session_id") or "").strip()
    if job_sid and job_sid == str(active_session_id or "").strip():
        return True
    return False


def _trip_owned_cancel(job_id: str, svc: JobServices) -> None:
    """Trip the in-process kill switch only after positive ownership."""
    try:
        from puppetmaster.cancellation import request_cancel

        request_cancel(job_id)
    except Exception as e:
        svc.diag("server.swarm_cancel_flag", e)


def _unknown_job_refusal(job_id: str) -> tuple[int, dict]:
    return 404, {"ok": False, "error": "unknown job_id", "job_id": job_id}


def _artifacts_from_durable(durable: Any, job_id: str, svc: JobServices, state_obj: Any) -> list:
    """Load formatted artifacts from an already-open DurableState-like object."""
    if durable is None:
        return []
    if hasattr(durable, "job_artifacts"):
        return list(svc.retry_on_locked(lambda: durable.job_artifacts(job_id)) or [])
    store = getattr(durable, "store", None)
    if store is None or not hasattr(store, "list_artifacts"):
        return []
    raw = svc.retry_on_locked(lambda: store.list_artifacts(job_id))
    fmt = state_obj
    if fmt is None or not hasattr(fmt, "format_artifacts"):
        try:
            fmt = svc.get_session().state()
        except Exception:
            fmt = durable
    if hasattr(fmt, "format_artifacts"):
        return list(fmt.format_artifacts(raw) or [])
    if hasattr(durable, "format_artifacts"):
        return list(durable.format_artifacts(raw) or [])
    return []


def post_swarm_cancel(body: dict, svc: JobServices) -> tuple[int, dict]:
    """Cooperative cancel for a swarm job. Best-effort and never raises.

    Local (provider-worker) jobs are cancelled via the per-job Event on the
    conversation; durable store jobs are marked cancelled in the store where
    possible. Shape: ``{ok, job_id}`` or ``{ok:false, error}``.
    """
    job_id = (body.get("job_id") or "").strip()
    if not job_id:
        return 400, {"ok": False, "error": "missing job_id"}

    local_owned = _inspect_local_job_ownership(job_id, svc)
    if local_owned is False:
        return _unknown_job_refusal(job_id)
    if local_owned is True:
        _trip_owned_cancel(job_id, svc)
        try:
            pilot = svc.get_pilot()
            if hasattr(pilot, "cancel_local_job") and pilot.cancel_local_job(job_id):
                return 200, {"ok": True, "job_id": job_id}
        except Exception as e:
            svc.diag("server.swarm_cancel_local", e)

    owned = _job_access_owned(job_id, svc)
    sibling_durable = None
    if owned is False:
        return _unknown_job_refusal(job_id)
    if owned is None:
        sibling_owned, sibling_durable = _inspect_sibling_job(job_id)
        if sibling_owned is not True:
            return _unknown_job_refusal(job_id)

    _trip_owned_cancel(job_id, svc)

    if sibling_durable is not None:
        try:
            from ..job_cancel import mark_store_job_cancelled

            store = getattr(sibling_durable, "store", None)
            return 200, {
                "ok": True,
                "job_id": job_id,
                "durable": True,
                "marked": mark_store_job_cancelled(store, job_id),
            }
        except Exception as e:
            svc.diag("server.swarm_cancel_sibling", e)
            return _unknown_job_refusal(job_id)

    # Durable Puppetmaster store job — harness then primary CLI only.
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
    return _unknown_job_refusal(job_id)


def get_jobs(repo_override: str | None, svc: JobServices) -> tuple[int, list]:
    """GET /api/jobs — Marionette-owned job list (harness + owned CLI merge)."""
    return 200, svc.scoped_jobs_snapshot(repo_root=repo_override or None)


def get_job_events(qs: dict, svc: JobServices) -> tuple[int, Any]:
    """GET /api/jobs/events and GET /api/jobs/<id>/events — ``?include=``."""
    qs = qs or {}
    job_id = (
        (qs.get("job_id") or qs.get("id") or [""])[0] or ""
    ).strip()
    if not job_id:
        return 400, {"error": "missing job id"}
    include = ((qs.get("include") or ["lifecycle"])[0] or "lifecycle").strip()
    since_raw = (qs.get("since") or qs.get("cursor") or ["0"])[0]
    try:
        cursor = int(since_raw or 0)
    except (TypeError, ValueError):
        cursor = 0
    owned = _job_access_owned(job_id, svc)
    durable = None
    if owned is False:
        return _unknown_job_refusal(job_id)
    if owned is None:
        sibling_owned, sibling_durable = _inspect_sibling_job(job_id)
        if sibling_owned is not True:
            return _unknown_job_refusal(job_id)
        durable = sibling_durable
    else:
        try:
            durable = svc.get_session().state()
        except Exception:
            durable = None
    if durable is None or not hasattr(durable, "events_since"):
        return 200, {"events": [], "cursor": cursor}
    try:
        payload = durable.events_since(job_id, cursor, include=include)
    except TypeError:
        payload = durable.events_since(job_id, cursor)
    if not isinstance(payload, dict):
        payload = {"events": payload or [], "cursor": cursor}
    return 200, payload


def get_artifacts(job_id: str | None, svc: JobServices) -> tuple[int, Any]:
    """GET /api/artifacts — owned dual-store resolve (harness, then CLI durable).

    A primary miss inspects the sibling store and reads it only when owned.
    Unknown or unowned ids return empty without reading by id.
    """
    jid = (job_id or "").strip()
    if not jid:
        return 400, {"error": "missing job id"}
    owned = _job_access_owned(jid, svc)
    artifacts: list = []
    state_obj = None
    try:
        state_obj = svc.get_session().state()
    except Exception:
        state_obj = None
    if owned is False:
        return 200, []
    if owned is None:
        sibling_owned, sibling_durable = _inspect_sibling_job(jid)
        if sibling_owned is not True:
            return 200, []
        try:
            artifacts = _artifacts_from_durable(sibling_durable, jid, svc, state_obj)
        except Exception:
            artifacts = []
    else:
        try:
            if state_obj is not None:
                artifacts = svc.retry_on_locked(lambda: state_obj.job_artifacts(jid))
        except Exception:
            artifacts = []
        if not artifacts:
            try:
                from ..cli_job_merge import open_cli_durable_state

                cli_state = open_cli_durable_state(svc.cfg.repo or "")
                artifacts = _artifacts_from_durable(cli_state, jid, svc, state_obj)
            except Exception:
                pass
    try:
        from ..session_fts import best_effort_index_job_artifacts

        best_effort_index_job_artifacts(
            svc.cfg.state_dir or "",
            jid,
            artifacts=artifacts,
            durable=state_obj,
        )
    except Exception:
        pass
    return 200, artifacts


def get_swarm_live(repo_override: str | None, svc: JobServices) -> tuple[int, dict]:
    """GET /api/swarm/live — swarm tracker JSON (auth already applied by Handler)."""
    from ..job_scoping import (
        apply_job_economics_policy,
        annotate_job_accounting,
        filter_local_jobs,
        job_repo_cwd,
        parse_job_dispatch_id,
        parse_job_session_id,
        resolve_job_model,
    )
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
        from .cost_accounting import PRICE_SOURCE_UNKNOWN, _normalize_price_source

        price_in, price_out = resolve_price(svc.cfg.driver)
        raw_in, raw_out, _price_src = price_with_source(svc.cfg.driver)
        if price_in is None or price_out is None:
            # Explicit OpenRouter unknown: fail closed (no fabricated dollars).
            price_in, price_out, price_source = 0.0, 0.0, PRICE_SOURCE_UNKNOWN
        else:
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
            job_cache_basis = "unknown"
            job_cache_unpriced_tokens = 0
            try:
                cache_detail_fn = svc.cache_saved_usd_swarm_detail
                if cache_detail_fn is not None:
                    cache_detail = cache_detail_fn(raw_arts, registry)
                    job_cache_saved = round(
                        float(cache_detail.get("cache_saved_usd_swarm") or 0.0),
                        6,
                    )
                    job_cache_basis = str(
                        cache_detail.get("swarm_cache_savings_basis") or "unknown"
                    )
                    job_cache_unpriced_tokens = int(
                        cache_detail.get("swarm_cache_unpriced_tokens") or 0
                    )
                else:
                    raise TypeError("no swarm cache detail helper")
            except Exception:
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
            try:
                from .swarm_cost import _tokens_in_swarm

                job_tokens_in = int(_tokens_in_swarm(raw_arts) or 0)
            except Exception:
                job_tokens_in = 0
            job_model = resolve_job_model(
                raw_arts,
                (tasks_by_job.get(jid, []) if tasks_by_job is not None else []),
                j.get("adapter", ""),
            )
            outcome = canonical_job_outcome(raw_arts)

            tasks_list = []
            raw_tasks = []
            try:
                raw_tasks = (tasks_by_job.get(jid, []) if tasks_by_job is not None
                             else svc.retry_on_locked(lambda: job_store.list_tasks(jid)))
            except Exception:
                raw_tasks = []
            job_cwd = job_repo_cwd(raw_tasks)
            try:
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
                    task_model = resolve_job_model(
                        [a for a in raw_arts if getattr(a, "task_id", "") == tid],
                        [t],
                    ) if tid else ""
                    if task_model:
                        entry["model"] = task_model
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

            savings_fields = (
                svc.job_savings_fields(jid)
                if j.get("accounting_owned")
                else {
                    "tool_output_tokens_saved": 0,
                    "tool_output_savings_usd": 0.0,
                    "tool_output_compactions": 0,
                }
            )
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
                "tokens_in": job_tokens_in,
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
                "swarm_cache_savings_basis": job_cache_basis,
                "swarm_cache_unpriced_tokens": job_cache_unpriced_tokens,
                "artifacts": artifacts_list,
                "artifacts_complete": artifacts_complete,
                "outcome": outcome,
                "tasks": tasks_list,
                "source": j.get("source", "harness"),
                "label": j.get("label"),
                "dispatch_id": parse_job_dispatch_id(j.get("label")),
                "session_id": j.get("session_id") or parse_job_session_id(j.get("label"), raw_tasks),
                "accounting_scope": j.get("accounting_scope", "visibility_only"),
                "accounting_owned": bool(j.get("accounting_owned")),
                "cross_project": bool(j.get("cross_project")),
                **savings_fields,
            }
            if j.get("cli_state_dir"):
                row["cli_state_dir"] = j.get("cli_state_dir")
            if job_cwd:
                row["cwd"] = job_cwd
            # Optional validation-reuse provenance (absent on legacy rows).
            for _rk in (
                "reuse_status",
                "source_job_id",
                "validation_fingerprint",
                "invalidated_paths",
                "reuse_reason",
            ):
                if j.get(_rk) not in (None, "", [], {}):
                    row[_rk] = j.get(_rk)
            if str(jid).startswith("job_"):
                try:
                    from harness.financial_receipt import (
                        load_pm_cost_report,
                        persistable_pm_receipt,
                    )
                    try:
                        raw_report = load_pm_cost_report(job_store, jid, registry=registry)
                    except Exception:
                        raw_report = {}
                    receipt = persistable_pm_receipt(raw_report)
                    row["financial_receipt"] = receipt
                    if receipt.get("spend_usd") is not None:
                        row["est_cost_usd"] = receipt["spend_usd"]
                        row["estimated"] = bool(receipt.get("estimated"))
                        row["cost_provenance"] = receipt.get("cost_provenance") or row.get("cost_provenance")
                    elif receipt.get("spend_basis") == "unavailable":
                        # Do not keep a routing forecast as spend.
                        row["est_cost_usd"] = 0.0
                        row["estimated"] = True
                        row["cost_provenance"] = "unknown"
                    if receipt.get("route_forecast_usd") is not None:
                        row["route_forecast_usd"] = receipt.get("route_forecast_usd")
                    tasks = row.get("tasks") or []
                    actual_tasks = ((raw_report.get("actual_cost") or {}).get("tasks") or [])
                    priced_by_id = {
                        str(t.get("task_id")): t
                        for t in actual_tasks
                        if isinstance(t, dict) and t.get("task_id")
                    }
                    if len(tasks) == 1 and receipt.get("spend_usd") is not None:
                        tasks[0]["est_cost_usd"] = receipt["spend_usd"]
                        tasks[0]["estimated"] = bool(receipt.get("estimated"))
                        tasks[0]["cost_provenance"] = receipt.get("cost_provenance")
                    elif priced_by_id:
                        for task in tasks:
                            pt = priced_by_id.get(str(task.get("id") or ""))
                            if not pt:
                                continue
                            if pt.get("priced") and pt.get("marginal_cost_usd") is not None:
                                task["est_cost_usd"] = round(float(pt["marginal_cost_usd"]), 6)
                                task["estimated"] = bool(pt.get("tokens_estimated"))
                                task["cost_provenance"] = "static"
                    elif len(tasks) > 1:
                        for task in tasks:
                            task.pop("est_cost_usd", None)
                except Exception:
                    pass
            res_jobs.append(apply_job_economics_policy(row))
    except Exception as e:
        svc.diag("server.jobs_list_aggregate", e)

    # Merge in-process provider-native worker jobs (job_id "local-*").
    # These run on the user's own key rather than a Puppetmaster adapter,
    # so they never enter the durable store above -- without this the panel
    # reads "No swarm jobs yet" while a worker is visibly running.
    try:
        from ..local_job_swarm_view import merge_local_jobs_into_swarm_live

        pilot = svc.get_pilot()
        active_session_id = svc.sessions.active or getattr(pilot, "harness_session_id", "") or ""
        registered_job_ids = list(getattr(pilot, "_session_job_ids", []) or [])
        scoped_locals = filter_local_jobs(
            pilot.live_local_jobs(),
            active_session_id=active_session_id,
            repo_root=scoped_repo,
            registered_job_ids=registered_job_ids,
        )
        scoped_locals = [
            apply_job_economics_policy(
                annotate_job_accounting(
                    job,
                    active_session_id=active_session_id,
                    registered_job_ids=registered_job_ids,
                )
            )
            for job in scoped_locals
        ]
        res_jobs = merge_local_jobs_into_swarm_live(res_jobs, scoped_locals)
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
    saw_cache_actual = False
    saw_cache_unknown = False
    live_cache_unpriced_tokens = 0
    swarm_cached = 0
    swarm_input = 0
    job_tokens_sum = 0
    store_job_cost = 0.0
    store_job_measured = 0.0
    store_job_estimated = 0.0
    try:
        for j in res_jobs:
            if not j.get("accounting_owned"):
                continue
            is_local = str(j.get("id") or "").startswith("local-")
            if not is_local:
                _job_cost = float(j.get("est_cost_usd") or 0.0)
                store_job_cost += _job_cost
                if j.get("estimated") is False:
                    store_job_measured += _job_cost
                else:
                    store_job_estimated += _job_cost
                job_tokens_sum += int(j.get("tokens") or 0)
            # Savings meters: every visible row counts once (store + local).
            # merge_local_jobs_into_swarm_live already dedupes store ids.
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
            job_cached = int(j.get("tokens_cached") or 0)
            swarm_cached += job_cached
            swarm_input += int(j.get("tokens_in") or 0)
            live_cache_unpriced_tokens += int(
                j.get("swarm_cache_unpriced_tokens") or 0
            )
            if job_cached > 0:
                if j.get("swarm_cache_savings_basis") == "actual_usage":
                    saw_cache_actual = True
                else:
                    saw_cache_unknown = True
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
    if saw_cache_actual and not saw_cache_unknown and live_cache_unpriced_tokens == 0:
        live_cache_basis = "actual_usage"
    else:
        live_cache_basis = "unknown"

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
        try:
            from .cost_accounting import _source_owned_cache_lanes

            cache_lanes = _source_owned_cache_lanes(
                pilot_tokens_in=0,
                pilot_tokens_cached=0,
                swarm_tokens_in=swarm_input,
                swarm_tokens_cached=swarm_cached,
            )
        except Exception:
            cache_lanes = {
                "pilot_input_tokens": 0,
                "pilot_cache_read_tokens": 0,
                "pilot_cache_hit_ratio": None,
                "swarm_input_tokens": int(swarm_input),
                "swarm_cache_read_tokens": int(swarm_cached),
                "swarm_cache_hit_ratio": None,
                "prompt_input_tokens": int(swarm_input),
                "prompt_cache_read_tokens": int(swarm_cached),
                "prompt_cache_hit_ratio": None,
                "tokens_cached": int(swarm_cached),
                "pilot_cache_savings_tokens": 0,
            }
        tokens_cached = int(cache_lanes["tokens_cached"])
        pilot_only_cached = 0
        _cache_savings_usd = 0.0
        _cache_savings_gross_usd = 0.0
        tool_savings = {}
    else:
        tokens_used = int(getattr(pilot, "_tokens_used", 0) or 0)
        # Accurate split: input tokens at price_in, output at price_out, with
        # cached prompt tokens re-billed at the cache-read discount. Falls
        # back to a single-rate estimate if the in/out split isn't tracked.
        _t_in = int(getattr(pilot, "_tokens_in", 0) or 0)
        _t_cached = int(getattr(pilot, "_tokens_cached", 0) or 0)
        _w_in = int(getattr(pilot, "_worker_tokens_in", 0) or 0)
        _w_out = int(getattr(pilot, "_worker_tokens_out", 0) or 0)
        _w_cached = int(getattr(pilot, "_worker_tokens_cached", 0) or 0)
        est_session_cost = svc.session_cost_split(pilot, price_in, price_out)
        # Add swarm store-job spend from the scoped job list only.
        # Local provider jobs are already inside _worker_cost_usd.
        est_session_cost += store_job_cost
        # Same token parity as /api/usage: pilot-only + store job tokens.
        tokens_used = max(0, tokens_used - _w_in - _w_out) + job_tokens_sum
        try:
            from .cost_accounting import _source_owned_cache_lanes

            cache_lanes = _source_owned_cache_lanes(
                pilot_tokens_in=_t_in,
                pilot_tokens_cached=_t_cached,
                worker_tokens_in=_w_in,
                worker_tokens_cached=_w_cached,
                swarm_tokens_in=swarm_input,
                swarm_tokens_cached=swarm_cached,
            )
        except Exception:
            cache_lanes = {
                "pilot_input_tokens": max(0, _t_in - _w_in),
                "pilot_cache_read_tokens": max(0, _t_cached - _w_cached),
                "pilot_cache_hit_ratio": None,
                "swarm_input_tokens": int(swarm_input),
                "swarm_cache_read_tokens": int(swarm_cached),
                "swarm_cache_hit_ratio": None,
                "prompt_input_tokens": max(0, _t_in - _w_in) + int(swarm_input),
                "prompt_cache_read_tokens": max(0, _t_cached - _w_cached)
                + int(swarm_cached),
                "prompt_cache_hit_ratio": None,
                "tokens_cached": max(0, _t_cached - _w_cached) + int(swarm_cached),
                "pilot_cache_savings_tokens": max(0, _t_cached - _w_cached),
            }
        pilot_only_cached = int(cache_lanes["pilot_cache_savings_tokens"])
        tokens_cached = int(cache_lanes["tokens_cached"])
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
    session_measured = float(store_job_measured)
    session_estimated = float(store_job_estimated)
    pilot_portion = max(0.0, float(est_session_cost) - float(store_job_cost))
    if _live_estimated:
        session_estimated += pilot_portion
    else:
        session_measured += pilot_portion
    return 200, {
        "session": {
            "tokens_used": tokens_used,
            "est_cost_usd": round(est_session_cost, 6),
            "measured_cost_usd": round(session_measured, 6),
            "estimated_cost_usd": round(session_estimated, 6),
            "cost_source": _live_cost_source,
            "price_source": price_source,
            "estimated": bool(_live_estimated),
            "driver": svc.cfg.driver,
            # Prompt-cache hits (billed at the cache-read discount) so the
            # UI can show how much input was served near-free -- proof the
            # harness is not token-hungry -- plus the USD it saved.
            "tokens_cached": tokens_cached,
            "pilot_input_tokens": int(cache_lanes.get("pilot_input_tokens") or 0),
            "pilot_cache_read_tokens": int(
                cache_lanes.get("pilot_cache_read_tokens") or 0
            ),
            "pilot_cache_hit_ratio": cache_lanes.get("pilot_cache_hit_ratio"),
            "swarm_input_tokens": int(cache_lanes.get("swarm_input_tokens") or 0),
            "swarm_cache_read_tokens": int(
                cache_lanes.get("swarm_cache_read_tokens") or 0
            ),
            "swarm_cache_hit_ratio": cache_lanes.get("swarm_cache_hit_ratio"),
            "prompt_input_tokens": int(cache_lanes.get("prompt_input_tokens") or 0),
            "prompt_cache_read_tokens": int(
                cache_lanes.get("prompt_cache_read_tokens") or 0
            ),
            "prompt_cache_hit_ratio": cache_lanes.get("prompt_cache_hit_ratio"),
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
            "swarm_cache_savings_basis": live_cache_basis,
            "swarm_cache_unpriced_tokens": int(live_cache_unpriced_tokens),
            **tool_savings,
        },
        "jobs": res_jobs,
    }
