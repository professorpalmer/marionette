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
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from harness.command_jobs import (
    COMMAND_TERMINAL_STATES,
    command_fingerprint,
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


def find_command_batch_by_action(
    session: Any,
    action_id: str,
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
        return row
    return None


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
    if job.get("terminal_receipt") is not None:
        out["terminal_receipt"] = job.get("terminal_receipt")
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


def start_command_batch(
    session: Any,
    commands: Sequence[str],
    action_id: str,
    *,
    max_concurrency: Optional[int] = None,
) -> Dict[str, Any]:
    """Register/reuse a durable command batch and return a pending receipt.

    Replay with the same ``action_id`` reuses completed children (never reruns
    them), keeps running children as-is, and deterministically restarts
    failed / cancelled / unstarted children for fingerprints present in
    ``commands``.
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

    existing = find_command_batch_by_action(session, aid)
    if existing is not None:
        return _replay_command_batch(
            session,
            existing,
            normalized,
            cwd=repo,
            max_concurrency=concurrency,
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
                fp,
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
    """Idempotent replay: reuse completed, restart failed/unstarted deterministically."""
    batch_id = str(existing.get("id") or "")
    aid = str(existing.get("action_id") or "")
    register_child = session._register_command_job
    children_meta = [
        dict(c) for c in (existing.get("children") or []) if isinstance(c, dict)
    ]
    by_fp: Dict[str, Dict[str, Any]] = {
        str(c.get("command_fingerprint") or ""): c for c in children_meta
    }
    launch_plan: List[Tuple[str, str]] = []
    child_ids = list(existing.get("child_job_ids") or [])

    for index, command in enumerate(commands):
        fp = command_fingerprint(command)
        preview = secret_free_command_preview(command)
        prior = by_fp.get(fp)
        if prior is not None:
            child_id = str(prior.get("job_id") or "")
            live = lookup_command_job(session, child_id) if child_id else None
            status = str((live or prior).get("status") or "")
            live_receipt = (
                live.get("terminal_receipt")
                if live and live.get("terminal_receipt") is not None
                else prior.get("terminal_receipt")
            )
            # Wave 4: completed children (status or first-wins receipt) must
            # never rerun — SSE/renderer loss must not discard settled work.
            # Intentional replay of failed/cancelled still allocates a *new*
            # child below.
            if status == "completed" or (
                isinstance(live_receipt, dict)
                and str(live_receipt.get("status") or "") == "completed"
            ):
                # Reuse completed — do not rerun.
                prior["status"] = "completed"
                if isinstance(live_receipt, dict):
                    prior["terminal_receipt"] = live_receipt
                if live and live.get("exit_code") is not None:
                    prior["exit_code"] = live.get("exit_code")
                continue
            if status == "running":
                # Deterministic: leave the in-flight child alone.
                prior["status"] = "running"
                continue
            if status == "registered":
                # Already checkpointed/settled rows must not be relaunched.
                if isinstance(live_receipt, dict):
                    prior["status"] = str(live_receipt.get("status") or status)
                    prior["terminal_receipt"] = live_receipt
                    continue
                # Unstarted: launch the existing registered row.
                with _CHILD_COMMAND_LOCK:
                    _CHILD_COMMAND_TEXT[child_id] = command
                launch_plan.append((child_id, command))
                prior["status"] = "registered"
                continue
            # failed / cancelled / timeout / truncated → new child for this fingerprint
            child_short = uuid.uuid4().hex[:8]
            new_id = f"local-cmd-{child_short}"
            register_child(
                new_id,
                command=command,
                action_id=aid,
                command_fingerprint=fp,
                command_preview=preview,
                cwd=cwd,
                batch_id=batch_id,
                batch_index=index,
            )
            with _CHILD_COMMAND_LOCK:
                _CHILD_COMMAND_TEXT[new_id] = command
            prior.update({
                "job_id": new_id,
                "index": index,
                "command_fingerprint": fp,
                "command_preview": preview,
                "status": "registered",
                "terminal_receipt": None,
                "exit_code": None,
                "idempotency_key": batch_idempotency_key(
                    str(getattr(session, "harness_session_id", "") or ""),
                    aid,
                    fp,
                ),
            })
            if child_id in child_ids:
                child_ids = [new_id if x == child_id else x for x in child_ids]
            else:
                child_ids.append(new_id)
            launch_plan.append((new_id, command))
            continue

        # New fingerprint not in prior batch — append a child.
        child_short = uuid.uuid4().hex[:8]
        new_id = f"local-cmd-{child_short}"
        register_child(
            new_id,
            command=command,
            action_id=aid,
            command_fingerprint=fp,
            command_preview=preview,
            cwd=cwd,
            batch_id=batch_id,
            batch_index=index,
        )
        with _CHILD_COMMAND_LOCK:
            _CHILD_COMMAND_TEXT[new_id] = command
        meta = {
            "job_id": new_id,
            "index": index,
            "command_fingerprint": fp,
            "command_preview": preview,
            "status": "registered",
            "idempotency_key": batch_idempotency_key(
                str(getattr(session, "harness_session_id", "") or ""),
                aid,
                fp,
            ),
        }
        children_meta.append(meta)
        child_ids.append(new_id)
        by_fp[fp] = meta
        launch_plan.append((new_id, command))

    update = getattr(session, "_update_command_batch_children", None)
    if callable(update):
        update(
            batch_id,
            children=children_meta,
            child_job_ids=child_ids,
            max_concurrency=max_concurrency,
        )

    with _BATCH_STOP_LOCK:
        stop_ev = _BATCH_STOP_EVENTS.get(batch_id)
        if stop_ev is None:
            stop_ev = threading.Event()
            _BATCH_STOP_EVENTS[batch_id] = stop_ev
        else:
            stop_ev.clear()

    if launch_plan:
        mark = getattr(session, "_mark_command_batch_running", None)
        if callable(mark):
            mark(batch_id)
        _start_batch_supervisor(
            session,
            batch_id,
            launch_plan,
            cwd=cwd,
            max_concurrency=max_concurrency,
        )
    else:
        sync = getattr(session, "_sync_command_batch_from_children", None)
        if callable(sync):
            sync(batch_id)

    refreshed = lookup_command_batch(session, batch_id) or existing
    receipt = build_batch_pending_receipt(refreshed)
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
                launch_registered_command_job(session, job_id, command, cwd)
                # Wait until this child leaves non-terminal states so the
                # semaphore truly bounds concurrent processes.
                deadline = time.time() + 3600.0
                while time.time() < deadline:
                    live = lookup_command_job(session, job_id)
                    if live is None:
                        break
                    if str(live.get("status") or "") in COMMAND_TERMINAL_STATES:
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

    threading.Thread(
        target=_supervise,
        daemon=True,
        name=f"pmh-cmdbatch-{batch_id[-8:]}",
    ).start()
