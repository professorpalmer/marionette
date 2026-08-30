from __future__ import annotations

"""Marionette-owned views over the durable Puppetmaster job store.

Jobs are stamped with ``origin=marionette`` and ``session_id`` at dispatch
(job label + task payload). Visibility requires that ownership proof, or a
job id registered by the active session. cwd/repo match is not ownership.
"""

import json
import os
from typing import Any, Iterable, Optional

from .paths import same_workspace_path

ACCOUNTING_SCOPE_MARIONETTE = "marionette"
ACCOUNTING_SCOPE_VISIBILITY = "visibility_only"

# Economic fields zeroed on owned-but-unbilled rows (accounting fail-closed).
_JOB_ECONOMIC_FIELDS = (
    "tokens",
    "est_cost_usd",
    "tokens_cached",
    "routing_saved_usd",
    "routing_tokens_compared",
    "delegation_saved_usd",
    "delegation_tokens_compared",
    "cache_saved_usd",
    "tool_output_tokens_saved",
    "tool_output_savings_usd",
    "tool_output_compactions",
)


def current_app_run_id() -> str:
    """Process-scoped app id (HARNESS_APP_RUN_ID / usage_meters seam)."""
    try:
        from harness.api.usage_meters import _app_run_id

        return _app_run_id()
    except Exception:
        return (os.environ.get("HARNESS_APP_RUN_ID") or "").strip()


def cli_cost_merge_enabled() -> bool:
    """HARNESS_CLI_COST_MERGE — default OFF; never makes arbitrary CLI jobs billable."""
    raw = (os.environ.get("HARNESS_CLI_COST_MERGE") or "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def job_label_for_session(
    session_id: str,
    *,
    app_run_id: str = "",
    origin: str = "marionette",
    dispatch_id: str = "",
) -> Optional[str]:
    """JSON job label carrying session + Marionette provenance for dispatch."""
    sid = (session_id or "").strip()
    if not sid:
        return None
    data: dict[str, str] = {"session_id": sid}
    if dispatch_id:
        data["dispatch_id"] = str(dispatch_id).strip()
    if origin:
        data["origin"] = str(origin)
    run_id = (app_run_id or current_app_run_id()).strip()
    if run_id:
        data["app_run_id"] = run_id
        data["app_instance_id"] = run_id
    return json.dumps(data)


def stamp_task_payload(
    payload: dict,
    *,
    session_id: str = "",
    cwd: str = "",
    origin: str = "marionette",
    app_run_id: str = "",
) -> dict:
    """Return a copy of ``payload`` with session/repo/provenance fields."""
    out = dict(payload or {})
    if cwd:
        out.setdefault("cwd", os.path.abspath(cwd) if os.path.isabs(cwd) or cwd else cwd)
    sid = (session_id or "").strip()
    if sid:
        out["session_id"] = sid
    if origin:
        out.setdefault("origin", str(origin))
    run_id = (app_run_id or current_app_run_id()).strip()
    if run_id:
        out.setdefault("app_run_id", run_id)
        out.setdefault("app_instance_id", run_id)
    return out


def _parse_label_dict(label: Any) -> dict:
    if not label:
        return {}
    text = str(label).strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def parse_job_origin(label: Any, tasks: list) -> str:
    """Extract dispatch origin from label or task payloads (backward compatible)."""
    origin = _parse_label_dict(label).get("origin")
    if origin:
        return str(origin)
    for task in tasks or []:
        payload = getattr(task, "payload", None) or {}
        if isinstance(payload, dict) and payload.get("origin"):
            return str(payload.get("origin"))
    return ""


def parse_job_app_run_id(label: Any, tasks: list) -> str:
    """Extract app run id from label or task payloads."""
    data = _parse_label_dict(label)
    for key in ("app_run_id", "app_instance_id"):
        if data.get(key):
            return str(data[key])
    for task in tasks or []:
        payload = getattr(task, "payload", None) or {}
        if not isinstance(payload, dict):
            continue
        for key in ("app_run_id", "app_instance_id"):
            if payload.get(key):
                return str(payload[key])
    return ""


