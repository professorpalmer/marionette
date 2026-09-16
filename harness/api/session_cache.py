"""Explicit-session cache controls; never fall back to the active view."""
from __future__ import annotations

from ..cache_keep_warm import KeepWarmLimits, set_reasoning_retention
from ..pilot_replacement import input_publication
from ..session_runners import resolve_session_runner
from ..sessions import persist_live_transcript


def _resolve(session_id, svc):
    if not isinstance(session_id, str) or not session_id.strip():
        return None, (400, {"error": "An explicit session_id is required."})
    pilot = resolve_session_runner(svc.get_runners(), session_id)
    if pilot is None or getattr(pilot, "cache_keep_warm", None) is None:
        return None, (409, {"error": "This session runner is unavailable.", "code": "session_changed"})
    return pilot, None


def _status(pilot, session_id):
    return {"session_id": session_id, "retain_reasoning": getattr(pilot, "retain_reasoning", False),
            **pilot.cache_keep_warm.status()}


def get_session_cache(session_id, svc):
    pilot, error = _resolve(session_id, svc)
    if error:
        return error
    return 200, _status(pilot, session_id)


def post_session_cache(body, svc):
    session_id = body.get("session_id")
    pilot, error = _resolve(session_id, svc)
    if error:
        return error
    action = body.get("action")
    if action not in {"start", "stop", "preferences"}:
        return 400, {"error": "Use start, stop, or preferences."}
    try:
        limits = KeepWarmLimits(body.get("max_refreshes", 3), body.get("idle_seconds", 3600),
                                body.get("max_spend_usd", 5.0)) if action == "start" else None
        if action == "preferences" and type(body.get("retain_reasoning")) is not bool:
            raise ValueError("retain_reasoning must be a boolean")
    except ValueError as exc:
        return 400, {"error": str(exc)}
    try:
        with input_publication(pilot):
            if svc.get_runners().get(session_id) is not pilot:
                return 409, {"error": "The session runner changed.", "code": "session_changed"}
            if action == "preferences":
                if pilot.is_turn_busy():
                    return 409, {"error": "Finish or stop the turn before changing reasoning retention."}
                set_reasoning_retention(pilot, body["retain_reasoning"])
                persist_live_transcript(pilot, pilot.state_dir, session_id, writer=svc.save_transcript)
            elif action == "start":
                pilot.cache_keep_warm.start(limits)
            else:
                pilot.cache_keep_warm.stop()
            return 200, _status(pilot, session_id)
    except ValueError as exc:
        return 409, {"error": str(exc)}
