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


def merge_scoped_cli_jobs(
    harness_jobs: list[dict],
    *,
    harness_store,
    active_session_id: str,
    repo_root: str,
    workspace_root: str,
) -> tuple[list[dict], Any | None]:
    """Return harness jobs plus visible CLI jobs, tagged with ``source``."""
    from .job_scoping import filter_store_jobs

    harness_ids = {j.get("id") for j in harness_jobs if j.get("id")}
    merged: list[dict] = []
    for job in harness_jobs:
        row = dict(job)
        row.setdefault("source", "harness")
        merged.append(row)

    cli_state = open_cli_durable_state(workspace_root)
    if cli_state is None:
        return merged, None

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
            if not jid or jid in harness_ids:
                continue
            row = dict(job)
            row["source"] = "cli"
            merged.append(row)
    except Exception as exc:
        _log_merge_failure("cli_job_merge.merge_jobs", exc)
        return merged, None

    return merged, cli_state.store


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
