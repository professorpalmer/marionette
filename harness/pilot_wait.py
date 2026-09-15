from __future__ import annotations

"""In-turn wait / keep-alive for background jobs.

Cursor-style Await: the send loop stays on the live turn, sleeps in short
slices, emits wait notices so SSE/chrome stay alive, and folds finished
worker results into the same turn. The FE keep-alive resume path remains
the fallback when this turn already closed.
"""

import time
from typing import Any, Callable, Iterator

WAIT_DEFAULT_SEC = 2.0
WAIT_MAX_SEC = 30.0
WAIT_SLICE_SEC = 0.25
WAIT_KEEPALIVE_SEC = 2.0
WAIT_KEEPALIVE_CAP = 90


def parse_wait_seconds(raw: Any, default: float = WAIT_DEFAULT_SEC) -> float:
    """Clamp a wait duration. Invalid values fall back to ``default``."""
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = float(default)
    if seconds != seconds:  # NaN
        seconds = float(default)
    return max(0.1, min(WAIT_MAX_SEC, seconds))


def _cancel_requested(session: Any) -> bool:
    cancel = getattr(session, "_cancel", None)
    if cancel is None:
        return False
    is_set = getattr(cancel, "is_set", None)
    if not callable(is_set):
        return False
    try:
        return bool(is_set())
    except Exception:
        return False


def _target_command_wait_state(session: Any, job_id: str) -> str:
    """Return pending, settled, unavailable, or empty for a non-command id."""
    job_id = str(job_id or "").strip()
    if not job_id.startswith("local-cmd-"):
        return ""
    try:
        from .command_jobs import (
            COMMAND_TERMINAL_STATES,
            command_job_outcome,
            lookup_command_job,
        )
        job = lookup_command_job(session, job_id)
        if job is None:
            return "unavailable"
        status = str(command_job_outcome(job).get("status") or "").strip().lower()
        if status in COMMAND_TERMINAL_STATES or status == "unknown":
            return "settled"
        return "pending"
    except Exception:
        return "unavailable"


def pending_jobs_keep_alive(session: Any, *, job_id: str = "") -> bool:
    """True when the requested or any session-owned background work is live.

    Queued distill/wiki leftovers must not keep the turn open — those drain
    after finalize. In-flight swarms and command jobs keep the pilot on Await.
    """
    if _cancel_requested(session):
        return False
    target_state = _target_command_wait_state(session, job_id)
    if target_state:
        return target_state == "pending"
    for name in ("has_pending_swarms", "has_pending_command_jobs"):
        has_pending = getattr(session, name, None)
        if not callable(has_pending):
            continue
        try:
            if has_pending():
                return True
        except Exception:
            continue
    return False


def format_wait_status(
    session: Any,
    seconds: float,
    settled: bool,
    *,
    job_id: str = "",
) -> str:
    """Compact tool-result text the pilot can act on."""
    target_state = _target_command_wait_state(session, job_id)
    if target_state == "unavailable":
        return (
            f"(wait {seconds:g}s) Background command {job_id} status is "
            "unavailable; do not assume it settled."
        )
    if target_state == "pending":
        return (
            f"(wait {seconds:g}s) Background command {job_id} is still running. "
            "Call wait again to stay on this turn; do not end the turn."
        )
    if target_state == "settled":
        return (
            f"(wait {seconds:g}s) Background command {job_id} settled. "
            "Report the outcome and continue — do not wait for the user."
        )
    pending_n = 0
    for name in ("has_pending_swarms", "has_pending_command_jobs"):
        has_pending = getattr(session, name, None)
        if callable(has_pending):
            try:
                pending_n += 1 if bool(has_pending()) else 0
            except Exception:
                continue
    if settled and pending_n == 0:
        return (
            f"(wait {seconds:g}s) Background jobs settled. "
            "Report the outcome and continue — do not wait for the user."
        )
    if pending_n:
        return (
            f"(wait {seconds:g}s) Jobs still running. Call wait again to "
            "stay on this turn; do not end the turn."
        )
    return f"(wait {seconds:g}s) No pending background jobs."


def apply_ready_swarm_results(session: Any) -> Iterator[Any]:
    """Fold queued worker results into this turn (no FE pilot_resume)."""
    drain = getattr(session, "drain_swarm_results", None)
    if not callable(drain):
        return
    try:
        yielded = drain(emit_resume=False, already_holding_busy=True)
    except TypeError:
        # Older drain signature — skip in-turn apply rather than explode.
        return
    if yielded is None:
        return
    for ev in yielded:
        yield ev


