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

# Inline Orchestrator stores created by run_agentic_edit / run_cursor_edit.
# These are never the durable workspace CLI store, even when the process
# temporarily exports PUPPETMASTER_STATE_DIR to the scratch root.
HOST_SCRATCH_MARKER_NAME = ".marionette-host-scratch"
HOST_SCRATCH_DIR_PREFIXES = ("pmh-edit-", "pmh-cursor-edit-")


def is_marionette_host_scratch_dir(path: Any) -> bool:
    """True when ``path`` is a Marionette inline-Orchestrator scratch store.

    Identity is the temp-dir prefix and/or an explicit marker file. Never
    raises — merge hot paths treat an unreadable candidate as "not scratch".
    """
    try:
        if path is None:
            return False
        raw = str(path).strip()
        if not raw:
            return False
        candidate = Path(raw).expanduser()
        name = candidate.name
        if name.startswith(HOST_SCRATCH_DIR_PREFIXES):
            return True
        return (candidate / HOST_SCRATCH_MARKER_NAME).is_file()
    except Exception:
        return False


def mark_marionette_host_scratch(path: Any) -> None:
    """Write the host-scratch marker under ``path``. Best-effort; never raises."""
    try:
        if path is None:
            return
        raw = str(path).strip()
        if not raw:
            return
        root = Path(raw)
        root.mkdir(parents=True, exist_ok=True)
        (root / HOST_SCRATCH_MARKER_NAME).write_text("", encoding="utf-8")
    except Exception:
        pass


def _durable_workspace_state_dir(workspace_root: str = "") -> Optional[Path]:
    """Resolve the durable per-workspace store without honoring process env."""
    from puppetmaster.state import default_state_dir

    cwd = Path(workspace_root or os.getcwd())
    return default_state_dir(cwd)


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


def ensure_workspace_project_store(workspace_root: str = "") -> Optional[str]:
    """Create the durable PM project store for a newly opened workspace.

    ``resolve_cli_state_dir`` requires ``state.sqlite3``. A session that
    relocates into a fresh git repo has no store yet, so Jobs 503s
    ("No Puppetmaster project store"). Mint the sqlite file the same way
    the CLI does on first job so the pane stays live.
    """
    raw = (workspace_root or "").strip()
    if not raw:
        return None
    existing = resolve_cli_state_dir(raw)
    if existing:
        return existing
    try:
        from puppetmaster.state import default_state_dir
        from puppetmaster.store_factory import create_store

        path = default_state_dir(Path(raw).expanduser())
        if path is None:
            return None
        path.mkdir(parents=True, exist_ok=True)
        if is_marionette_host_scratch_dir(path):
            return None
        if not (path / "state.sqlite3").is_file():
            create_store("sqlite", str(path)).ensure_schema()
        if not (path / "state.sqlite3").is_file():
            return None
        return str(path)
    except Exception as exc:
        _log_merge_failure("cli_job_merge.ensure_workspace_project_store", exc)
        return None


def resolve_cli_state_dir(workspace_root: str = "") -> Optional[str]:
    """Resolve the Puppetmaster project state dir for ``workspace_root``.

    A host scratch ``PUPPETMASTER_STATE_DIR`` (inline Orchestrator) is ignored
    so ``/api/swarm/live`` opens the durable workspace store. Process env is
    never mutated.
    """
    try:
        from puppetmaster.state import resolve_state_dir

        cwd = Path(workspace_root or os.getcwd())
        env_value = (os.environ.get("PUPPETMASTER_STATE_DIR") or "").strip()
        if env_value and is_marionette_host_scratch_dir(env_value):
            state_path = _durable_workspace_state_dir(str(cwd))
        else:
            state_path = resolve_state_dir(cwd=cwd)
            if is_marionette_host_scratch_dir(state_path):
                state_path = _durable_workspace_state_dir(str(cwd))
        if state_path is None:
            return None
        if is_marionette_host_scratch_dir(state_path):
            return None
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
    # Same short lock wait as cross-project opens: a parent worker / live app
    # holding state.sqlite3 must not stall /api/usage for multi-second busy
    # timeouts on every StatusBar poll.
    return open_cli_durable_at(state_dir, busy_timeout_ms=400)


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
        if is_marionette_host_scratch_dir(state_dir):
            return None
        from harness.state import DurableState

        durable = DurableState(state_dir)
        store = durable.store
        if hasattr(store, "busy_timeout_ms"):
            store.busy_timeout_ms = int(busy_timeout_ms)
        return durable
    except Exception as exc:
        _log_merge_failure("cli_job_merge.open_at", exc, msg=state_dir)
        return None


