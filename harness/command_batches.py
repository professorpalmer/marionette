"""Opt-in durable command-batch supervisor (Wave 3).

One aggregate local-job row references one durable Wave 2 child command job
per command. Each child owns its own truth and terminal receipt; the aggregate
never becomes the source of truth for child stdout or exit codes.

Explicit / opt-in only — never inferred from ``run_parallel``, duration, or
command text. Provider-swarm ``run_parallel`` semantics are unchanged; batch
rows use role/adapter ``command_batch`` so projections cannot misclassify them
as provider workers.
"""
from __future__ import annotations

import threading
from functools import wraps
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from harness.command_jobs import (
    COMMAND_TERMINAL_STATES,
    command_fingerprint,
    command_job_outcome,
    launch_registered_command_job,
    lookup_command_job,
    secret_free_command_preview,
)
from harness.job_scoping import ACCOUNTING_SCOPE_MARIONETTE

# Validation batch size gate from the chat-loop resilience plan.
MAX_COMMAND_BATCH_SIZE = 6

COMMAND_BATCH_KIND = "run_command_batch"
COMMAND_BATCH_ROLE = "command_batch"
COMMAND_BATCH_ADAPTER = "command_batch"

# In-memory command text for children that still need to launch. Never projected
# and never written into the durable job row (secrets stay off disk).
_CHILD_COMMAND_TEXT: Dict[str, str] = {}
_CHILD_COMMAND_LOCK = threading.Lock()

# Aggregate → stop flag (stop-before-start for children not yet launched).
_BATCH_STOP_EVENTS: Dict[str, threading.Event] = {}
_BATCH_STOP_LOCK = threading.Lock()


# Serialize registration of a logical action inside the owning backend process.
_BATCH_ACTION_LOCK = threading.RLock()
_BATCH_SUPERVISORS: set = set()
_BATCH_SUPERVISOR_LOCK = threading.Lock()


def _serialize_batch_action(fn):
    @wraps(fn)
    def locked(*args, **kwargs):
        with _BATCH_ACTION_LOCK:
            return fn(*args, **kwargs)
    return locked


def is_command_batch_action(act: Any) -> bool:
    """True only for the explicit ``run_command_batch`` pilot verb."""
    if act is None:
        return False
    return str(getattr(act, "kind", "") or "").strip() == COMMAND_BATCH_KIND


def normalize_batch_commands(commands: Any) -> List[str]:
    """Coerce a commands payload into 1..MAX non-empty UTF-8 command strings."""
    if commands is None:
        return []
    if isinstance(commands, str):
        raw_list: List[Any] = [commands]
    elif isinstance(commands, (list, tuple)):
        raw_list = list(commands)
    else:
        raise ValueError("commands must be a list of command strings")
    out: List[str] = []
    for item in raw_list:
        text = str(item or "").strip()
        if text:
            out.append(text)
    if not out:
        raise ValueError("run_command_batch requires a non-empty commands list")
    if len(out) > MAX_COMMAND_BATCH_SIZE:
        raise ValueError(
            f"run_command_batch supports at most {MAX_COMMAND_BATCH_SIZE} commands "
            f"(got {len(out)})"
        )
    return out


def batch_idempotency_key(
    session_id: str,
    action_id: str,
    fingerprint: str,
) -> str:
    """Stable key: session_id + action_id + command fingerprint."""
    return f"{session_id}\0{action_id}\0{fingerprint}"


def lookup_command_batch(session: Any, batch_id: str) -> Optional[Dict[str, Any]]:
    """Restart-safe lookup of an aggregate command-batch job."""
    getter = getattr(session, "get_local_job", None)
    if callable(getter):
        job = getter(batch_id)
    else:
        jobs = getattr(session, "_local_jobs", None) or {}
        lock = getattr(session, "_local_jobs_lock", None)
        if lock is not None:
            with lock:
                job = dict(jobs.get(batch_id) or {}) if batch_id in jobs else None
        else:
            job = dict(jobs.get(batch_id) or {}) if batch_id in jobs else None
    if not isinstance(job, dict) or not job:
        return None
    if str(job.get("job_kind") or "") != COMMAND_BATCH_KIND:
        if str(job.get("role") or "") != COMMAND_BATCH_ROLE:
            return None
    return job


def _batch_child_fingerprints(row: Dict[str, Any]) -> List[str]:
    children = row.get("children") or []
    ordered = [
        child for child in children
        if isinstance(child, dict)
    ]
    ordered.sort(key=lambda child: int(child.get("index") or 0))
    return [str(child.get("command_fingerprint") or "") for child in ordered]


