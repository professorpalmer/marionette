from __future__ import annotations

"""Session- and repo-scoped views over the durable Puppetmaster job store.

Jobs are stamped with ``session_id`` at dispatch (job label + task payload).
Repo membership is derived at read time from each task payload's ``cwd`` via
longest-prefix match against the open workspace root — no store migration.
"""

import json
import os
from typing import Any, Iterable, Optional

ACCOUNTING_SCOPE_MARIONETTE = "marionette"
ACCOUNTING_SCOPE_VISIBILITY = "visibility_only"

# Economic fields zeroed on visibility-only rows (tracker still shows the job).
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
    """True when ``cwd`` sits under ``repo_root`` (longest-prefix / commonpath)."""
    if not cwd or not repo_root:
        return False
    try:
        return os.path.commonpath([_norm_path(repo_root), _norm_path(cwd)]) == _norm_path(repo_root)
    except ValueError:
        return False


_RUNNING_STATUSES = frozenset({"running", "in_progress", "pending", "started"})


def job_is_running(status: Any) -> bool:
    return str(status or "").strip().lower() in _RUNNING_STATUSES


def job_visible_for_view(
    *,
    session_id: str,
    label: Any,
    tasks: list,
    active_session_id: str,
    repo_root: str,
    status: Any = None,
) -> bool:
    """Filter rule for /api/jobs and /api/swarm/live.

    Visible when the stamped session matches the active session, OR the job is
    an unstamped legacy row whose task cwd lies under the current workspace.
    Running jobs use those same rules (they must not leak across open
    directories). Narrow escape: a running job with no session stamp and no
    cwd stays visible so true orphans remain cancellable.
    """
    stamped = parse_job_session_id(label, tasks) or (session_id or "").strip()
    cwd = job_repo_cwd(tasks)
    if stamped:
        if stamped == (active_session_id or ""):
            return True
        # Match filter_local_jobs: a running job under the open workspace stays
        # visible across session switches so the tracker cannot go blank while
        # chat still says a swarm is running (CLI + harness).
        return bool(
            job_is_running(status)
            and repo_root
            and cwd
            and cwd_under_repo(cwd, repo_root)
        )
    if not cwd:
        # Unstamped + no cwd: only running orphans stay visible (cancellable).
        return job_is_running(status)
    if not repo_root:
        return False
    return cwd_under_repo(cwd, repo_root)


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
    """Decide whether a visible job may affect Marionette economic totals.

    Visibility (``job_visible_for_view``) and accounting ownership are split:
    foreign/unstamped CLI jobs stay on the tracker but fail closed for money.
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
) -> tuple[list[dict], dict[str, list]]:
    """Return visible job rows plus task lists loaded for visibility filtering."""
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
        ):
            row = dict(job)
            if label is not None:
                row["label"] = label
            stamped_sid = parse_job_session_id(label, tasks)
            if stamped_sid:
                row["session_id"] = stamped_sid
            visible.append(row)
    return visible, tasks_by_job


def filter_store_jobs(
    jobs: list[dict],
    store,
    *,
    active_session_id: str,
    repo_root: str,
) -> list[dict]:
    """Return ``jobs`` rows visible for the active session + workspace."""
    visible, _tasks = filter_store_jobs_with_tasks(
        jobs,
        store,
        active_session_id=active_session_id,
        repo_root=repo_root,
    )
    return visible


def filter_local_jobs(local_jobs: list[dict], *, active_session_id: str, repo_root: str) -> list[dict]:
    """Apply the same visibility rule to in-process ``local-*`` worker rows.

    Running locals whose cwd sits under the open workspace stay visible even
    when the session stamp drifted (interrupt / mid-flight session switch) --
    otherwise the chat can say "swarm running" while the tracker looks empty.
    Terminal jobs still require an exact session match (or legacy cwd match
    when unstamped).
    """
    visible: list[dict] = []
    for job in local_jobs or []:
        label = job.get("label")
        session_id = job.get("session_id") or parse_job_session_id(label, [])
        cwd = (job.get("cwd") or "").strip()
        if session_id:
            if session_id == (active_session_id or ""):
                visible.append(job)
                continue
            if (
                job_is_running(job.get("status"))
                and repo_root
                and cwd
                and cwd_under_repo(cwd, repo_root)
            ):
                visible.append(job)
            continue
        if not cwd:
            # Unstamped + no cwd: only running orphans stay visible (cancellable).
            if job_is_running(job.get("status")):
                visible.append(job)
            continue
        if repo_root and cwd_under_repo(cwd, repo_root):
            visible.append(job)
    return visible