def parse_job_session_id(label: Any, tasks: list) -> str:
    """Extract a stamped session id from the job label or task payloads."""
    if label:
        text = str(label).strip()
        if text:
            try:
                data = json.loads(text)
                if isinstance(data, dict):
                    sid = data.get("session_id") or data.get("harness_session_id")
                    if sid:
                        return str(sid)
            except Exception:
                pass
    for task in tasks or []:
        payload = getattr(task, "payload", None) or {}
        if not isinstance(payload, dict):
            continue
        sid = payload.get("session_id") or payload.get("harness_session_id")
        if sid:
            return str(sid)
    return ""


def parse_job_dispatch_id(label: Any) -> str:
    """Extract the host dispatch identity stamped before PM starts workers."""
    return str(_parse_label_dict(label).get("dispatch_id") or "").strip()


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def job_repo_cwd(tasks: list) -> str:
    """Longest normalized ``cwd`` found on task payloads (deepest wins)."""
    cwds: list[str] = []
    for task in tasks or []:
        if isinstance(task, dict):
            payload = task.get("payload") or {}
        else:
            payload = getattr(task, "payload", None) or {}
        if not isinstance(payload, dict):
            continue
        cwd = (payload.get("cwd") or "").strip()
        if cwd:
            cwds.append(_norm_path(cwd))
    return max(cwds, key=len) if cwds else ""


def cwd_under_repo(cwd: str, repo_root: str) -> bool:
    """True when ``cwd`` sits under ``repo_root`` (prefix, alias, or ancestor)."""
    if not cwd or not repo_root:
        return False
    try:
        repo_n = _norm_path(repo_root)
        if os.path.commonpath([repo_n, _norm_path(cwd)]) == repo_n:
            return True
    except ValueError:
        pass
    if same_workspace_path(cwd, repo_root):
        return True
    current = os.path.abspath(os.path.normpath(cwd))
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent
        if os.path.exists(current) and same_workspace_path(current, repo_root):
            return True


_RUNNING_STATUSES = frozenset({"running", "in_progress", "pending", "started"})


def job_is_running(status: Any) -> bool:
    return str(status or "").strip().lower() in _RUNNING_STATUSES


def job_owned_by_marionette(
    *,
    session_id: str = "",
    label: Any = None,
    tasks: list | None = None,
    job_id: str = "",
    registered_job_ids: Iterable[str] | None = None,
    origin: str = "",
    source: str = "",
    allow_registered_heal: bool = True,
) -> bool:
    """Positive ownership proof for Marionette job surfaces.

    Durable proof is ``origin=marionette`` plus a stamped session id.
    A pre-origin row with a non-empty session id is owned only when it comes
    from the harness/local sidecar (or its id is registered by that session).
    A CLI/sibling-store row still requires ``origin=marionette``.
    Registered-id healing applies only to the active harness/local store —
    never a sibling CLI store, even when ids collide.
    cwd/repo match, running state, and ``app_run_id`` are never ownership.
    """
    jid = str(job_id or "").strip()
    source_norm = (source or "").strip().lower()
    is_cli = source_norm == "cli"
    # Colliding ids in a sibling CLI store must not inherit harness registry.
    allow_heal = bool(allow_registered_heal) and not is_cli
    registered = {str(x).strip() for x in (registered_job_ids or []) if x}
    if allow_heal and jid and jid in registered:
        return True
    task_list = tasks or []
    stamped = parse_job_session_id(label, task_list) or (session_id or "").strip()
    parsed_origin = (origin or parse_job_origin(label, task_list) or "").strip()
    if parsed_origin == "marionette" and stamped:
        return True
    if jid.startswith("local-") and stamped:
        return True
    if stamped and not is_cli:
        return True
    return False


def job_visible_for_view(
    *,
    session_id: str,
    label: Any,
    tasks: list,
    active_session_id: str,
    repo_root: str,
    status: Any = None,
    job_id: str = "",
    registered_job_ids: Iterable[str] | None = None,
    origin: str = "",
    source: str = "",
    allow_registered_heal: bool = True,
) -> bool:
    """Filter rule for /api/jobs and /api/swarm/live.

    Surfaces only Marionette-owned jobs. Repo/all scope is applied later on
    that owned set. ``active_session_id``, ``repo_root``, and ``status`` are
    kept for call-compat and are not admission keys.
    """
    del active_session_id, repo_root, status
    return job_owned_by_marionette(
        session_id=session_id,
        label=label,
        tasks=tasks,
        job_id=job_id,
        registered_job_ids=registered_job_ids,
        origin=origin,
        source=source,
        allow_registered_heal=allow_registered_heal,
    )


