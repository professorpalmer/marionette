from __future__ import annotations

"""Session- and repo-scoped views over the durable Puppetmaster job store.

Jobs are stamped with ``session_id`` at dispatch (job label + task payload).
Repo membership is derived at read time from each task payload's ``cwd`` via
longest-prefix match against the open workspace root — no store migration.
"""

import json
import os
from typing import Any, Optional


def job_label_for_session(session_id: str) -> Optional[str]:
    """JSON job label carrying the harness session id for dispatch-time stamping."""
    sid = (session_id or "").strip()
    if not sid:
        return None
    return json.dumps({"session_id": sid})


def stamp_task_payload(payload: dict, *, session_id: str = "", cwd: str = "") -> dict:
    """Return a copy of ``payload`` with session/repo fields the filter reads."""
    out = dict(payload or {})
    if cwd:
        out.setdefault("cwd", cwd)
    sid = (session_id or "").strip()
    if sid:
        out["session_id"] = sid
    return out


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


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def job_repo_cwd(tasks: list) -> str:
    """Longest normalized ``cwd`` found on task payloads (deepest wins)."""
    cwds: list[str] = []
    for task in tasks or []:
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
    if stamped:
        return stamped == (active_session_id or "")
    cwd = job_repo_cwd(tasks)
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


def filter_store_jobs(
    jobs: list[dict],
    store,
    *,
    active_session_id: str,
    repo_root: str,
) -> list[dict]:
    """Return ``jobs`` rows visible for the active session + workspace."""
    if not jobs:
        return []
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
            visible.append(row)
    return visible


def filter_local_jobs(local_jobs: list[dict], *, active_session_id: str, repo_root: str) -> list[dict]:
    """Apply the same visibility rule to in-process ``local-*`` worker rows."""
    visible: list[dict] = []
    for job in local_jobs or []:
        label = job.get("label")
        session_id = job.get("session_id") or parse_job_session_id(label, [])
        cwd = (job.get("cwd") or "").strip()
        if session_id:
            if session_id == (active_session_id or ""):
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
