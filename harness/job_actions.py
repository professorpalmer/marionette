"""Bounded, sanitized nested worker action rows for local jobs.

WorkerResult.events (and progressive ProviderWorker action_start/action_result)
are reduced to an ordered ``actions[]`` list keyed by stable action_id. Only
kind, goal/target, status, duration_ms, and a bounded error string are kept —
never stdout, prompts, args, env, or arbitrary payload fields.
"""
from __future__ import annotations

import copy
from typing import Any, Iterable, Optional

# Hard caps so a chatty worker cannot balloon swarm_local_jobs.json / live JSON.
MAX_JOB_ACTIONS = 80
MAX_ACTION_KIND_CHARS = 64
MAX_ACTION_GOAL_CHARS = 240
MAX_ACTION_ERROR_CHARS = 240
MAX_ACTION_ID_CHARS = 128

_ALLOWED_STATUSES = frozenset({"running", "complete", "failed"})
_TERMINAL_STATUSES = frozenset({"complete", "failed"})

# Keys that must never leak into persisted/live action rows.
_FORBIDDEN_DATA_KEYS = frozenset({
    "stdout", "stderr", "output", "content", "text", "prompt", "prompts",
    "args", "arguments", "argv", "env", "environ", "environment",
    "payload", "messages", "history", "system", "secrets", "secret",
    "token", "tokens", "api_key", "authorization", "password",
    "command",  # may contain secrets; never a goal source or persisted field
})

# Allow-listed keys on a persisted/live action row (reload re-sanitizes to these).
_ACTION_ROW_KEYS = frozenset({
    "action_id", "kind", "goal", "status", "duration_ms", "error", "worker_id",
})


def _bound_str(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: max(0, limit - 1)] + "…"


def _normalize_status(raw: Any, *, failed: bool = False) -> str:
    status = str(raw or "").strip().lower()
    if status in ("completed", "done", "success", "ok"):
        status = "complete"
    elif status in ("error", "cancelled", "canceled"):
        status = "failed"
    elif status in ("in_progress", "pending", "started"):
        status = "running"
    if failed and status != "complete":
        status = "failed"
    if status not in _ALLOWED_STATUSES:
        status = "failed" if failed else "running"
    return status


def _monotonic_status(existing: Any, incoming: Any) -> str:
    """Never regress terminal → running; never silently upgrade failed → complete.

    Same action_id means the same action (retries must use a new id). A terminal
    incoming status may settle a running row.
    """
    old = _normalize_status(existing)
    new = _normalize_status(incoming)
    if old in _TERMINAL_STATUSES and new == "running":
        return old
    if old == "failed" and new == "complete":
        return old
    return new


def _event_parts(ev: Any) -> tuple[str, dict]:
    kind = getattr(ev, "kind", None)
    data = getattr(ev, "data", None)
    if kind is None and isinstance(ev, dict):
        kind = ev.get("kind")
        data = ev.get("data") if "data" in ev else ev
    if not isinstance(data, dict):
        data = {}
    return str(kind or ""), data


def sanitize_action_row(
    *,
    action_id: str,
    kind: str = "",
    goal: str = "",
    status: str = "running",
    duration_ms: Any = None,
    error: Any = None,
    worker_id: str = "",
) -> Optional[dict]:
    """Return a bounded action row, or None when the id is unusable."""
    aid = _bound_str(action_id, MAX_ACTION_ID_CHARS)
    if not aid:
        return None
    err = _bound_str(error, MAX_ACTION_ERROR_CHARS) if error else ""
    failed = bool(err) or str(status or "").lower() in ("failed", "error", "cancelled", "canceled")
    row = {
        "action_id": aid,
        "kind": _bound_str(kind, MAX_ACTION_KIND_CHARS),
        "goal": _bound_str(goal, MAX_ACTION_GOAL_CHARS),
        "status": _normalize_status(status, failed=failed),
        "duration_ms": None,
        "error": err,
    }
    wid = _bound_str(worker_id, MAX_ACTION_ID_CHARS)
    if wid:
        row["worker_id"] = wid
    if duration_ms is not None:
        try:
            ms = int(duration_ms)
            if ms >= 0:
                row["duration_ms"] = ms
        except (TypeError, ValueError):
            pass
    return row


def sanitize_worker_event(ev: Any) -> Optional[dict]:
    """Map an action_start / action_result event into a sanitized action row."""
    kind, data = _event_parts(ev)
    if kind not in ("action_start", "action_result"):
        return None
    if any(k in data for k in _FORBIDDEN_DATA_KEYS):
        # Drop obviously contaminated payloads rather than echoing secrets.
        # Still allow the event if forbidden keys are present but we only read
        # the safe allow-list below — strip by omission, not by rejecting the
        # whole event (native cards often carry adapter/artifacts alongside id).
        pass

    action_id = data.get("id") or data.get("action_id") or data.get("tool_call_id") or ""
    # Explicit safe metadata only — never command (may contain secrets).
    goal = data.get("goal") or data.get("path") or data.get("target") or ""
    action_kind = data.get("kind") or data.get("tool") or ""
    if kind == "action_start":
        return sanitize_action_row(
            action_id=str(action_id),
            kind=str(action_kind),
            goal=str(goal),
            status="running",
        )

    err = data.get("error")
    status = "failed" if err else (data.get("status") or "complete")
    if str(status).lower() in ("pending", "deferred", "skipped"):
        # Dispatch acks are not terminal worker tool rows.
        status = "complete" if not err else "failed"
    return sanitize_action_row(
        action_id=str(action_id),
        kind=str(action_kind),
        goal=str(goal),
        status=str(status),
        duration_ms=data.get("duration_ms"),
        error=err,
    )