def find_command_batch_by_action(
    session: Any,
    action_id: str,
    fingerprints: Optional[Sequence[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Find the aggregate batch registered for ``action_id`` in this session."""
    aid = str(action_id or "").strip()
    if not aid:
        return None
    session_id = str(getattr(session, "harness_session_id", "") or "")
    live = getattr(session, "live_local_jobs", None)
    if callable(live):
        rows = live()
    else:
        lock = getattr(session, "_local_jobs_lock", None)
        jobs = getattr(session, "_local_jobs", None) or {}
        if lock is not None:
            with lock:
                rows = [dict(j) for j in jobs.values()]
        else:
            rows = [dict(j) for j in jobs.values()]
    found = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("job_kind") or "") != COMMAND_BATCH_KIND and str(
            row.get("role") or ""
        ) != COMMAND_BATCH_ROLE:
            continue
        if str(row.get("action_id") or "") != aid:
            continue
        if session_id and str(row.get("session_id") or "") not in ("", session_id):
            continue
        found.append(row)
    if not found:
        return None
    if fingerprints is not None:
        want = [str(x) for x in fingerprints]
        exact = [row for row in found if _batch_child_fingerprints(row) == want]
        if exact:
            found = exact
    found.sort(key=lambda row: float(row.get("created_at") or 0), reverse=True)
    return found[0]


def project_command_batch_fields(job: Dict[str, Any]) -> Dict[str, Any]:
    """Secret-free aggregate/child fields for API / swarm-live projection."""
    if not isinstance(job, dict):
        return {}
    if str(job.get("job_kind") or "") != COMMAND_BATCH_KIND and str(
        job.get("role") or ""
    ) != COMMAND_BATCH_ROLE:
        return {}
    children = job.get("children") if isinstance(job.get("children"), list) else []
    projected_children: List[Dict[str, Any]] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        entry = {
            "job_id": str(child.get("job_id") or ""),
            "index": int(child.get("index") or 0),
            "command_fingerprint": str(child.get("command_fingerprint") or ""),
            "command_preview": str(child.get("command_preview") or ""),
            "status": str(child.get("status") or ""),
        }
        for key in ("recovery_state", "recovery_receipt"):
            if key in child:
                entry[key] = child[key]
        if child.get("terminal_receipt") is not None:
            entry["terminal_receipt"] = child.get("terminal_receipt")
        if child.get("exit_code") is not None:
            entry["exit_code"] = child.get("exit_code")
        # Never project raw command text.
        projected_children.append(entry)
    out: Dict[str, Any] = {
        "job_kind": COMMAND_BATCH_KIND,
        "action_id": str(job.get("action_id") or ""),
        "child_job_ids": list(job.get("child_job_ids") or []),
        "children": projected_children,
        "child_count": int(job.get("child_count") or len(projected_children)),
        "max_concurrency": int(job.get("max_concurrency") or 0),
        "started_at": job.get("started_at") or job.get("created_at"),
        "mixed_terminal": bool(job.get("mixed_terminal")),
    }
    for key in ("terminal_receipt", "recovery_state", "recovery_receipt"):
        if key in job:
            out[key] = job[key]
    return out


def build_batch_pending_receipt(job: Dict[str, Any]) -> Dict[str, Any]:
    """Pending/terminal receipt for a command-batch aggregate."""
    fields = project_command_batch_fields(job)
    status = str(job.get("status") or "registered")
    receipt: Dict[str, Any] = {
        "job_id": str(job.get("id") or ""),
        "batch_id": str(job.get("id") or ""),
        "session_id": str(job.get("session_id") or ""),
        "action_id": str(job.get("action_id") or ""),
        "status": "pending" if status in ("registered", "running") else status,
        "kind": COMMAND_BATCH_KIND,
        "job_kind": COMMAND_BATCH_KIND,
        "role": COMMAND_BATCH_ROLE,
        "adapter": COMMAND_BATCH_ADAPTER,
        "source": str(job.get("source") or "harness"),
        "accounting_owned": bool(job.get("accounting_owned", True)),
        "accounting_scope": str(
            job.get("accounting_scope") or ACCOUNTING_SCOPE_MARIONETTE
        ),
        "cwd": str(job.get("cwd") or ""),
        "started_at": job.get("started_at") or job.get("created_at"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "child_job_ids": fields.get("child_job_ids") or [],
        "children": fields.get("children") or [],
        "child_count": fields.get("child_count") or 0,
        "max_concurrency": fields.get("max_concurrency") or 0,
        "mixed_terminal": bool(fields.get("mixed_terminal")),
        "terminal_receipt": job.get("terminal_receipt"),
        "message": (
            f"Command batch registered as job {job.get('id')}; "
            "each child owns its own terminal receipt."
        ),
    }
    for key in ("recovery_state", "recovery_receipt"):
        if key in job:
            receipt[key] = job[key]
    return receipt


def resolve_batch_max_concurrency(session: Any, command_count: int) -> int:
    """Bound concurrency by config.max_workers and batch size (never swarm pool)."""
    n = max(1, int(command_count))
    cfg = getattr(session, "config", None)
    max_workers = 4
    if cfg is not None:
        try:
            max_workers = max(1, int(getattr(cfg, "max_workers", 4) or 4))
        except (TypeError, ValueError):
            max_workers = 4
    return max(1, min(n, max_workers, MAX_COMMAND_BATCH_SIZE))


@_serialize_batch_action
def start_command_batch(
    session: Any,
    commands: Sequence[str],
    action_id: str,
    *,
    max_concurrency: Optional[int] = None,
) -> Dict[str, Any]:
    """Register/reuse a durable command batch and return a pending receipt.

    Replay with the same ``action_id`` preserves every observed outcome. Only
    registered children without a launch checkpoint may start. Commands and
    cwd are immutable for an action; intentional retries need a new action ID.
    """
    normalized = normalize_batch_commands(commands)
    aid = str(action_id or "").strip()
    if not aid:
        raise ValueError("run_command_batch requires a non-empty action_id")
    repo = str(getattr(getattr(session, "config", None), "repo", "") or "").strip()
    if not repo:
        raise ValueError("No workspace directory (config.repo) is open.")

    register_batch = getattr(session, "_register_command_batch_job", None)
    register_child = getattr(session, "_register_command_job", None)
    if not callable(register_batch) or not callable(register_child):
        raise RuntimeError("session does not support command-batch registration")

    concurrency = int(max_concurrency) if max_concurrency is not None else 0
    if concurrency <= 0:
        concurrency = resolve_batch_max_concurrency(session, len(normalized))
    else:
        concurrency = max(1, min(concurrency, len(normalized), MAX_COMMAND_BATCH_SIZE))

    expected_fps = [command_fingerprint(command) for command in normalized]
    existing = find_command_batch_by_action(session, aid, fingerprints=expected_fps)
    if existing is None:
        existing = find_command_batch_by_action(session, aid)
    if existing is not None:
        try:
            return _replay_command_batch(
                session,
                existing,
                normalized,
                cwd=repo,
                max_concurrency=concurrency,
            )
        except ValueError:
            # Providers reuse run_command_batch:0 after a finished batch.
            # A live/unknown row still refuses mutation.
            if str(existing.get("status") or "") not in COMMAND_TERMINAL_STATES:
                raise


    # Resource-pressure admit once per logical batch (optional host hook).
    admit = getattr(session, "_resource_pressure_admit", None)
    if callable(admit):
        allowed = admit(
            admission_group=f"command-batch:{aid}",
            admission_size=concurrency,
        )
        if not allowed:
            raise RuntimeError(
                getattr(session, "_resource_pressure_capacity_message", lambda: "")()
                or "Resource capacity constrained; not dispatching command batch."
            )

    short = uuid.uuid4().hex[:8]
    batch_id = f"local-cmdbatch-{short}"
    children_meta: List[Dict[str, Any]] = []
    child_ids: List[str] = []
    launch_plan: List[Tuple[str, str]] = []  # (job_id, command)

    for index, command in enumerate(normalized):
        fp = command_fingerprint(command)
        preview = secret_free_command_preview(command)
        child_short = uuid.uuid4().hex[:8]
        child_id = f"local-cmd-{child_short}"
        child_row = register_child(
            child_id,
            command=command,
            action_id=aid,
            command_fingerprint=fp,
            command_preview=preview,
            cwd=repo,
            batch_id=batch_id,
            batch_index=index,
        )
        with _CHILD_COMMAND_LOCK:
            _CHILD_COMMAND_TEXT[child_id] = command
        child_ids.append(child_id)
        children_meta.append({
            "job_id": child_id,
            "index": index,
            "command_fingerprint": fp,
            "command_preview": preview,
            "status": str(child_row.get("status") or "registered"),
            "idempotency_key": batch_idempotency_key(
                str(getattr(session, "harness_session_id", "") or ""),
                aid,
                f"{index}:{fp}",
            ),
        })
        launch_plan.append((child_id, command))

    batch = register_batch(
        batch_id,
        action_id=aid,
        children=children_meta,
        child_job_ids=child_ids,
        cwd=repo,
        max_concurrency=concurrency,
    )
    with _BATCH_STOP_LOCK:
        _BATCH_STOP_EVENTS[batch_id] = threading.Event()

    _start_batch_supervisor(
        session,
        batch_id,
        launch_plan,
        cwd=repo,
        max_concurrency=concurrency,
    )
    refreshed = lookup_command_batch(session, batch_id) or batch
    return build_batch_pending_receipt(refreshed)


def cancel_command_batch(session: Any, batch_id: str) -> bool:
    """Cancel a batch: stop-before-start for unstarted; coop-cancel active.

    Completed siblings are never discarded or rewritten.
    """
    batch = lookup_command_batch(session, batch_id)
    if batch is None:
        return False
    with _BATCH_STOP_LOCK:
        ev = _BATCH_STOP_EVENTS.get(batch_id)
        if ev is None:
            ev = threading.Event()
            _BATCH_STOP_EVENTS[batch_id] = ev
        ev.set()

    cancel_child = getattr(session, "cancel_local_job", None)
    changed = False
    for child_id in list(batch.get("child_job_ids") or []):
        child = lookup_command_job(session, str(child_id))
        if child is None:
            continue
        status = str(child.get("status") or "")
        if status in COMMAND_TERMINAL_STATES:
            # Preserve completed/failed/cancelled siblings as-is.
            continue
        has_launch = isinstance(child.get("launch_checkpoint"), dict)
        if status == "registered" and not has_launch:
            # Stop-before-start: honest cancelled terminal without process launch.
            finish = getattr(session, "_finish_command_job", None)
            if callable(finish):
                finish(
                    str(child_id),
                    status="cancelled",
                    summary="Cancelled before start (batch stop)",
                    exit_code=-1,
                    output="",
                )
                changed = True
            continue
        # Checkpointed / running children: cooperative cancel so the worker
        # can persist partial stdout in the single terminal receipt.
        if callable(cancel_child):
            if cancel_child(str(child_id)):
                changed = True
    sync = getattr(session, "_sync_command_batch_from_children", None)
    if callable(sync):
        sync(batch_id, parent_cancelled=True)
    return changed or True


def _replay_command_batch(
    session: Any,
    existing: Dict[str, Any],
    commands: List[str],
    *,
    cwd: str,
    max_concurrency: int,
) -> Dict[str, Any]:
    """Reconcile each original occurrence; never retry an uncertain effect."""
    batch_id = str(existing.get("id") or "")
    children = existing.get("children") or []
    expected = [command_fingerprint(command) for command in commands]
    if (
        str(existing.get("cwd") or "") != cwd
        or len(children) != len(expected)
        or any(
            not isinstance(child, dict)
            or child.get("index") != index
            or child.get("command_fingerprint") != expected[index]
            for index, child in enumerate(children)
        )
    ):
        raise ValueError("Command batch action identity conflict: commands or cwd changed")

    launch_plan = []
    reconciled = []
    for prior, command in zip(children, commands):
        child_id = str(prior.get("job_id") or "")
        live = lookup_command_job(session, child_id)
        child = dict(prior)
        if live is not None:
            if (
                live.get("action_id") != existing.get("action_id")
                or live.get("batch_id") != batch_id
                or live.get("batch_index") != prior.get("index")
                or live.get("command_fingerprint") != prior.get("command_fingerprint")
                or live.get("cwd") != cwd
            ):
                raise ValueError("Command batch child identity conflict")
            if (
                live.get("status") == "registered"
                and live.get("launch_checkpoint") is None
                and live.get("terminal_receipt") is None
            ):
                launch_plan.append((child_id, command))
            child.update(command_job_outcome(live))
            if child["status"] == "unknown":
                child.pop("exit_code", None)
        elif not isinstance(prior.get("terminal_receipt"), dict):
            child["status"] = "unknown"
            child["recovery_state"] = "unknown"
        reconciled.append(child)

    # Never clear a prior Stop request or rewrite settled journal receipts.
    if launch_plan:
        _start_batch_supervisor(
            session, batch_id, launch_plan, cwd=cwd,
            max_concurrency=int(existing.get("max_concurrency") or max_concurrency),
        )
    sync = getattr(session, "_sync_command_batch_from_children", None)
    if callable(sync):
        sync(batch_id)
    view = dict(lookup_command_batch(session, batch_id) or existing)
    view["children"] = reconciled
    receipt = build_batch_pending_receipt(view)
    if any(child.get("status") == "unknown" for child in reconciled):
        receipt["status"] = "unknown"
        receipt["recovery_state"] = "unknown"
        receipt["recovery_receipt"] = view.get("recovery_receipt") or receipt.pop("terminal_receipt", None)
        receipt["terminal_receipt"] = None
        receipt["message"] = "Command outcome unknown; replay did not repeat checkpointed work."
    receipt["replayed"] = True
    return receipt


def _start_batch_supervisor(
    session: Any,
    batch_id: str,
    launch_plan: List[Tuple[str, str]],
    *,
    cwd: str,
    max_concurrency: int,
) -> None:
    """Daemon supervisor: bounded concurrency, stop-before-start, per-child cancel."""
    owner = (id(session), batch_id)
    with _BATCH_SUPERVISOR_LOCK:
        if owner in _BATCH_SUPERVISORS:
            return
        _BATCH_SUPERVISORS.add(owner)
    mark = getattr(session, "_mark_command_batch_running", None)
    if callable(mark):
        mark(batch_id)

    def _supervise() -> None:
        sem = threading.Semaphore(max(1, int(max_concurrency)))
        workers: List[threading.Thread] = []

        def _run_one(job_id: str, command: str) -> None:
            try:
                with _BATCH_STOP_LOCK:
                    stop_ev = _BATCH_STOP_EVENTS.get(batch_id)
                if stop_ev is not None and stop_ev.is_set():
                    # Stop-before-start: child stays cancelled if still registered.
                    child = lookup_command_job(session, job_id)
                    if child and str(child.get("status") or "") == "registered":
                        finish = getattr(session, "_finish_command_job", None)
                        if callable(finish):
                            finish(
                                job_id,
                                status="cancelled",
                                summary="Cancelled before start (batch stop)",
                                exit_code=-1,
                                output="",
                            )
                    return
                # Another path may have cancelled the registered row already.
                child = lookup_command_job(session, job_id)
                if child and str(child.get("status") or "") in COMMAND_TERMINAL_STATES:
                    return
                if not launch_registered_command_job(session, job_id, command, cwd):
                    return
                # Wait until this child leaves non-terminal states so the
                # semaphore truly bounds concurrent processes.
                while True:
                    live = lookup_command_job(session, job_id)
                    if live is None:
                        break
                    if str(live.get("status") or "") in COMMAND_TERMINAL_STATES or live.get("status") == "unknown":
                        break
                    if str(live.get("status") or "") == "registered":
                        # Thread may not have flipped to running yet.
                        time.sleep(0.01)
                        continue
                    time.sleep(0.02)
            finally:
                sem.release()
                sync = getattr(session, "_sync_command_batch_from_children", None)
                if callable(sync):
                    sync(batch_id)

        for job_id, command in launch_plan:
            with _BATCH_STOP_LOCK:
                stop_ev = _BATCH_STOP_EVENTS.get(batch_id)
            if stop_ev is not None and stop_ev.is_set():
                child = lookup_command_job(session, job_id)
                if child and str(child.get("status") or "") == "registered":
                    finish = getattr(session, "_finish_command_job", None)
                    if callable(finish):
                        finish(
                            job_id,
                            status="cancelled",
                            summary="Cancelled before start (batch stop)",
                            exit_code=-1,
                            output="",
                        )
                continue
            sem.acquire()
            t = threading.Thread(
                target=_run_one,
                args=(job_id, command),
                daemon=True,
                name=f"pmh-cmdbatch-child-{job_id[-8:]}",
            )
            workers.append(t)
            t.start()

        for t in workers:
            t.join()
        sync = getattr(session, "_sync_command_batch_from_children", None)
        if callable(sync):
            sync(batch_id)
        with _CHILD_COMMAND_LOCK:
            for job_id, _cmd in launch_plan:
                _CHILD_COMMAND_TEXT.pop(job_id, None)

    def _owned_supervise() -> None:
        try:
            _supervise()
        finally:
            with _BATCH_SUPERVISOR_LOCK:
                _BATCH_SUPERVISORS.discard(owner)

    threading.Thread(
        target=_owned_supervise,
        daemon=True,
        name=f"pmh-cmdbatch-{batch_id[-8:]}",
    ).start()
