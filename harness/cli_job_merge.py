from __future__ import annotations

"""Read-only merge of Puppetmaster CLI jobs from the per-project state dir.

CLI runs (``python -m puppetmaster cursor/swarm run``) write to Puppetmaster's
per-workspace project store under ``app_state_root()/projects/<slug>-<hash>/``.
The harness uses its own ``state_dir``; this module resolves the CLI store the
same way the Puppetmaster CLI does and merges visible jobs into job views.
"""

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from harness.diag import note as _diag_note

_merge_failure_logged = False


def reset_merge_diag_for_tests() -> None:
    """Clear the one-shot diagnostics flag (tests only)."""
    global _merge_failure_logged
    _merge_failure_logged = False


def _log_merge_failure(where: str, exc: BaseException | None = None, msg: str = "") -> None:
    global _merge_failure_logged
    if _merge_failure_logged:
        return
    _merge_failure_logged = True
    _diag_note(where, exc, msg)


def _retry_on_locked(read, attempts: int = 3, delay: float = 0.15):
    for attempt in range(attempts):
        try:
            return read()
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() and attempt < attempts - 1:
                time.sleep(delay)
                continue
            raise
    return read()


def resolve_cli_state_dir(workspace_root: str = "") -> Optional[str]:
    """Resolve the Puppetmaster project state dir for ``workspace_root``."""
    try:
        from puppetmaster.state import resolve_state_dir

        cwd = Path(workspace_root or os.getcwd())
        state_path = resolve_state_dir(cwd=cwd)
        if not state_path.is_dir():
            return None
        if not (state_path / "state.sqlite3").is_file():
            return None
        return str(state_path)
    except Exception as exc:
        _log_merge_failure("cli_job_merge.resolve_state_dir", exc)
        return None


def open_cli_durable_state(workspace_root: str = ""):
    """Open the CLI project store read-only-ish (list/read only, no writes)."""
    state_dir = resolve_cli_state_dir(workspace_root)
    if not state_dir:
        return None
    try:
        from harness.state import DurableState

        durable = DurableState(state_dir)
        _retry_on_locked(lambda: durable.store.list_jobs())
        return durable
    except Exception as exc:
        _log_merge_failure("cli_job_merge.open_store", exc)
        return None


# Cross-project live merge bounds. Machines can accrue thousands of stale
# project stores; opening every SQLite file freezes /api/jobs and /api/usage.
_CROSS_PROJECT_MAX_OPENS = 32
_CROSS_PROJECT_MAX_AGE_S = 48 * 3600
_CROSS_PROJECT_WALL_S = 1.5


def open_cli_durable_at(state_dir: str, *, busy_timeout_ms: int = 5000):
    """Open a DurableState for an explicit project state dir.

    ``busy_timeout_ms`` bounds how long we wait on a locked live store. The
    cross-project tracker scan uses a short timeout so one busy MCP/app DB
    cannot freeze the Swarm Tracker (or pytest) for many seconds per project.
    """
    if not state_dir:
        return None
    try:
        from harness.state import DurableState

        durable = DurableState(state_dir)
        store = durable.store
        if hasattr(store, "busy_timeout_ms"):
            store.busy_timeout_ms = int(busy_timeout_ms)
        _retry_on_locked(lambda: store.list_jobs(), attempts=2, delay=0.05)
        return durable
    except Exception as exc:
        _log_merge_failure("cli_job_merge.open_at", exc, msg=state_dir)
        return None


def _foreign_state_dir_candidates(
    primary_resolved: str,
    *,
    max_opens: int = _CROSS_PROJECT_MAX_OPENS,
    max_age_s: float = _CROSS_PROJECT_MAX_AGE_S,
) -> list[str]:
    """Newest-first foreign project dirs worth opening for a live-job scan.

    Prefers ``state.sqlite3`` files touched recently so stale archives (thousands
    of hashes under ``projects/``) never enter the open path.
    """
    try:
        from puppetmaster.state import list_project_state_dirs
    except Exception as exc:
        _log_merge_failure("cli_job_merge.list_projects", exc)
        return []

    now = time.time()
    ranked: list[tuple[float, str]] = []
    for project in list_project_state_dirs():
        try:
            state_dir = str(project.resolve())
        except Exception:
            state_dir = str(project)
        if primary_resolved and state_dir == primary_resolved:
            continue
        db = Path(state_dir) / "state.sqlite3"
        if not db.is_file():
            continue
        try:
            mtime = db.stat().st_mtime
        except OSError:
            continue
        if max_age_s > 0 and (now - mtime) > max_age_s:
            continue
        ranked.append((mtime, state_dir))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [state_dir for _, state_dir in ranked[: max(0, int(max_opens))]]


