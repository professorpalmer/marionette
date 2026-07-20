"""Schedules HTTP route bodies (control plane; daemon stays external)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from ..schedule_core import (
    CronExpr,
    Schedule,
    next_real_fire_after,
    timezone_mode,
)
from ..schedule_store import (
    REMOVE_CANCEL_REQUESTED,
    REMOVE_REMOVED,
    REMOVE_STALE_RECOVERED,
    ScheduleStore,
    default_db_path,
)

JsonPayload = Union[dict, list]


def _store() -> ScheduleStore:
    return ScheduleStore(str(default_db_path()))


def _next_fire_previews(schedule: Schedule, count: int = 3) -> List[str]:
    """Host-local ISO minute previews (IANA per-schedule zones deferred)."""
    try:
        cron = CronExpr.parse(schedule.cron)
    except ValueError:
        return []
    cur = datetime.now()
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
        "repo": schedule.repo,
        "swarm_adapter": schedule.swarm_adapter,
        "driver": schedule.driver,
        "enabled": schedule.enabled,
        "max_tokens": schedule.max_tokens,
        "max_seconds": schedule.max_seconds,
        "max_swarms": schedule.max_swarms,
        "timezone": "",
        "timezone_mode": timezone_mode(schedule),
        "display_status": schedule.display_status(),
        "last_status": schedule.last_status,
        "last_run_at": schedule.last_run_at,
        "last_fire_at": schedule.last_fire_at,
        "created_at": schedule.created_at,
        "enabled_at": schedule.enabled_at,
        "next_fires": _next_fire_previews(schedule),
    }


def _require_id(body: dict) -> Optional[str]:
    sid = (body.get("id") or "").strip()
    return sid or None


def _reject_timezone_if_set(body: dict) -> Optional[tuple[int, JsonPayload]]:
    """IANA per-schedule zones are deferred; HTTP stays host-local only."""
    if "timezone" not in body:
        return None
    raw = body.get("timezone")
    if raw is None:
        return None
    if str(raw).strip():
        return 400, {"error": "IANA timezone deferred; use host-local (omit timezone)"}
    return None


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
        limit = int(limit_raw) if str(limit_raw).strip() else 50
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
    rejected = _reject_timezone_if_set(body)
    if rejected is not None:
        return rejected
    name = (body.get("name") or "").strip()
    objective = (body.get("objective") or "").strip()
    cron = (body.get("cron") or "").strip()
    if not name:
        return 400, {"error": "name is required"}
    if not objective:
        return 400, {"error": "objective is required"}
    if not cron:
        return 400, {"error": "cron is required"}
    try:
        CronExpr.parse(cron)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    sched = Schedule(
        id="",
        name=name,
        objective=objective,
        cron=cron,
        repo=str(body.get("repo") or ""),
        swarm_adapter=str(body.get("swarm_adapter") or "demo"),
        driver=str(body.get("driver") or ""),
        enabled=bool(body.get("enabled", True)),
        max_tokens=int(body.get("max_tokens") or 0),
        max_seconds=int(body.get("max_seconds") or 0),
        max_swarms=int(body.get("max_swarms") or 0),
        timezone="",
    )
    store = _store()
    try:
        store.add(sched)
        payload = _schedule_payload(sched)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    finally:
        store.close()
    return 200, payload


def post_schedules_update(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/schedules/update."""
    rejected = _reject_timezone_if_set(body)
    if rejected is not None:
        return rejected
    sid = _require_id(body)
    if not sid:
        return 400, {"error": "missing schedule id"}
    fields: Dict[str, Any] = {}
    for key in (
        "name", "objective", "cron", "repo", "driver", "swarm_adapter",
        "max_tokens", "max_seconds", "max_swarms",
    ):
        if key in body:
            fields[key] = body[key]
    # Empty timezone is a no-op clear to host-local; non-empty already rejected.
    if "timezone" in body:
        fields["timezone"] = ""
    if not fields:
        return 400, {"error": "nothing to update"}
    store = _store()
    try:
        if store.get(sid) is None:
            return 404, {"error": "schedule not found"}
        updated = store.update_fields(sid, **fields)
        if updated is None:
            return 404, {"error": "schedule not found"}
        payload = _schedule_payload(updated)
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
        if not store.set_enabled(sid, True):
            return 404, {"error": "schedule not found"}
        sched = store.get(sid)
        assert sched is not None
        payload = _schedule_payload(sched)
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
        if not store.set_enabled(sid, False):
            return 404, {"error": "schedule not found"}
        sched = store.get(sid)
        assert sched is not None
        payload = _schedule_payload(sched)
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
        run = _sched.run_one_now(store, sid)
    finally:
        store.close()
    if run is None:
        return 404, {"error": "schedule not found"}
    return 200, {"ok": True, "run": run}


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
