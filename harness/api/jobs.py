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


def _inspect_sibling_job(job_id: str, *, strict: bool = False) -> tuple[bool | None, Any]:
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
        if strict:
            raise
        state_dir = None
    if state_dir is None:
        return None, None
    durable = open_cli_durable_at(str(state_dir))
    store = getattr(durable, "store", None) if durable is not None else None
    if store is None:
        if strict:
            raise OSError("Sibling artifact store is unavailable")
        return None, None
    if strict:
        return _artifact_job_owned(durable, job_id, "cli", []), durable
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


def get_cancellation_receipt(qs: dict, svc: JobServices) -> tuple[int, dict]:
    """Read an existing receipt; never create or retry cancellation."""
    fields = ("job_id", "state_id", "source", "repo", "session_id", "request_id")
    if not isinstance(qs, dict):
        return 400, {"ok": False, "code": "invalid_cancellation_query"}
    if any(not isinstance(qs.get(k), list) or len(qs[k]) != 1 for k in fields):
        return 400, {"ok": False, "code": "invalid_cancellation_query",
                     "error": "Invalid receipt query."}
    if (set(qs) - set(fields) - {'version', 'incarnation'}
            or any(not isinstance(qs[k], list) or len(qs[k]) != 1 for k in ('version', 'incarnation') if k in qs)):
        return 400, {"ok": False, "code": "invalid_cancellation_query"}
    values = {k: qs[k][0] for k in fields}
    ref_fields = {}
    if 'version' in qs:
        if qs['version'][0] not in ('1', '2'):
            return 400, {"ok": False, "code": "invalid_cancellation_query"}
        ref_fields['version'] = int(qs['version'][0])
    if 'incarnation' in qs:
        ref_fields['incarnation'] = qs['incarnation'][0]
    selection = {"version": 2, "job_ref": {"job_id": values.pop("job_id"),
                 "state_id": values.pop("state_id"), **ref_fields},
                 **{k: values[k] for k in ("source", "repo", "session_id")}}
    return _scoped_cancel({"selection": selection, "request_id": values["request_id"]}, svc,
                          read_only=True)


def post_swarm_cancel(body: dict, svc: JobServices) -> tuple[int, dict]:
    return _scoped_cancel(body, svc, read_only=False)


