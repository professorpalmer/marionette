"""Schedules HTTP route bodies shared by the app and external daemon."""

from __future__ import annotations

from datetime import datetime
import time
import os
import sqlite3
from uuid import UUID
from typing import Any, Dict, List, Optional, Union

from ..schedule_core import (
    CronExpr,
    Schedule,
    clip_notepad,
    next_real_fire_after,
    parse_failure_deliver,
    parse_missed_policy,
    timezone_mode,
    validate_timezone,
    schedule_wall,
    validate_recurrence,
)
from ..schedule_store import (
    REMOVE_CANCEL_REQUESTED,
    REMOVE_REMOVED,
    REMOVE_STALE_RECOVERED,
    ScheduleStore,
    ScheduleConflict,
    default_db_path,
)

JsonPayload = Union[dict, list]


def _store() -> ScheduleStore:
    return ScheduleStore(str(default_db_path()))


def _next_fire_previews(schedule: Schedule, count: int = 3) -> List[str]:
    """Wall time with UTC offset for IANA schedules; empty when paused/invalid."""
    if not schedule.enabled:
        return []
    if schedule.interval_seconds:
        anchor = schedule.enabled_at or schedule.created_at
        step = schedule.interval_seconds
        index = max(1, int((max(time.time(), schedule.last_fire_at) - anchor) // step) + 1)
        return [datetime.fromtimestamp(anchor + (index + i) * step).isoformat(sep=" ", timespec="seconds")
                for i in range(count)]
    try:
        cron = CronExpr.parse(schedule.cron)
        validate_timezone(schedule.timezone)
        cur = schedule_wall(schedule, datetime.now())
    except ValueError:
        return []
    out: List[str] = []
    for _ in range(count):
        try:
            cur = next_real_fire_after(cron, cur)
        except ValueError:
            break
        out.append(cur.isoformat(sep=" ", timespec="minutes"))
    return out


def _schedule_payload(schedule: Schedule) -> Dict[str, Any]:
    return {
        "id": schedule.id,
        "name": schedule.name,
        "objective": schedule.objective,
        "cron": schedule.cron,
        "interval_seconds": schedule.interval_seconds,
        "repo": schedule.repo,
        "swarm_adapter": schedule.swarm_adapter,
        "driver": schedule.driver,
        "delivery_mode": schedule.delivery_mode,
        "enabled": schedule.enabled,
        "max_tokens": schedule.max_tokens,
        "max_seconds": schedule.max_seconds,
        "max_swarms": schedule.max_swarms,
        "timezone": schedule.timezone,
        "revision": schedule.revision,
        "timezone_mode": timezone_mode(schedule),
        "display_status": schedule.display_status(),
        "last_status": schedule.last_status,
        "last_run_at": schedule.last_run_at,
        "last_fire_at": schedule.last_fire_at,
        "created_at": schedule.created_at,
        "enabled_at": schedule.enabled_at,
        "missed_policy": parse_missed_policy(schedule.missed_policy),
        "continuity_digest": str(schedule.continuity_digest or ""),
        "notepad": clip_notepad(schedule.notepad),
        "monitor_mode": bool(schedule.monitor_mode),
        "failure_deliver": parse_failure_deliver(schedule.failure_deliver),
        "next_fires": _next_fire_previews(schedule),
    }


def _require_id(body: dict) -> Optional[str]:
    raw = body.get("id")
    sid = raw.strip() if isinstance(raw, str) else ""
    return sid or None


def _write_fields(body: dict, *, creating: bool = False) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    for key in ("name", "objective", "cron", "repo", "driver", "swarm_adapter",
                "timezone", "notepad", "missed_policy", "failure_deliver"):
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, str):
            raise ValueError("%s must be text" % key)
        fields[key] = value.strip() if key != "notepad" else clip_notepad(value)
    for key in ("name", "objective"):
        if (creating or key in fields) and not fields.get(key):
            raise ValueError("%s is required" % key)
    if "swarm_adapter" in fields and not fields["swarm_adapter"]:
        raise ValueError("swarm_adapter is required when supplied")
    if creating or "repo" in fields:
        repo = fields.get("repo", "")
        if not repo or not os.path.isabs(repo):
            raise ValueError("repo must be an explicit absolute project path; each run starts a fresh session")
    if "interval_seconds" in body:
        fields["interval_seconds"] = body["interval_seconds"]
    if creating or "interval_seconds" in fields:
        fields.setdefault("cron", "")
        validate_recurrence(fields["cron"], fields.get("interval_seconds", 0))
    elif "cron" in fields and fields["cron"]:
        CronExpr.parse(fields["cron"])
    if "timezone" in fields:
        fields["timezone"] = validate_timezone(fields["timezone"])
    for key in ("max_tokens", "max_seconds", "max_swarms", "revision"):
        if key in body:
            value = body[key]
            if type(value) is not int or value < 0 or value > 9223372036854775807:
                raise ValueError("%s must be a non-negative 64-bit integer" % key)
            fields[key] = value
    for key in ("enabled", "monitor_mode"):
        if key in body:
            if not isinstance(body[key], bool):
                raise ValueError("%s must be true or false" % key)
            fields[key] = body[key]
    for key, choices in (("missed_policy", ("skip", "once", "all")),
                         ("failure_deliver", ("route", "suppress"))):
        if key in fields and fields[key] not in choices:
            raise ValueError("%s must be one of: %s" % (key, ", ".join(choices)))
    return fields


def get_schedules() -> tuple[int, JsonPayload]:
    """GET /api/schedules."""
    store = _store()
    try:
        schedules = [_schedule_payload(s) for s in store.list()]
    finally:
        store.close()
    return 200, {"schedules": schedules}


def get_schedules_history(
    schedule_id: str = "",
    limit_raw: str = "",
) -> tuple[int, JsonPayload]:
    """GET /api/schedules/history?id=&limit=."""
    sid = (schedule_id or "").strip()
    if not sid:
        return 400, {"error": "missing schedule id"}
    try:
        limit = int(limit_raw) if str(limit_raw or "").strip() else 50
    except (TypeError, ValueError):
        return 400, {"error": "limit must be an integer"}
    limit = max(1, min(limit, 500))
    store = _store()
    try:
        if store.get(sid) is None:
            return 404, {"error": "schedule not found"}
        runs = store.list_runs(sid, limit=limit)
    finally:
        store.close()
    return 200, {"id": sid, "runs": runs}


def post_schedules_add(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/schedules/add."""
    try:
        fields = _write_fields(body, creating=True)
        fields.pop("revision", None)
        request_id = body.get("request_id")
        if request_id is not None:
            if not isinstance(request_id, str):
                raise ValueError("request_id must be a UUID")
            try:
                request_id = UUID(request_id).hex
            except ValueError:
                raise ValueError("request_id must be a UUID")
        sched = Schedule(id=request_id or "", **fields)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    store = _store()
    try:
        try:
            store.add(sched)
        except sqlite3.IntegrityError:
            existing = store.get(sched.id)
            if not request_id or existing is None:
                raise
            if any(getattr(existing, key) != value for key, value in fields.items()):
                return 409, {"error": "This request already created a schedule; refresh and edit that schedule"}
            return 200, _schedule_payload(existing)
        payload = _schedule_payload(sched)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    finally:
        store.close()
    return 200, payload


def post_schedules_update(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/schedules/update."""
    sid = _require_id(body)
    if not sid:
        return 400, {"error": "missing schedule id"}
    try:
        fields = _write_fields(body)
        revision = fields.pop("revision", None)
        fields.pop("enabled", None)
        if not fields:
            raise ValueError("nothing to update")
    except ValueError as exc:
        return 400, {"error": str(exc)}
    store = _store()
    try:
        if store.get(sid) is None:
            return 404, {"error": "schedule not found"}
        updated = store.update_fields(sid, expected_revision=revision, **fields)
        if updated is None:
            return 404, {"error": "schedule not found"}
        payload = _schedule_payload(updated)
    except ScheduleConflict as exc:
        return 409, {"error": str(exc)}
    except ValueError as exc:
        return 400, {"error": str(exc)}
    finally:
        store.close()
    return 200, payload


def post_schedules_enable(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/schedules/enable."""
    sid = _require_id(body)
    if not sid:
        return 400, {"error": "missing schedule id"}
    store = _store()
    try:
        revision = _write_fields({"revision": body["revision"]}).get("revision") if "revision" in body else None
        if not store.set_enabled(sid, True, expected_revision=revision):
            return 404, {"error": "schedule not found"}
        sched = store.get(sid)
        if sched is None:
            return 404, {"error": "schedule not found"}
        payload = _schedule_payload(sched)
    except ScheduleConflict as exc:
        return 409, {"error": str(exc)}
    except ValueError as exc:
        return 400, {"error": str(exc)}
    finally:
        store.close()
    return 200, payload


def post_schedules_disable(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/schedules/disable."""
    sid = _require_id(body)
    if not sid:
        return 400, {"error": "missing schedule id"}
    store = _store()
    try:
        revision = _write_fields({"revision": body["revision"]}).get("revision") if "revision" in body else None
        if not store.set_enabled(sid, False, expected_revision=revision):
            return 404, {"error": "schedule not found"}
        sched = store.get(sid)
        if sched is None:
            return 404, {"error": "schedule not found"}
        payload = _schedule_payload(sched)
    except ScheduleConflict as exc:
        return 409, {"error": str(exc)}
    except ValueError as exc:
        return 400, {"error": str(exc)}
    finally:
        store.close()
    return 200, payload


def post_schedules_remove(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/schedules/remove."""
    sid = _require_id(body)
    if not sid:
        return 400, {"error": "missing schedule id"}
    store = _store()
    try:
        outcome = store.remove(sid)
    finally:
        store.close()
    if outcome is False:
        return 404, {"error": "schedule not found"}
    return 200, {"ok": True, "outcome": outcome}


def post_schedules_run_now(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/schedules/run-now — one-shot via scheduler.run_one_now."""
    from .. import scheduler as _sched

    sid = _require_id(body)
    if not sid:
        return 400, {"error": "missing schedule id"}
    store = _store()
    try:
        if store.get(sid) is None:
            return 404, {"error": "schedule not found"}
        revision = _write_fields({"revision": body["revision"]}).get("revision") if "revision" in body else None
        run = _sched.run_one_now(store, sid, expected_revision=revision)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    finally:
        store.close()
    if run is None:
        return 404, {"error": "schedule not found"}
    run["id"] = run.get("run_id", "")
    if run["status"] == "blocked":
        return 409, {"ok": False, "error": "Schedule is already running or changed; refresh before retrying", "run": run}
    return 200, {"ok": run["status"] == "ok", "run": run}


# Re-export remove outcomes for tests / callers.
__all__ = [
    "REMOVE_CANCEL_REQUESTED",
    "REMOVE_REMOVED",
    "REMOVE_STALE_RECOVERED",
    "get_schedules",
    "get_schedules_history",
    "post_schedules_add",
    "post_schedules_update",
    "post_schedules_enable",
    "post_schedules_disable",
    "post_schedules_remove",
    "post_schedules_run_now",
]