def resolve_job_model(raw_arts, raw_tasks, adapter: str = "") -> str:
    """Model badge: FINAL routing decision, else task payload model, else adapter.

    A task may emit ``router`` then ``router-fallback`` (or escalation). Prefer
    the later decision so a failed plan-billed first pick does not badge the
    job as ``cursor/gpt-5-4`` when workers actually ran on agentic GLM.
    """
    try:
        from puppetmaster.models import ArtifactType
    except Exception:
        ArtifactType = None  # type: ignore

    rank = {
        "router-escalation": 3,
        "router-fallback": 2,
        "router": 1,
    }
    best_model = ""
    best_rank = 0
    if ArtifactType is not None:
        for art in raw_arts or []:
            if getattr(art, "type", None) != ArtifactType.ROUTING:
                continue
            created_by = getattr(art, "created_by", "") or ""
            r = rank.get(created_by, 0)
            if r == 0:
                continue
            payload = getattr(art, "payload", None) or {}
            model = payload.get("model_id") or payload.get("model")
            if not model:
                continue
            if r > best_rank:
                best_rank = r
                best_model = str(model)
    if best_model:
        return best_model

    for task in raw_tasks or []:
        payload = getattr(task, "payload", None) or {}
        if isinstance(payload, dict) and payload.get("model"):
            return str(payload["model"])

    return (adapter or "").strip()


def resolve_job_accounting(
    *,
    job: dict,
    label: Any = None,
    tasks: list | None = None,
    active_session_id: str = "",
    registered_job_ids: Iterable[str] | None = None,
    app_run_id: str = "",
    cli_cost_merge: bool | None = None,
) -> dict[str, Any]:
    """Decide whether an admitted job may affect Marionette economic totals.

    Visibility (``job_visible_for_view``) and accounting ownership are split:
    an owned row can still be unbilled (owned-but-unbilled) when CLI cost
    merge is off or the process ``app_run_id`` does not match.
    """
    row = job or {}
    jid = str(row.get("id") or "").strip()
    source = str(row.get("source") or "harness").strip().lower() or "harness"
    label_val = label if label is not None else row.get("label")
    task_list = tasks if tasks is not None else []
    session_id = (
        parse_job_session_id(label_val, task_list)
        or str(row.get("session_id") or "").strip()
    )
    active = (active_session_id or "").strip()
    registered = {str(x).strip() for x in (registered_job_ids or []) if x}
    run_id = (app_run_id or current_app_run_id()).strip()
    merge_cli = cli_cost_merge_enabled() if cli_cost_merge is None else bool(cli_cost_merge)

    owned = False
    scope = ACCOUNTING_SCOPE_VISIBILITY

    if jid and jid in registered:
        owned = True
        scope = ACCOUNTING_SCOPE_MARIONETTE
    elif source == "cli":
        if merge_cli and session_id and session_id == active:
            origin = parse_job_origin(label_val, task_list) or str(row.get("origin") or "")
            if origin == "marionette":
                job_run = parse_job_app_run_id(label_val, task_list) or str(
                    row.get("app_run_id") or row.get("app_instance_id") or ""
                ).strip()
                if run_id and job_run and job_run == run_id:
                    owned = True
                    scope = ACCOUNTING_SCOPE_MARIONETTE
    elif session_id and session_id == active:
        # Legacy session-stamped harness jobs: count only for matching session.
        owned = True
        scope = ACCOUNTING_SCOPE_MARIONETTE

    return {
        "accounting_owned": owned,
        "accounting_scope": scope,
        "source": source,
    }