def _running_job_rows(durable) -> list[dict]:
    read = getattr(durable, "list_running_jobs", None)
    return list(read() if callable(read) else durable.list_jobs() or [])


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
        if is_marionette_host_scratch_dir(state_dir):
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
    tasks_by_job: dict[tuple, list] | None = None,
) -> list[dict]:
    """Scan recent sibling PM project stores for running jobs.

    ``merge_scoped_cli_jobs`` applies Marionette ownership before any row is
    listed. This helper is the bounded open/scan only — unstamped CLI and
    foreign harness jobs must not appear on Marionette surfaces.

    Tasks used for ownership/session stamps are loaded from the same open
    store that listed the row. Callers pass ``tasks_by_job`` to collect them;
    they stay off the live row.

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
                lambda d=durable: _running_job_rows(d), attempts=2, delay=0.05
            )
        except Exception:
            continue
        store = getattr(durable, "store", None)
        collected: list[dict] = []
        for job in rows or []:
            jid = job.get("id") if isinstance(job, dict) else getattr(job, "id", None)
            if not jid:
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
            row["cross_project"] = True
            key = job_read_key(row, store)
            if key in seen_ids:
                continue
            collected.append(row)
            seen_ids.add(key)
        if tasks_by_job is not None and store is not None and collected:
            jids = [str(row.get("id") or "") for row in collected if row.get("id")]
            try:
                loaded = bulk_load_store_tasks(store, jids)
            except Exception:
                loaded = {}
            for jid in jids:
                tasks = list(loaded.get(jid, []))
                tasks_by_job[job_read_key({"id": jid, "source": "cli", "cli_state_dir": state_dir}, store)] = tasks
        out.extend(collected)
    return out


def cross_project_scan_enabled() -> bool:
    """True when sibling PM project stores may be opened for live merge/actions.

    Hermetic pytest sets ``HARNESS_CLI_CROSS_PROJECT=0`` so cancel/artifacts
    cannot scan the developer's live project tree.
    """
    flag = (os.environ.get("HARNESS_CLI_CROSS_PROJECT") or "1").strip().lower()
    return flag not in ("0", "false", "no", "off")


def merge_scoped_cli_jobs(
    harness_jobs: list[dict],
    *,
    harness_store,
    active_session_id: str,
    repo_root: str,
    workspace_root: str,
    registered_job_ids: list | set | None = None,
) -> tuple[list[dict], Any | None, dict[tuple, list]]:
    """Return harness jobs plus Marionette-owned CLI jobs, tagged with ``source``.

    Cross-project running rows are admitted only when they carry Marionette
    ownership (``origin=marionette`` plus a session stamp). Task payloads are
    loaded before that check so a task-only stamp survives. Registered-id
    healing never applies to CLI/sibling stores — a colliding id cannot admit
    foreign store data. Unstamped CLI / sibling store jobs stay out.

    The third tuple element is ``tasks_by_job`` loaded while filtering CLI rows
    (harness tasks are loaded separately in ``filter_store_jobs_with_tasks``).
    """
    from .job_scoping import filter_store_jobs_with_tasks, job_visible_for_view, parse_job_session_id

    merged: list[dict] = []
    cli_tasks_by_job: dict[tuple, list] = {}
    for job in harness_jobs:
        row = dict(job)
        row.setdefault("source", "harness")
        merged.append(row)

    seen_ids = {job_read_key(j, harness_store) for j in merged}
    primary_state_dir = resolve_cli_state_dir(workspace_root) or ""
    cli_state = open_cli_durable_state(workspace_root)
    primary_store = None

    if cli_state is not None:
        try:
            primary_state_dir = str(getattr(cli_state.store, "root", None) or primary_state_dir)
            cli_rows = _retry_on_locked(lambda: cli_state.list_jobs())
            visible, primary_tasks = filter_store_jobs_with_tasks(
                cli_rows,
                cli_state.store,
                active_session_id=active_session_id,
                repo_root=repo_root,
                registered_job_ids=None,
                source="cli",
                allow_registered_heal=False,
            )
            for job in visible:
                jid = job.get("id")
                if not jid:
                    continue
                row = dict(job)
                row["source"] = "cli"
                if primary_state_dir:
                    row["cli_state_dir"] = primary_state_dir
                key = job_read_key(row, cli_state.store)
                if key in seen_ids:
                    continue
                merged.append(row)
                seen_ids.add(key)
                cli_tasks_by_job[key] = primary_tasks.get(jid, [])
            primary_store = cli_state.store
        except Exception as exc:
            _log_merge_failure("cli_job_merge.merge_jobs", exc)

    # Opt-out for hermetic pytest (conftest sets HARNESS_CLI_CROSS_PROJECT=0)
    # and operators who want workspace-scoped tracker views only.
    if cross_project_scan_enabled():
        try:
            sibling_tasks: dict[tuple, list] = {}
            for row in merge_running_cli_jobs_all_projects(
                seen_ids=seen_ids,
                primary_state_dir=primary_state_dir,
                tasks_by_job=sibling_tasks,
            ):
                jid = str(row.get("id") or "")
                key = job_read_key(row)
                tasks = sibling_tasks.get(key, [])
                cli_tasks_by_job[key] = tasks
                if not job_visible_for_view(
                    session_id=parse_job_session_id(row.get("label"), tasks),
                    label=row.get("label"),
                    tasks=tasks,
                    active_session_id=active_session_id,
                    repo_root=repo_root,
                    status=row.get("status"),
                    job_id=jid,
                    registered_job_ids=None,
                    origin=str(row.get("origin") or ""),
                    source="cli",
                    allow_registered_heal=False,
                ):
                    continue
                stamped = parse_job_session_id(row.get("label"), tasks)
                if stamped:
                    row["session_id"] = stamped
                merged.append(row)
        except Exception as exc:
            _log_merge_failure("cli_job_merge.merge_running_all", exc)

    return merged, primary_store, cli_tasks_by_job


def job_read_key(job: dict, store=None) -> tuple:
    """Canonical store/source/job identity for read joins and deduplication."""
    from puppetmaster.state import state_identity

    source = job.get("source") or "harness"
    root = getattr(store, "root", None) if store is not None else None
    root = root or job.get("cli_state_dir")
    state_id = state_identity(root) if root else (job.get("job_ref") or {}).get("state_id")
    return (state_id, source, str(job.get("id") or ""))


def job_stores_for_read(jobs: list[dict], harness_store, cli_store) -> dict:
    """Resolve each row independently; an explicit unavailable CLI root stays unavailable."""
    by_dir: dict = {}
    out: dict = {}
    for job in jobs:
        if job.get("source") != "cli":
            store = harness_store
        elif job.get("cli_state_dir"):
            key = job_read_key(job)[0]
            if key not in by_dir:
                if cli_store is not None and job_read_key(job, cli_store)[0] == key:
                    by_dir[key] = cli_store
                else:
                    durable = open_cli_durable_at(job["cli_state_dir"])
                    by_dir[key] = getattr(durable, "store", None)
            store = by_dir[key]
        else:
            store = cli_store
        out[job_read_key(job, store)] = store
    return out


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


def bulk_load_store_artifacts(store, job_ids: list[str], *, unavailable: set | None = None) -> dict:
    arts_by_job: dict = {}
    if not store or not job_ids:
        if store is None and unavailable is not None:
            unavailable.update(job_ids)
        return arts_by_job
    try:
        for art in _retry_on_locked(lambda: store.list_artifacts_for_jobs(job_ids)):
            arts_by_job.setdefault(getattr(art, "job_id", None), []).append(art)
    except Exception:
        arts_by_job.clear()
        for jid in job_ids:
            try:
                arts_by_job[jid] = _retry_on_locked(lambda j=jid: store.list_artifacts(j))
            except Exception:
                if unavailable is not None:
                    unavailable.add(jid)
                arts_by_job[jid] = []
    return arts_by_job


def bulk_load_store_tasks(store, job_ids: list[str], *, unavailable: set | None = None) -> dict:
    tasks_by_job: dict = {}
    if not store or not job_ids:
        if store is None and unavailable is not None:
            unavailable.update(job_ids)
        return tasks_by_job
    try:
        for task in _retry_on_locked(lambda: store.list_tasks_for_jobs(job_ids)):
            tasks_by_job.setdefault(getattr(task, "job_id", None), []).append(task)
    except Exception:
        tasks_by_job.clear()
        for jid in job_ids:
            try:
                tasks_by_job[jid] = _retry_on_locked(lambda j=jid: store.list_tasks(j))
            except Exception:
                if unavailable is not None:
                    unavailable.add(jid)
                tasks_by_job[jid] = []
    return tasks_by_job