def _scoped_cancel(body: dict, svc: JobServices, *, read_only: bool) -> tuple[int, dict]:
    """Cancel one versioned selection without using PM's global job-id flag.

    Durable requests carry the rendered generations into PM's atomic comparison.
    Local workers retain their pilot-owned per-job event.
    """
    from dataclasses import asdict
    from .scoped_cancellation import parse_bindings, parse_job_ref, task_page, runtime_available
    identity_errors = ()
    from puppetmaster.state import state_identity
    from ..cli_job_merge import resolve_cli_state_dir
    from ..job_scoping import job_owned_by_marionette
    from ..paths import same_workspace_path
    from puppetmaster.store_factory import create_store

    refused = (409, {"ok": False, "code": "job_cancel_unavailable",
                     "error": "Cancel is unavailable for this job selection."})
    if not isinstance(body, dict):
        return refused
    if "selection" not in body and not body.get("job_id"):
        return 400, {"ok": False, "error": "missing job_id"}
    selection = body.get("selection")
    if not isinstance(selection, dict) or type(selection.get("version")) is not int or selection["version"] not in (1, 2):
        return refused
    ref = selection.get("job_ref")
    if not isinstance(ref, dict) or not {"job_id", "state_id"} <= ref.keys():
        return refused
    jid, state_id = ref.get("job_id"), ref.get("state_id")
    sid, repo, source = (selection.get(k) for k in ("session_id", "repo", "source"))
    if (not all(isinstance(v, str) and v.strip() for v in (jid, sid, repo))
            or source not in ("harness", "cli", "local")
            or ("job_id" in body and body["job_id"] != jid)):
        return refused
    local_incarnation = selection.get("local_incarnation")
    if "local_incarnation" in selection and (
            source != "local" or not isinstance(local_incarnation, str)
            or not 1 <= len(local_incarnation) <= 128):
        return refused
    if source == "local":
        if not local_incarnation:
            return refused
        if set(ref) != {"job_id", "state_id"}:
            return refused
        job_ref = None
    else:
        if ref.get('version') != 2:
            return 409, {"ok": False, "code": "scoped_kernel_cancellation_required",
                         "error": "Refresh the task view: an incarnation-bound job reference is required."}
        if not runtime_available():
            return 503, {"ok": False, "code": "scoped_cancellation_unsupported"}
        from puppetmaster.identity import StoreIdentityError
        identity_errors = (StoreIdentityError,)
        try:
            job_ref = parse_job_ref(ref)
        except (ValueError, TypeError):
            return refused
        if not read_only and job_ref.version != 2:
            return 409, {"ok": False, "code": "scoped_kernel_cancellation_required",
                         "error": "Refresh the task view: cancellation requires an incarnation-bound job reference."}
    captured_repo = svc.cfg.repo or ""
    captured_sid = getattr(svc.sessions, "active", None)
    if sid != captured_sid or not same_workspace_path(repo, captured_repo):
        return refused
    try:
        pilot = svc.get_pilot()

        def context_matches() -> bool:
            if (getattr(svc.sessions, "active", None) != captured_sid
                    or not same_workspace_path(svc.cfg.repo or "", captured_repo)
                    or (source == "local" and svc.get_pilot() is not pilot)):
                return False
            if source == "local" and local_incarnation is not None:
                index = getattr(pilot, "_local_metadata", None)
                if getattr(index, "incarnation", None) != local_incarnation:
                    return False
            if source == "harness":
                session = svc.get_session()
                root = getattr(session, "state_dir", None)
                if root is None:
                    root = getattr(session.state().store, "root", None)
                return bool(root) and state_identity(root) == state_id
            return True

        if source == "local":
            if read_only or selection["version"] != 1:
                return refused
            if state_id is not None or not jid.startswith("local-"):
                return refused
            if not context_matches():
                return refused
            job = pilot.get_local_job(jid)
            if (not isinstance(job, dict) or job.get("id") != jid
                    or job.get("session_id") != sid
                    or not job.get("cwd") or not same_workspace_path(job["cwd"], repo)
                    or getattr(pilot, "harness_session_id", None) != sid
                    or not context_matches()):
                return refused
            accepted = pilot.cancel_local_job(jid, incarnation=local_incarnation)
            if not accepted or not context_matches():
                return refused
            return 200, {"ok": True, "job_id": jid, "selection": selection,
                         "cancellation": "local_event"}

        if not isinstance(state_id, str) or not state_id or jid.startswith("local-"):
            return refused
        if source == "harness":
            store = svc.get_session().state().store
        else:
            root = resolve_cli_state_dir(captured_repo)
            if not root:
                return 503, {"ok": False, "error": "Job store is unavailable."}
            if state_identity(root) != state_id:
                return refused
            store = create_store("sqlite", root, mode="attach")
        if not runtime_available(store):
            return 503, {"ok": False, "code": "scoped_cancellation_unsupported"}
        if state_identity(store.root) != state_id:
            return refused
        attach = getattr(store, "attach", None)
        if callable(attach):
            attach()
        def owned_summary():
            page = store.list_job_summaries(job_ref=job_ref, limit=1, max_scan=2, max_bytes=8192)
            if page.outcome != 'complete' or len(page.items) != 1:
                return None
            row = page.items[0]
            if (row.deleted or row.job_ref != job_ref or row.session_id != sid
                    or not job_owned_by_marionette(session_id=row.session_id, origin=row.origin or '',
                                                   source=source, allow_registered_heal=False)):
                return None
            return row

        job = owned_summary()
        if job is None:
            return refused
        if selection["version"] != 2:
            return 409, {"ok": False, "code": "scoped_kernel_cancellation_required",
                         "error": "Refresh the task view: durable cancellation requires bound v2 selections."}
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 256:
            return refused
        receipt = store.get_cancellation_receipt(job_ref, request_id)
        if read_only and receipt is None:
            if not context_matches():
                return refused
            return 404, {"ok": False, "code": "cancellation_receipt_missing",
                         "error": "No receipt found. Stop is unconfirmed; an explicit retry must reuse the original request."}
        try:
            bindings = parse_bindings([asdict(b) for b in receipt.bindings] if read_only else selection.get("bindings"))
        except (TypeError, ValueError):
            return refused
        selection = {**selection, "bindings": [asdict(b) for b in bindings]}

        def authority_matches(*, require_complete_view: bool):
            if (not context_matches() or state_identity(store.root) != state_id
                    or (source == "cli" and state_identity(resolve_cli_state_dir(captured_repo)) != state_id)):
                return False
            current_job = owned_summary()
            if current_job is None or any(getattr(current_job, field) != getattr(job, field)
                                         for field in ("origin", "project_id", "session_id")):
                return False
            if require_complete_view:
                page = task_page(store, job_ref)
                if page.outcome != "complete" or {t.id for t in page.items} != {b.task_id for b in bindings}:
                    return False
            for binding in bindings:
                task = store.get_task_by_id(binding.task_id)
                if (task.job_id != jid or task.payload.get("session_id") != sid
                        or not task.payload.get("cwd")
                        or not same_workspace_path(task.payload["cwd"], repo)):
                    return False
            return (context_matches()
                    and (source != "cli" or state_identity(resolve_cli_state_dir(captured_repo)) == state_id))

        if not authority_matches(require_complete_view=not read_only and receipt is None):
            return 409, {"ok": False, "code": "cancellation_view_unavailable",
                         "error": "Task scope changed or is incomplete (maximum 200). Refresh the view; no new stop request was sent."}
        if not read_only:
            receipt = store.request_cancellation(job_ref, request_id, bindings)
        if (not authority_matches(require_complete_view=False) or receipt.job_ref != job_ref or receipt.request_id != request_id
                or (receipt.outcome != "conflict" and receipt.bindings != bindings)):
            return 409, {"ok": False, "code": "cancellation_context_changed",
                         "error": "Cancellation acknowledgement is unavailable for this context. Reconcile the original request."}
        return 200, {"ok": True, "job_id": jid, "selection": selection,
                     "request_id": request_id, "receipt": {**asdict(receipt), "job_ref": receipt.job_ref.as_dict()}}

    except identity_errors:
        return refused
    except Exception as exc:
        svc.diag("server.swarm_cancel_scoped", exc)
        return 503, {"ok": False, "error": "Job cancellation could not be completed."}