def annotate_job_accounting(
    job: dict,
    *,
    active_session_id: str = "",
    registered_job_ids: Iterable[str] | None = None,
    app_run_id: str = "",
    tasks: list | None = None,
    cli_cost_merge: bool | None = None,
) -> dict:
    """Return ``job`` copy with accounting_scope / accounting_owned / source."""
    row = dict(job or {})
    acct = resolve_job_accounting(
        job=row,
        label=row.get("label"),
        tasks=tasks,
        active_session_id=active_session_id,
        registered_job_ids=registered_job_ids,
        app_run_id=app_run_id,
        cli_cost_merge=cli_cost_merge,
    )
    row.update(acct)
    cwd = job_repo_cwd(tasks or [])
    if cwd:
        row["cwd"] = cwd
    return row


def annotate_jobs_accounting(
    jobs: list[dict],
    *,
    active_session_id: str = "",
    registered_job_ids: Iterable[str] | None = None,
    app_run_id: str = "",
    tasks_by_job: dict | None = None,
    cli_cost_merge: bool | None = None,
) -> list[dict]:
    """Tag every visible job row with accounting ownership metadata."""
    out: list[dict] = []
    for job in jobs or []:
        jid = job.get("id")
        tasks = (tasks_by_job or {}).get(jid, []) if jid else []
        out.append(
            annotate_job_accounting(
                job,
                active_session_id=active_session_id,
                registered_job_ids=registered_job_ids,
                app_run_id=app_run_id,
                tasks=tasks,
                cli_cost_merge=cli_cost_merge,
            )
        )
    return out


def zero_job_economics(row: dict) -> dict:
    """Strip economic meters from a visibility-only job row (in place safe copy)."""
    out = dict(row or {})
    for key in _JOB_ECONOMIC_FIELDS:
        out[key] = 0
    out["routing_savings_basis"] = "unknown"
    out["routing_savings_counted"] = False
    out["delegation_savings_basis"] = "unknown"
    out["delegation_savings_counted"] = False
    out["cost_provenance"] = out.get("cost_provenance") or "default"
    out["estimated"] = True
    if isinstance(out.get("tasks"), list):
        out["tasks"] = [
            {
                **t,
                "tokens": 0,
                "est_cost_usd": 0,
            }
            if isinstance(t, dict)
            else t
            for t in out["tasks"]
        ]
    return out


def apply_job_economics_policy(row: dict) -> dict:
    """Zero economic fields when ``accounting_owned`` is false."""
    if row.get("accounting_owned"):
        return row
    return zero_job_economics(row)


def filter_accountable_jobs(jobs: list[dict]) -> list[dict]:
    """Keep only rows positively owned by Marionette for usage aggregation."""
    return [j for j in (jobs or []) if j.get("accounting_owned")]


def filter_store_jobs_with_tasks(
    jobs: list[dict],
    store,
    *,
    active_session_id: str,
    repo_root: str,
    registered_job_ids: Iterable[str] | None = None,
    source: str = "harness",
    allow_registered_heal: bool = True,
) -> tuple[list[dict], dict[str, list]]:
    """Return Marionette-owned job rows plus task lists used for ownership."""
    if not jobs:
        return [], {}
    jids = [j.get("id") for j in jobs if j.get("id")]
    tasks_by_job: dict = {}
    labels_by_job: dict = {}
    try:
        for task in store.list_tasks_for_jobs(jids):
            tasks_by_job.setdefault(getattr(task, "job_id", None), []).append(task)
    except Exception:
        for jid in jids:
            try:
                tasks_by_job[jid] = store.list_tasks(jid)
            except Exception:
                tasks_by_job[jid] = []

    try:
        for job in store.list_jobs():
            labels_by_job[job.id] = getattr(job, "label", None)
    except Exception:
        pass

    visible: list[dict] = []
    for job in jobs:
        jid = job.get("id")
        if not jid:
            continue
        label = job.get("label", labels_by_job.get(jid))
        tasks = tasks_by_job.get(jid, [])
        if job_visible_for_view(
            session_id=parse_job_session_id(label, tasks),
            label=label,
            tasks=tasks,
            active_session_id=active_session_id,
            repo_root=repo_root,
            status=job.get("status"),
            job_id=str(jid),
            registered_job_ids=registered_job_ids if allow_registered_heal else None,
            origin=str(job.get("origin") or ""),
            source=str(job.get("source") or source or ""),
            allow_registered_heal=allow_registered_heal,
        ):
            row = dict(job)
            if label is not None:
                row["label"] = label
            stamped_sid = parse_job_session_id(label, tasks)
            if stamped_sid:
                row["session_id"] = stamped_sid
            elif allow_registered_heal and str(jid) in {
                str(x).strip() for x in (registered_job_ids or []) if x
            }:
                heal_sid = (active_session_id or "").strip()
                if not heal_sid:
                    continue
                row["session_id"] = heal_sid
            elif not (row.get("session_id") or "").strip():
                continue
            parsed_origin = parse_job_origin(label, tasks)
            if parsed_origin:
                row["origin"] = parsed_origin
            visible.append(row)
    return visible, tasks_by_job