def apply_ready_command_results(session: Any) -> Iterator[Any]:
    """Deliver command terminal receipts without scheduling pilot continuation."""
    from .conversation import ConvEvent
    drain = getattr(session, "drain_command_job_receipts", None)
    if not callable(drain):
        return
    try:
        receipts = drain(already_holding_busy=True)
    except TypeError:
        receipts = drain()
    except Exception:
        return
    for receipt in receipts or []:
        if isinstance(receipt, dict):
            yield ConvEvent("action_result", receipt)


def keep_alive_wait_slice(
    session: Any,
    seconds: float = WAIT_KEEPALIVE_SEC,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Iterator[Any]:
    """Sleep up to ``seconds``, emit wait chrome, apply any finished jobs."""
    from .conversation import ConvEvent

    seconds = parse_wait_seconds(seconds, default=WAIT_KEEPALIVE_SEC)
    deadline = monotonic() + seconds
    last_notice = 0.0
    yield ConvEvent("notice", {
        "kind": "wait",
        "message": "Waiting for background jobs…",
    })
    while monotonic() < deadline:
        if _cancel_requested(session):
            yield ConvEvent("notice", {
                "kind": "wait",
                "message": "Wait interrupted.",
            })
            return
        for ev in apply_ready_swarm_results(session):
            yield ev
        for ev in apply_ready_command_results(session):
            yield ev
        if not pending_jobs_keep_alive(session):
            yield ConvEvent("notice", {
                "kind": "wait",
                "message": "Background jobs finished.",
            })
            return
        now = monotonic()
        if now - last_notice >= 1.0:
            last_notice = now
            remain = max(0.0, deadline - now)
            yield ConvEvent("notice", {
                "kind": "wait",
                "message": f"Waiting for background jobs… {remain:.0f}s left",
            })
        sleep(min(WAIT_SLICE_SEC, max(0.0, deadline - monotonic())))
    for ev in apply_ready_swarm_results(session):
        yield ev
    for ev in apply_ready_command_results(session):
        yield ev


def dispatch_wait_action(
    session: Any,
    act: Any,
    aid: str,
    is_native: bool,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Iterator[Any]:
    """Pilot ``wait`` tool: timed keep-alive, then a status action_result."""
    from .conversation import ConvEvent

    args = getattr(act, "arguments", None) or {}
    job_id = str(args.get("job_id") or "").strip()
    raw_seconds = args.get("seconds")
    if raw_seconds is None:
        raw_seconds = getattr(act, "limit", None)
    seconds = parse_wait_seconds(raw_seconds, default=WAIT_DEFAULT_SEC)
    deadline = monotonic() + seconds
    last_notice = 0.0
    while monotonic() < deadline:
        if _cancel_requested(session):
            body = "(wait interrupted)"
            yield ConvEvent("action_result", {
                "id": aid, "error": body,
            })
            session._append_action_result(act, aid, body, is_native, ok=False)
            return
        for ev in apply_ready_swarm_results(session):
            yield ev
        for ev in apply_ready_command_results(session):
            yield ev
        if not pending_jobs_keep_alive(session, job_id=job_id):
            break
        now = monotonic()
        if now - last_notice >= 1.0:
            last_notice = now
            remain = max(0.0, deadline - now)
            yield ConvEvent("notice", {
                "kind": "wait",
                "message": f"Waiting… {remain:.0f}s left",
            })
        sleep(min(WAIT_SLICE_SEC, max(0.0, deadline - monotonic())))
    for ev in apply_ready_swarm_results(session):
        yield ev
    for ev in apply_ready_command_results(session):
        yield ev
    target_state = _target_command_wait_state(session, job_id)
    settled = (
        not pending_jobs_keep_alive(session, job_id=job_id)
        and target_state != "unavailable"
    )
    body = format_wait_status(session, seconds, settled, job_id=job_id)
    yield ConvEvent("action_result", {
        "id": aid,
        "status": "ok",
        "message": body,
        "settled": settled,
    })
    session._append_action_result(act, aid, body, is_native, ok=True)


def note_keep_alive_wait(session: Any) -> bool:
    """Count harness-injected waits; False once the per-turn cap is hit."""
    n = int(getattr(session, "_keep_alive_waits", 0) or 0) + 1
    session._keep_alive_waits = n
    return n <= WAIT_KEEPALIVE_CAP