def upsert_action_row(actions: list, row: dict) -> list:
    """Update-or-append by action_id; preserve first-seen order; bound length.

    Status is monotonic: complete/failed never regress to running, and failed
    never silently becomes complete (same id = same action).
    """
    if not isinstance(row, dict):
        return list(actions or [])
    aid = str(row.get("action_id") or "").strip()
    if not aid:
        return list(actions or [])
    out = list(actions or [])
    for i, existing in enumerate(out):
        if not isinstance(existing, dict):
            continue
        if str(existing.get("action_id") or "") != aid:
            continue
        merged = dict(existing)
        # Never blank out a known kind/goal with empties from a sparse result.
        new_kind = str(row.get("kind") or "").strip()
        if new_kind:
            merged["kind"] = new_kind
        new_goal = str(row.get("goal") or "").strip()
        if new_goal:
            merged["goal"] = new_goal
        if row.get("status"):
            merged["status"] = _monotonic_status(merged.get("status"), row["status"])
        # Late duration_ms is always welcome (telemetry) and must not change a
        # failed status/error — monotonic status already blocked failed→complete.
        if row.get("duration_ms") is not None:
            merged["duration_ms"] = row["duration_ms"]
        if row.get("error"):
            # Only attach error when the settled status is failed (monotonic
            # may have kept failed over a late complete that cleared error).
            if merged.get("status") == "failed":
                merged["error"] = row["error"]
        elif merged.get("status") == "complete":
            merged["error"] = ""
        if row.get("worker_id") and not merged.get("worker_id"):
            merged["worker_id"] = row["worker_id"]
        if not merged.get("kind"):
            merged["kind"] = "tool_call"
        out[i] = merged
        return out[:MAX_JOB_ACTIONS]
    clean = dict(row)
    if not clean.get("kind"):
        clean["kind"] = "tool_call"
    out.append(clean)
    if len(out) > MAX_JOB_ACTIONS:
        out = out[-MAX_JOB_ACTIONS:]
    return out


def ingest_worker_events(events: Optional[Iterable[Any]]) -> list:
    """Reduce a WorkerResult.events stream to ordered sanitized actions."""
    actions: list = []
    if not events:
        return actions
    for ev in events:
        row = sanitize_worker_event(ev)
        if row is None:
            continue
        actions = upsert_action_row(actions, row)
    return actions


def merge_action_lists(base: Optional[list], incoming: Optional[list]) -> list:
    """Merge two sanitized action lists by action_id (incoming wins fields)."""
    out = list(base or [])
    for row in incoming or []:
        if isinstance(row, dict):
            sanitized = sanitize_action_row(
                action_id=str(row.get("action_id") or ""),
                kind=str(row.get("kind") or ""),
                goal=str(row.get("goal") or ""),
                status=str(row.get("status") or "running"),
                duration_ms=row.get("duration_ms"),
                error=row.get("error"),
                worker_id=str(row.get("worker_id") or ""),
            )
            if sanitized:
                out = upsert_action_row(out, sanitized)
    return out


def sanitize_actions_list(actions: Optional[Iterable[Any]]) -> list:
    """Re-sanitize persisted/tampered action rows through the allowlist + bounds."""
    out: list = []
    for raw in actions or []:
        if not isinstance(raw, dict):
            continue
        sanitized = sanitize_action_row(
            action_id=str(raw.get("action_id") or ""),
            kind=str(raw.get("kind") or ""),
            goal=str(raw.get("goal") or ""),
            status=str(raw.get("status") or "running"),
            duration_ms=raw.get("duration_ms"),
            error=raw.get("error"),
            worker_id=str(raw.get("worker_id") or ""),
        )
        if sanitized is None:
            continue
        # Drop any non-allowlisted keys that sneak in via stale disk rows.
        clean = {k: sanitized[k] for k in _ACTION_ROW_KEYS if k in sanitized}
        out = upsert_action_row(out, clean)
    return out[:MAX_JOB_ACTIONS]


def settle_running_actions(
    actions: Optional[list],
    *,
    reason: str = "interrupted",
    to_status: str = "failed",
) -> list:
    """Flip remaining status=running rows to a terminal status.

    Default is failed (interrupt / job-failed paths). Pass ``to_status="complete"``
    when the parent job completed successfully so late stragglers are not painted red.
    """
    terminal = _normalize_status(to_status, failed=(to_status != "complete"))
    if terminal == "running":
        terminal = "failed"
    err = _bound_str(reason, MAX_ACTION_ERROR_CHARS) or "interrupted"
    out: list = []
    for raw in actions or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        if _normalize_status(row.get("status")) == "running":
            row["status"] = terminal
            if terminal == "failed" and not row.get("error"):
                row["error"] = err
        out.append(row)
    return sanitize_actions_list(out)


def snapshot_actions(actions: Optional[list]) -> list:
    """Deep-copy a bounded actions list for lock-free callers."""
    return copy.deepcopy(list(actions or [])[:MAX_JOB_ACTIONS])