def filter_store_jobs(
    jobs: list[dict],
    store,
    *,
    active_session_id: str,
    repo_root: str,
    registered_job_ids: Iterable[str] | None = None,
    source: str = "harness",
    allow_registered_heal: bool = True,
) -> list[dict]:
    """Return ``jobs`` rows owned by Marionette."""
    visible, _tasks = filter_store_jobs_with_tasks(
        jobs,
        store,
        active_session_id=active_session_id,
        repo_root=repo_root,
        registered_job_ids=registered_job_ids,
        source=source,
        allow_registered_heal=allow_registered_heal,
    )
    return visible


def filter_local_jobs(
    local_jobs: list[dict],
    *,
    active_session_id: str,
    repo_root: str,
    registered_job_ids: Iterable[str] | None = None,
) -> list[dict]:
    """Apply the same ownership rule to in-process ``local-*`` worker rows.

    Sidecar rows stamped with a session id stay owned (including after reload).
    cwd match and running orphans are never admission.
    """
    visible: list[dict] = []
    for job in local_jobs or []:
        label = job.get("label")
        session_id = job.get("session_id") or parse_job_session_id(label, [])
        if job_visible_for_view(
            session_id=session_id or "",
            label=label,
            tasks=[],
            active_session_id=active_session_id,
            repo_root=repo_root,
            status=job.get("status"),
            job_id=str(job.get("id") or ""),
            registered_job_ids=registered_job_ids,
            origin=str(job.get("origin") or ""),
            source="harness",
            allow_registered_heal=True,
        ):
            row = dict(job)
            if session_id and not (row.get("session_id") or "").strip():
                row["session_id"] = session_id
            if not (row.get("session_id") or "").strip():
                registered = {str(x).strip() for x in (registered_job_ids or []) if x}
                heal_sid = (active_session_id or "").strip()
                if str(row.get("id") or "") in registered and heal_sid:
                    row["session_id"] = heal_sid
                else:
                    continue
            visible.append(row)
    return visible


def inspect_store_job_ownership(
    store,
    job_id: str,
    *,
    source: str = "harness",
    registered_job_ids: Iterable[str] | None = None,
    allow_registered_heal: bool = True,
) -> bool | None:
    """Return True/False when ``job_id`` is in ``store``, else None if absent.

    Used by artifacts/cancel so a known unowned id is refused without leaking
    that it exists. Missing ``list_tasks`` is treated as an empty task list.
    """
    jid = str(job_id or "").strip()
    if store is None or not jid:
        return None
    job = None
    try:
        getter = getattr(store, "get_job", None)
        if callable(getter):
            try:
                job = getter(jid)
            except Exception:
                job = None
        if job is None:
            for row in store.list_jobs() or []:
                rid = row.get("id") if isinstance(row, dict) else getattr(row, "id", None)
                if rid == jid:
                    job = row
                    break
    except Exception:
        return None
    if job is None:
        return None
    label = job.get("label") if isinstance(job, dict) else getattr(job, "label", None)
    tasks: list = []
    try:
        lister = getattr(store, "list_tasks", None)
        if callable(lister):
            tasks = list(lister(jid) or [])
    except Exception:
        tasks = []
    origin = ""
    if isinstance(job, dict):
        origin = str(job.get("origin") or "")
    else:
        origin = str(getattr(job, "origin", "") or "")
    return job_owned_by_marionette(
        session_id=parse_job_session_id(label, tasks),
        label=label,
        tasks=tasks,
        job_id=jid,
        registered_job_ids=registered_job_ids,
        origin=origin,
        source=source,
        allow_registered_heal=allow_registered_heal,
    )