def get_jobs(repo_override: str | None, svc: JobServices) -> tuple[int, list]:
    """GET /api/jobs — Marionette-owned job list (harness + owned CLI merge)."""
    from puppetmaster.models import JobRef
    from puppetmaster.state import state_identity

    try:
        harness_root = svc.get_session().state().store.root
    except Exception:
        harness_root = None
    rows = svc.scoped_jobs_snapshot(repo_root=repo_override or None)
    result = []
    for job in rows:
        row = dict(job)
        try:
            source = row.get("source", "harness")
            root = (row.get("cli_state_dir") if source == "cli"
                    else harness_root if source == "harness" else None)
            if root and not str(row.get("id", "")).startswith("local-"):
                row["job_ref"] = JobRef(job_id=row["id"], state_id=state_identity(root)).as_dict()
        except Exception:
            # A list row without a reference cannot authorize a scoped read.
            row.pop("job_ref", None)
        result.append(row)
    return 200, result


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


def get_scoped_artifacts(qs: dict, svc: JobServices) -> tuple[int, Any]:
    """Read one captured JobRef in the active workspace/session; never discover stores."""
    from puppetmaster.models import JobRef
    from puppetmaster.state import state_identity
    from ..cli_job_merge import resolve_cli_state_dir
    from ..job_scoping import job_owned_by_marionette
    from ..paths import same_workspace_path
    from ..state import DurableState

    jid, sid, repo, source, state_id = (
        (qs.get(key) or [""])[0]
        for key in ("job_id", "session_id", "repo", "source", "state_id")
    )
    unavailable = (409, {"code": "job_artifacts_unavailable",
                         "error": "Artifacts are unavailable for this job in the selected workspace and session."})
    captured_repo = svc.cfg.repo or ""
    captured_session = getattr(svc.sessions, "active", None)
    if (not all((jid, sid, repo, state_id)) or source not in ("harness", "cli")
            or sid != captured_session or not same_workspace_path(repo, captured_repo)):
        return unavailable
    try:
        if source == "harness":
            durable = svc.get_session().state()
        else:
            root = resolve_cli_state_dir(captured_repo)
            if not root:
                return 503, {"error": "Artifact store is unavailable."}
            if state_identity(root) != state_id:
                return unavailable
            durable = DurableState(root)
        store = durable.store
        selected_ref = JobRef(job_id=jid, state_id=state_identity(store.root))
        if selected_ref.state_id != state_id:
            return unavailable
        try:
            job = store.get_job(jid)
        except (KeyError, FileNotFoundError):
            return unavailable
        if job is None:
            return unavailable
        registered = getattr(svc.get_pilot(), "_session_job_ids", []) or []
        # Authorize the label before reading task payloads; legacy task-only rows
        # cannot prove scope for this endpoint and remain unavailable.
        if (parse_job_session_id(job.label, []) != sid
                or not job_owned_by_marionette(label=job.label, job_id=jid,
                    source=source, registered_job_ids=registered)):
            return unavailable
        tasks = svc.retry_on_locked(lambda: store.list_tasks(jid))
        if not tasks or any(
            task.payload.get("session_id") != sid
            or not task.payload.get("cwd")
            or not same_workspace_path(task.payload["cwd"], repo)
            for task in tasks
        ):
            return unavailable
        artifacts = svc.retry_on_locked(lambda: durable.job_artifacts(jid))
        if (getattr(svc.sessions, "active", None) != captured_session
                or not same_workspace_path(svc.cfg.repo or "", captured_repo)):
            return unavailable
        return 200, {"job_ref": selected_ref.as_dict(), "source": source,
                     "repo": repo, "session_id": sid, "artifacts": artifacts}
    except Exception:
        return 503, {"error": "Artifact records could not be read."}