def merge_running_cli_jobs_all_projects(
    *,
    seen_ids: set,
    primary_state_dir: str = "",
) -> list[dict]:
    """Surface live CLI/MCP jobs from recent sibling PM project stores.

    Cursor MCP and Marionette often resolve different workspace hashes
    (``.marionette`` Home vs ``Projects/marionette``). Without a cross-project
    live merge, the Swarm Tracker goes blank while chat still awaits a swarm.
    Terminal jobs stay workspace-scoped via ``merge_scoped_cli_jobs``.

    Bounded by recent ``state.sqlite3`` mtime, a max open count, and a short
    wall-clock budget so a bloated ``projects/`` tree cannot stall HTTP polls.
    """
    from .job_scoping import job_is_running

    out: list[dict] = []
    primary = (primary_state_dir or "").strip()
    try:
        primary_resolved = str(Path(primary).resolve()) if primary else ""
    except Exception:
        primary_resolved = primary

    deadline = time.monotonic() + _CROSS_PROJECT_WALL_S
    for state_dir in _foreign_state_dir_candidates(primary_resolved):
        if time.monotonic() >= deadline:
            break
        # Short lock wait: live Cursor MCP / Marionette often hold these DBs.
        durable = open_cli_durable_at(state_dir, busy_timeout_ms=400)
        if durable is None:
            continue
        try:
            rows = _retry_on_locked(
                lambda d=durable: d.list_jobs(), attempts=2, delay=0.05
            )
        except Exception:
            continue
        for job in rows or []:
            jid = job.get("id") if isinstance(job, dict) else getattr(job, "id", None)
            if not jid or jid in seen_ids:
                continue
            status = (
                job.get("status")
                if isinstance(job, dict)
                else getattr(job, "status", None)
            )
            if not job_is_running(status):
                continue
            if isinstance(job, dict):
                row = dict(job)
            else:
                row = {
                    "id": jid,
                    "goal": getattr(job, "goal", "") or "",
                    "status": status,
                    "adapter": getattr(job, "adapter", "") or "",
                    "label": getattr(job, "label", None),
                }
            row["source"] = "cli"
            row["cli_state_dir"] = state_dir
            out.append(row)
            seen_ids.add(jid)
    return out


def merge_scoped_cli_jobs(
    harness_jobs: list[dict],
    *,
    harness_store,
    active_session_id: str,
    repo_root: str,
    workspace_root: str,
) -> tuple[list[dict], Any | None]:
    """Return harness jobs plus visible CLI jobs, tagged with ``source``.

    Also merges **running** jobs from other Puppetmaster project stores so a
    Cursor-MCP swarm started under a sibling cwd still appears in the tracker.
    """
    from .job_scoping import filter_store_jobs

    harness_ids = {j.get("id") for j in harness_jobs if j.get("id")}
    merged: list[dict] = []
    for job in harness_jobs:
        row = dict(job)
        row.setdefault("source", "harness")
        merged.append(row)

    seen_ids = set(harness_ids)
    primary_state_dir = resolve_cli_state_dir(workspace_root) or ""
    cli_state = open_cli_durable_state(workspace_root)
    primary_store = None

    if cli_state is not None:
        try:
            cli_rows = _retry_on_locked(lambda: cli_state.list_jobs())
            visible = filter_store_jobs(
                cli_rows,
                cli_state.store,
                active_session_id=active_session_id,
                repo_root=repo_root,
            )
            for job in visible:
                jid = job.get("id")
                if not jid or jid in seen_ids:
                    continue
                row = dict(job)
                row["source"] = "cli"
                if primary_state_dir:
                    row["cli_state_dir"] = primary_state_dir
                merged.append(row)
                seen_ids.add(jid)
            primary_store = cli_state.store
        except Exception as exc:
            _log_merge_failure("cli_job_merge.merge_jobs", exc)

    try:
        for row in merge_running_cli_jobs_all_projects(
            seen_ids=seen_ids,
            primary_state_dir=primary_state_dir,
        ):
            merged.append(row)
    except Exception as exc:
        _log_merge_failure("cli_job_merge.merge_running_all", exc)

    return merged, primary_store


def partition_jobs_by_store(
    jobs: list[dict],
) -> tuple[list[str], list[str]]:
    """Split job ids into harness-store vs CLI-store buckets."""
    harness_ids: list[str] = []
    cli_ids: list[str] = []
    for job in jobs:
        jid = job.get("id")
        if not jid:
            continue
        if job.get("source") == "cli":
            cli_ids.append(jid)
        else:
            harness_ids.append(jid)
    return harness_ids, cli_ids


def cli_stores_by_job(jobs: list[dict]) -> dict[str, Any]:
    """Map CLI job id → store, including foreign ``cli_state_dir`` owners.

    Primary workspace jobs share one store; cross-project live merges stamp
    ``cli_state_dir`` so tracker/usage/cancel can load their artifacts.
    """
    by_dir: dict[str, Any] = {}
    out: dict[str, Any] = {}
    for job in jobs or []:
        if job.get("source") != "cli":
            continue
        jid = job.get("id")
        state_dir = (job.get("cli_state_dir") or "").strip()
        if not jid or not state_dir:
            continue
        if state_dir not in by_dir:
            durable = open_cli_durable_at(state_dir)
            by_dir[state_dir] = getattr(durable, "store", None) if durable else None
        store = by_dir.get(state_dir)
        if store is not None:
            out[str(jid)] = store
    return out


def bulk_load_store_artifacts(store, job_ids: list[str]) -> dict:
    arts_by_job: dict = {}
    if not store or not job_ids:
        return arts_by_job
    try:
        for art in _retry_on_locked(lambda: store.list_artifacts_for_jobs(job_ids)):
            arts_by_job.setdefault(getattr(art, "job_id", None), []).append(art)
    except Exception:
        for jid in job_ids:
            try:
                arts_by_job[jid] = _retry_on_locked(lambda j=jid: store.list_artifacts(j))
            except Exception:
                arts_by_job[jid] = []
    return arts_by_job


def bulk_load_store_tasks(store, job_ids: list[str]) -> dict:
    tasks_by_job: dict = {}
    if not store or not job_ids:
        return tasks_by_job
    try:
        for task in _retry_on_locked(lambda: store.list_tasks_for_jobs(job_ids)):
            tasks_by_job.setdefault(getattr(task, "job_id", None), []).append(task)
    except Exception:
        for jid in job_ids:
            try:
                tasks_by_job[jid] = _retry_on_locked(lambda j=jid: store.list_tasks(j))
            except Exception:
                tasks_by_job[jid] = []
    return tasks_by_job