def _artifact_job_owned(durable: Any, jid: str, source: str, registered: list) -> bool | None:
    from ..job_scoping import job_owned_by_marionette

    if durable is None:
        return None
    store = durable.store
    getter = getattr(store, "get_job", None)
    try:
        job = getter(jid) if callable(getter) else next(
            (row for row in store.list_jobs() if row.get("id") == jid), None)
    except (KeyError, FileNotFoundError):
        return None
    if job is None:
        return None
    label = job.get("label") if isinstance(job, dict) else job.label
    tasks = store.list_tasks(jid)
    return job_owned_by_marionette(label=label, tasks=tasks, job_id=jid,
        source=source, registered_job_ids=registered)


def get_artifacts(job_id: str | None, svc: JobServices) -> tuple[int, Any]:
    """Legacy id-only read. Ambiguous primary IDs require the scoped endpoint."""
    from ..cli_job_merge import open_cli_durable_state

    jid = (job_id or "").strip()
    if not jid:
        return 400, {"error": "missing job id"}
    try:
        state_obj = svc.get_session().state()
        registered = getattr(svc.get_pilot(), "_session_job_ids", []) or []
        primary_owned = _artifact_job_owned(state_obj, jid, "harness", registered)
        if primary_owned is False:
            return 200, []
        cli_state = open_cli_durable_state(svc.cfg.repo or "")
        cli_owned = _artifact_job_owned(cli_state, jid, "cli", [])
        if primary_owned is True and cli_owned is not None:
            from puppetmaster.state import state_identity

            primary_root = getattr(state_obj.store, "root", None)
            cli_root = getattr(cli_state.store, "root", None)
            if not primary_root or not cli_root or state_identity(primary_root) != state_identity(cli_root):
                return 409, {"code": "job_artifacts_unavailable", "error": "Select a scoped job reference to read artifacts."}
        if primary_owned is True:
            artifacts = svc.retry_on_locked(lambda: state_obj.job_artifacts(jid))
        elif cli_owned is True:
            artifacts = _artifacts_from_durable(cli_state, jid, svc, state_obj)
        elif cli_owned is False:
            return 200, []
        else:
            sibling_owned, sibling_durable = _inspect_sibling_job(jid, strict=True)
            if sibling_owned is not True:
                return 200, []
            artifacts = _artifacts_from_durable(sibling_durable, jid, svc, state_obj)
    except Exception:
        return 503, {"error": "Artifact records could not be read."}
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
        job_read_key,
        job_stores_for_read,
    )

    scoped_repo = (repo_override or "").strip() or (svc.cfg.repo or "")
    res_jobs: list = []
    read_unavailable = False
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

        stores_by_key = job_stores_for_read(jobs, store, cli_store)
        ids_by_store: dict = {}
        for key, job_store in stores_by_key.items():
            if job_store is not None:
                ids_by_store.setdefault((key[0], key[1]), []).append(key[2])
        arts_by_job: dict = {}
        tasks_by_job: dict = {}
        unavailable_by_key: dict = {}
        for (state_id, source), jids in ids_by_store.items():
            job_store = stores_by_key[(state_id, source, jids[0])]
            failed_arts, failed_tasks = set(), set()
            loaded_arts = bulk_load_store_artifacts(job_store, jids, unavailable=failed_arts)
            loaded_tasks = bulk_load_store_tasks(job_store, jids, unavailable=failed_tasks)
            for jid in jids:
                key = (state_id, source, jid)
                unavailable_by_key[key] = (["artifacts"] if jid in failed_arts else []) + (["tasks"] if jid in failed_tasks else [])
                arts_by_job[key] = loaded_arts.get(jid, [])
                tasks_by_job[key] = loaded_tasks.get(jid, [])

        for j in jobs:
            jid = j.get("id")
            if not jid:
                continue

            default_store = cli_store if j.get("source") == "cli" else store
            key = job_read_key(j) if j.get("cli_state_dir") else job_read_key(j, default_store)
            job_store = stores_by_key.get(key)
            raw_arts = arts_by_job.get(key, [])
            raw_tasks = tasks_by_job.get(key, [])
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
                raw_tasks,
                j.get("adapter", ""),
            )
            outcome = canonical_job_outcome(raw_arts)

            tasks_list = []
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

            savings_fields = {
                "tool_output_tokens_saved": 0,
                "tool_output_savings_usd": 0.0,
                "tool_output_compactions": 0,
            }
            if j.get("accounting_owned") and getattr(job_store, "root", None):
                from ..tool_output_savings import merged_savings_summary, savings_usd

                summary = merged_savings_summary(
                    "" if j.get("source") == "cli" else str(job_store.root),
                    cli_state_dirs=[str(job_store.root)] if j.get("source") == "cli" else None,
                    job_id=jid,
                )
                savings_fields = {
                    "tool_output_tokens_saved": summary.tokens_saved,
                    "tool_output_savings_usd": round(savings_usd(summary.tokens_saved, price_in), 6),
                    "tool_output_compactions": summary.record_count,
                }
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
            # Identity comes from the row's actual store, never a CLI-to-harness fallback.
            identity_store = job_store
            if identity_store is not None and getattr(identity_store, "root", None):
                from puppetmaster.models import JobRef
                from puppetmaster.state import state_identity

                row["job_ref"] = JobRef(job_id=jid, state_id=state_identity(identity_store.root)).as_dict()
            unavailable_fields = unavailable_by_key.get(key, []) if job_store is not None else ["artifacts", "tasks"]
            if unavailable_fields:
                row["read_status"] = "unavailable"
                row["unavailable_fields"] = unavailable_fields
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
                        raw_report = load_pm_cost_report(job_store, jid, registry=registry) if job_store is not None else {}
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
            if "artifacts" in unavailable_fields:
                for field in ("tokens", "tokens_in", "tokens_cached", "est_cost_usd", "routing_saved_usd",
                              "delegation_saved_usd", "cache_saved_usd", "outcome"):
                    row.pop(field, None)
                row["cost_provenance"] = "unknown"
                row["estimated"] = True
                from harness.financial_receipt import persistable_pm_receipt
                row["financial_receipt"] = persistable_pm_receipt({})
            res_jobs.append(apply_job_economics_policy(row))
    except Exception as e:
        read_unavailable = True
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
        **({"read_status": "unavailable"} if read_unavailable else {}),
        "session": {
            **({"read_status": "unavailable"} if any(j.get("read_status") == "unavailable" for j in res_jobs) else {}),
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
