"""Live-session control HTTP bodies (peeled from ``harness.server``).

Covers stash / interrupt / rewind / steer / prompt-queue, plus persist /
compact / state / context_at / swarm-results and restart-prepare (transcript
flush + resume latch). Process self-terminate for ``POST /api/restart``
stays on Handler.
"""

from __future__ import annotations

import os
import tempfile as _tf
from contextlib import nullcontext
from functools import wraps
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

from ..pilot_replacement import input_admission, input_publication
from ..delivery_mode import apply_delivery, normalize_delivery_mode, realized_steer_action
from ..prompt_queue import ORIGINAL_TEXT_UNSET, PromptQueueError, PromptQueueMixin
from ..session_actions import (
    ActionKind,
    SessionActionIllegalTransition,
    SessionActionStore,
    normalize_turn_input_mode,
)
from ..session_loop import (
    SessionLoopError,
    session_loop_snapshot,
    start_session_loop,
    stop_session_loop,
)


@dataclass
class SessionControlServices:
    """Explicit deps for session-control HTTP handlers."""

    cfg: Any
    get_pilot: Callable[[], Any]
    get_runners: Callable[[], Any]
    gate_active_pilot_ready: Callable[[], Optional[dict]]
    stash_put: Callable[..., str]
    save_active_transcript: Callable[[], None]
    upload_dir: str
    diag: Callable[..., Any]
    # persist / compact / state / context_at / swarm-results / restart-prepare
    get_sessions: Optional[Callable[[], Any]] = None
    save_transcript: Optional[Callable[..., None]] = None
    # Arms for the active view; optional session_id stamps the latch owner.
    set_resume_latch: Optional[Callable[..., None]] = None
    persist_boot_usage: Optional[Callable[..., None]] = None
    # Peek leaves the latch armed so StatusBar / LeftRail / runners polls cannot
    # steal the one-shot; consume is opt-in via ?consume_resume=1 (Conversation).
    # Both take (idle, session_id) and fail closed on owner mismatch.
    peek_resume_pending: Optional[Callable[..., bool]] = None
    consume_resume_pending: Optional[Callable[..., bool]] = None
    checkpoint_transcript: Optional[Callable[[], None]] = None
    context_at: Optional[Callable[..., Any]] = None
    pilot_swap_lock: Any = None
    gate_goal_pilot_ready: Optional[Callable[[], Optional[dict]]] = None


JsonPayload = Union[dict, list]


def _validate_upload_images(
    images: list, upload_dir: str, *, session=None
) -> tuple[Optional[list], Optional[tuple[int, dict]]]:
    if not isinstance(images, (list, tuple)):
        return None, (400, {'ok': False, 'code': 'input_attachment_invalid', 'error': 'Images must be a list.'})
    valid_imgs = []
    upload_dir_real = os.path.realpath(upload_dir)
    for p in images:
        if not p:
            continue
        if not isinstance(p, str):
            return None, (400, {'ok': False, 'code': 'input_attachment_invalid', 'error': 'Image references must be strings.'})
        if p.startswith('input:'):
            if session is None:
                return None, (403, {'ok': False, 'code': 'input_owner_required', 'error': 'A bound session is required.'})
            from ..input_receipts import session_input_store
            with input_publication(session):
                session_input_store(session).attachment(p)
            valid_imgs.append(p)
            continue
        real_p = os.path.realpath(p)
        try:
            if os.path.commonpath([upload_dir_real, real_p]) == upload_dir_real:
                valid_imgs.append(p)
            else:
                return None, (400, {"error": f"Invalid image path: {p}"})
        except ValueError:
            return None, (400, {"error": f"Invalid image path: {p}"})
    return valid_imgs, None


def prepare_session_restart(svc: SessionControlServices) -> tuple[bool, Optional[str]]:
    """Flush transcript + arm resume latch + persist boot usage.

    Shared by ``POST /api/session/persist`` and the prepare half of
    ``POST /api/restart``. Returns ``(ok, error_message)``.
    """
    try:
        sessions = svc.get_sessions() if svc.get_sessions is not None else None
        pilot = svc.get_pilot()
        if sessions is not None and sessions.active and svc.save_transcript is not None:
            from ..sessions import persist_live_transcript
            persist_live_transcript(
                pilot,
                svc.cfg.state_dir or _tf.gettempdir(),
                sessions.active,
                writer=svc.save_transcript,
            )
        if svc.set_resume_latch is not None:
            sid = ""
            if sessions is not None:
                sid = (getattr(sessions, "active", None) or "").strip()
            svc.set_resume_latch(sid)
        if svc.persist_boot_usage is not None:
            svc.persist_boot_usage(fold_live=True, force=True)
        return True, None
    except Exception as e:
        return False, str(e)


def post_session_persist(svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """POST /api/session/persist."""
    ok, err = prepare_session_restart(svc)
    if ok:
        return 200, {"ok": True}
    return 500, {"ok": False, "error": err}


def _event_kind(event: Any) -> str:
    """Kind of a compaction-stream event (ConvEvent or plain dict)."""
    kind = getattr(event, "kind", None)
    if kind is None and isinstance(event, dict):
        kind = event.get("kind")
    return str(kind or "")


def _event_data(event: Any) -> dict:
    """Payload of a compaction-stream event (ConvEvent or plain dict)."""
    data = getattr(event, "data", None)
    if data is None and isinstance(event, dict):
        data = event.get("data")
    return data if isinstance(data, dict) else {}


def _successful_compaction(
    events: list, *, before_tokens: int, after_tokens: int
) -> bool:
    """True only when a non-aborted compaction actually shrank history.

    ``_maybe_compact_history`` can still emit ``kind=compaction`` with
    ``aborted=True`` (degenerate_summary / insufficient_reduction) while
    leaving history unchanged — those must not count as Compact Now success.
    """
    for ev in events:
        if _event_kind(ev) != "compaction":
            continue
        data = _event_data(ev)
        if data.get("aborted"):
            continue
        try:
            ev_before = int(data.get("before_tokens", before_tokens))
            ev_after = int(data.get("after_tokens", after_tokens))
        except (TypeError, ValueError):
            ev_before, ev_after = before_tokens, after_tokens
        if ev_after < ev_before:
            return True
        if after_tokens < before_tokens:
            return True
    return False


def _record_post_compaction_snapshot(pilot: Any, svc: SessionControlServices) -> None:
    """Best-effort: journal a fresh L0-L3 layer snapshot after manual compaction.

    /api/usage builds its compaction advice from the LATEST recorded layer
    snapshot; without this refresh it keeps serving the pre-compaction L0, so
    the "Compact now" advisor stays visible (and survives reopen) even though
    the history really shrank.
    """
    try:
        from ..memory_layers import (
            record_memory_layer_snapshot,
            snapshot_memory_layers,
        )

        state_dir = getattr(pilot, "state_dir", "") or svc.cfg.state_dir or ""
        if not state_dir:
            return
        session_id = getattr(pilot, "harness_session_id", "") or "default"
        history = getattr(pilot, "_history", None) or []
        user_turns = sum(
            1 for m in history if isinstance(m, dict) and m.get("role") == "user"
        )
        turn = max(1, user_turns)
        record_memory_layer_snapshot(
            state_dir,
            session_id,
            turn,
            snapshot_memory_layers(
                pilot,
                state_dir,
                session_id,
                repo=getattr(svc.cfg, "repo", "") or "",
            ),
        )
    except Exception:
        pass


def _compaction_attempt_reason(pilot: Any) -> str:
    """Best-effort structured reason from the last compaction attempt."""
    try:
        attempt = getattr(pilot, "_last_compaction_attempt", None) or {}
        reason = str(attempt.get("reason") or "").strip()
        if reason:
            return reason
    except Exception:
        pass
    return "no_compactable_history"


def _compaction_error_message(reason: str) -> str:
    if reason == "no_compactable_history":
        return "Recent turn is already compact"
    if reason == "summary_rejected":
        return "Compaction summary was rejected; history left unchanged"
    if reason == "below_min_compactable":
        return "Not enough history to compact yet"
    return "no compaction occurred (history too small or summary rejected)"



def _hot_context_messages(pilot: Any) -> list:
    """User/tool/assistant turns from the live pilot. Fail closed to []."""
    try:
        data = pilot.export_transcript_data()
    except Exception:
        data = None
    if isinstance(data, dict):
        history = data.get("history")
        if isinstance(history, list) and history:
            return [m for m in history if isinstance(m, dict)]
    raw = getattr(pilot, "_history", None) or []
    msgs = [m for m in raw if isinstance(m, dict)]
    if msgs and str(msgs[0].get("role") or "") == "system":
        return msgs[1:]
    return msgs


def post_session_snapcompact(
    svc: SessionControlServices,
    body: Optional[dict] = None,
) -> tuple[int, JsonPayload]:
    """POST /api/session/snapcompact (also ``POST /api/session/compact`` op=snap).

    One-shot compact-to-snapshot on the existing vault/archive/journal path.
    """
    not_ready = svc.gate_active_pilot_ready()
    if not_ready is not None:
        return 409, not_ready
    body = body or {}
    pilot = svc.get_pilot()
    sessions = svc.get_sessions() if svc.get_sessions is not None else None
    sid = (
        str(body.get("session_id") or "").strip()
        or getattr(sessions, "active", None)
        or getattr(pilot, "harness_session_id", None)
        or "default"
    )
    messages = _hot_context_messages(pilot)
    state_dir = getattr(svc.cfg, "state_dir", None) or _tf.gettempdir()
    from ..compaction_vault import snap_compact

    result = snap_compact(state_dir, str(sid), messages)
    if not result.get("ok"):
        reason = str(result.get("reason") or "no_compactable_history")
        return 409, {
            **result,
            "compacted": False,
            "error": _compaction_error_message(reason),
        }
    if sessions is not None and sessions.active and svc.save_transcript is not None:
        try:
            from ..sessions import persist_live_transcript
            persist_live_transcript(
                pilot, state_dir, sessions.active, writer=svc.save_transcript,
            )
        except Exception:
            pass
    return 200, {
        **result,
        "compacted": True,
        "op": "snap",
    }


def post_session_compact_routed(
    body: dict,
    svc: SessionControlServices,
) -> tuple[int, JsonPayload]:
    """Route compact POSTs; ``op=snap`` is snapcompact, else Compact Now."""
    op = str((body or {}).get("op") or "").strip().lower()
    if op in ("snap", "snapcompact"):
        return post_session_snapcompact(svc, body)
    return post_session_compact(svc)


def post_session_snapcompact_routed(
    body: dict,
    svc: SessionControlServices,
) -> tuple[int, JsonPayload]:
    """POST /api/session/snapcompact with optional JSON body."""
    return post_session_snapcompact(svc, body)


def post_session_compact(svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """POST /api/session/compact.

    Manual "Compact now": force a compaction attempt and report success ONLY
    when a non-aborted ``compaction`` event shrank history. No-ops -- history
    too small to split, below min floor, degenerate summary, insufficient
    reduction -- return 409 with ``ok: false`` and a structured ``reason``.
    Failed / no-op attempts do not latch the Needs-attention ack.
    """
    not_ready = svc.gate_active_pilot_ready()
    if not_ready is not None:
        return 409, not_ready
    pilot = svc.get_pilot()
    before = pilot._estimate_context_tokens()
    events = list(pilot._maybe_compact_history(force=True))
    after = pilot._estimate_context_tokens()
    compacted = _successful_compaction(
        events, before_tokens=before, after_tokens=after
    )
    if not compacted:
        reason = _compaction_attempt_reason(pilot)
        # Keep below_min_compactable distinct — do not remap to the
        # "already compact" bucket while pressure may still be high.
        if reason == "below_trigger":
            reason = "no_compactable_history"
        # Refresh L0 snapshot for honest /api/usage advice; do NOT latch
        # calm on failed Compact Now (pressure must stay visible).
        _record_post_compaction_snapshot(pilot, svc)
        return 409, {
            "ok": False,
            "compacted": False,
            "before_tokens": before,
            "after_tokens": after,
            "reason": reason,
            "error": _compaction_error_message(reason),
        }
    sessions = svc.get_sessions() if svc.get_sessions is not None else None
    if sessions is not None and sessions.active and svc.save_transcript is not None:
        from ..sessions import persist_live_transcript
        persist_live_transcript(
            pilot,
            svc.cfg.state_dir or _tf.gettempdir(),
            sessions.active,
            writer=svc.save_transcript,
        )
    _record_post_compaction_snapshot(pilot, svc)
    try:
        from ..compaction_advisor import ack_manual_compaction

        ack_manual_compaction(pilot, reason="ok")
    except Exception:
        pass
    return 200, {
        "ok": True,
        "compacted": True,
        "before_tokens": before,
        "after_tokens": after,
        "reason": "ok",
    }


def _truthy_qs_flag(qs: dict, key: str) -> bool:
    raw = (qs.get(key, [""])[0] or "").strip().lower()
    return raw in ("1", "true", "yes")


def _qs_session_id(qs: dict) -> str:
    return (qs.get("session_id", [""])[0] or "").strip()


def get_session_state(qs: dict, svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """GET /api/session/state.

    ``resume_pending`` peeks by default so incidental polls cannot clear the
    self-edit restart latch. Pass ``?consume_resume=1`` to consume once (the
    Conversation resume-schedule path). Pass ``?rearm_resume=1`` to restore the
    latch after a consume that was abandoned by a session switch / cancelled
    kick (Conversation stillCurrent fence). Pass ``?session_id=`` so peek /
    consume / rearm only apply to the latch owner (fail closed on mismatch).
    """
    pilot = svc.get_pilot()
    runners = svc.get_runners()
    if pilot is None:
        return 200, {
            "state": "idle", "pending_swarms": False, "resume_pending": False,
            "runners": runners.statuses(), "active_view_id": None,
            "goal": {}, "todos": {},
        }
    state = pilot.state()
    idle = state == "idle"
    resume_pending = False
    qs = qs or {}
    session_id = _qs_session_id(qs)
    if _truthy_qs_flag(qs, "rearm_resume"):
        if svc.set_resume_latch is not None:
            svc.set_resume_latch(session_id)
        if svc.peek_resume_pending is not None:
            resume_pending = svc.peek_resume_pending(idle, session_id)
        else:
            resume_pending = bool(idle)
    elif _truthy_qs_flag(qs, "consume_resume"):
        if svc.consume_resume_pending is not None:
            resume_pending = svc.consume_resume_pending(idle, session_id)
    elif svc.peek_resume_pending is not None:
        resume_pending = svc.peek_resume_pending(idle, session_id)
    elif svc.consume_resume_pending is not None:
        # Legacy services without peek: never consume on a plain state read.
        resume_pending = False
    goal = {}
    try:
        if pilot is not None and hasattr(pilot, "session_goal_dict"):
            goal = pilot.session_goal_dict() or {}
    except Exception:
        goal = {}
    todos = {}
    try:
        todo_pilot = pilot
        if session_id:
            other = runners.get(session_id) if hasattr(runners, "get") else None
            todo_pilot = other
        if todo_pilot is not None and hasattr(todo_pilot, "todo_snapshot"):
            todos = todo_pilot.todo_snapshot() or {}
        elif session_id:
            todos = {}
    except Exception:
        todos = {}
    return 200, {
        "state": state,
        "pending_swarms": pilot.has_pending_swarms(),
        "resume_pending": resume_pending,
        "runners": runners.statuses(),
        # Active VIEW id so StatusBar can distinguish this session's
        # runner from background sessions still executing under the lease.
        "active_view_id": runners.active_view_id,
        # Sticky session GOAL (chip-ready); distinct from Schedule.objective.
        "goal": goal,
        "todos": todos,
    }


def _goal_pilot(svc: SessionControlServices, session_id: Optional[str] = None) -> tuple[Any, Optional[tuple[int, dict]]]:
    original = svc.get_pilot()
    owner = getattr(original, "harness_session_id", "") or ""
    if session_id is not None and session_id != owner:
        return None, (409, {"ok": False, "error": "active session changed", "code": "session_changed"})
    if not original:
        return None, (404, {"ok": False, "error": "no active session"})
    not_ready = (svc.gate_goal_pilot_ready or svc.gate_active_pilot_ready)()
    if not_ready is not None:
        return None, (409, not_ready)
    pilot = svc.get_pilot()
    if not pilot:
        return None, (404, {"ok": False, "error": "no active session"})
    if (getattr(pilot, "harness_session_id", "") or "") != owner:
        return None, (409, {"ok": False, "error": "active session changed", "code": "session_changed"})
    return pilot, None


def get_session_goal(session_id: Optional[str], svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """GET /api/session/goal."""
    pilot, err = _goal_pilot(svc, session_id)
    if err is not None:
        return err
    goal = {}
    try:
        goal = pilot.session_goal_dict() if hasattr(pilot, "session_goal_dict") else {}
    except Exception:
        goal = {}
    return 200, {"ok": True, "goal": goal}


def post_session_goal(body: dict, svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """POST /api/session/goal — set / pause / resume / complete / clear."""
    pilot, err = _goal_pilot(svc, body.get("session_id"))
    if err is not None:
        return err
    with svc.pilot_swap_lock if svc.pilot_swap_lock is not None else nullcontext():
        if svc.get_pilot() is not pilot:
            return 409, {"ok": False, "error": "active session changed", "code": "session_changed"}
        action = str(body.get("action") or "set").strip().lower()
        budget = body.get("token_budget")
        token_budget = None
        if budget not in ("", None):
            try:
                token_budget = int(budget)
            except (TypeError, ValueError):
                return 400, {"ok": False, "error": "token_budget must be an int"}
        try:
            if action == "set":
                text = (body.get("text") or body.get("goal") or "").strip()
                if not text:
                    return 400, {"ok": False, "error": "missing text"}
                goal = pilot.set_session_goal(text, token_budget=token_budget)
            elif action == "pause":
                goal = pilot.pause_session_goal()
            elif action == "resume":
                goal = pilot.resume_session_goal()
            elif action == "complete":
                goal = pilot.complete_session_goal()
            elif action == "clear":
                goal = pilot.clear_session_goal()
            else:
                return 400, {"ok": False, "error": "unknown action"}
        except Exception as exc:
            return 500, {"ok": False, "error": str(exc)}
        return 200, {"ok": True, "goal": goal}


def get_session_loop(svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """GET /api/session/loop — in-session interval/self-paced loop, not cron."""
    pilot, err = _goal_pilot(svc)
    if err is not None:
        return err
    return 200, {"ok": True, "loop": session_loop_snapshot(pilot)}


def post_session_loop(body: dict, svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """POST /api/session/loop — start / stop. Distinct from /api/session/goal."""
    pilot, err = _goal_pilot(svc)
    if err is not None:
        return err
    action = str(body.get("action") or "start").strip().lower()
    try:
        if action == "stop":
            loop = stop_session_loop(pilot)
        elif action == "start":
            interval = body.get("interval_seconds")
            if interval in ("", None):
                interval_seconds = None
            else:
                try:
                    interval_seconds = float(interval)
                except (TypeError, ValueError):
                    return 400, {
                        "ok": False,
                        "error": "interval_seconds must be a number",
                    }
            raw_until = body.get("until")
            if raw_until in ("", None):
                until = None
            else:
                try:
                    until = float(raw_until)
                except (TypeError, ValueError):
                    return 400, {
                        "ok": False,
                        "error": "until must be a unix timestamp",
                    }
            loop = start_session_loop(
                pilot,
                body.get("mode"),
                (body.get("prompt") or body.get("text") or ""),
                interval_seconds=interval_seconds,
                until=until,
            )
        else:
            return 400, {"ok": False, "error": "unknown action"}
    except SessionLoopError as exc:
        return 400, {"ok": False, "error": str(exc), "code": exc.code}
    return 200, {"ok": True, "loop": loop.to_dict()}


def post_session_todo(body: dict, svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """POST /api/session/todo — local /todo slash (view/export/import/mutate)."""
    pilot, err = _goal_pilot(svc)
    if err is not None:
        return err
    command = str(body.get("command") or body.get("text") or "").strip()
    workspace = str(getattr(svc.cfg, "repo", "") or "").strip()
    try:
        if not hasattr(pilot, "handle_todo_slash"):
            return 404, {"ok": False, "error": "todo slash is unavailable"}
        payload = pilot.handle_todo_slash(command, workspace_root=workspace)
    except Exception as exc:
        return 500, {"ok": False, "error": str(exc)}
    if not isinstance(payload, dict):
        return 500, {"ok": False, "error": "todo slash returned no payload"}
    return 200, payload


def get_session_context_at(
    turn: int, svc: SessionControlServices
) -> tuple[int, JsonPayload]:
    """GET /api/session/context_at?turn=N."""
    pilot = svc.get_pilot()
    if svc.context_at is None:
        from ..turn_context import context_at as _context_at
        record = _context_at(
            pilot.state_dir,
            getattr(pilot, "harness_session_id", "") or "default",
            turn,
        )
    else:
        record = svc.context_at(
            pilot.state_dir,
            getattr(pilot, "harness_session_id", "") or "default",
            turn,
        )
    if record is None:
        return 404, {"error": f"no context recorded for turn {turn}"}
    return 200, record


def get_session_swarm_results(svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """GET /api/session/swarm-results."""
    pilot = svc.get_pilot()
    results = []
    for ev in pilot.drain_swarm_results():
        results.append({"kind": ev.kind, "data": ev.data})
    if results and svc.checkpoint_transcript is not None:
        # The drain just appended history + display entries (incl. the
        # swarm outcome badge). This poll path runs while the session is
        # idle, so persist now -- otherwise closing the app before the
        # next turn would drop them.
        svc.checkpoint_transcript()
    return 200, {"results": results}


def post_chat_stash(body: dict, svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """POST /api/chat/stash."""
    message = body.get("message", "")
    images = body.get("images") or []
    if isinstance(images, str):
        images = [p for p in images.split("|") if p]
    documents = body.get("documents")
    metadata = {key: body[key] for key in ("documents", "retry_key", "input_id", "handoff_token", "session_id", "original_text")
                if key in body and (body[key] is not None or key == "original_text")}
    if (not isinstance(message, str) or not isinstance(images, list)
            or (documents is not None and not isinstance(documents, list))
            or any(not isinstance(value, str) or (not value and key != "original_text") for key, value in metadata.items() if key != "documents")):
        return 400, {"ok": False, "code": "input_invalid", "error": "Invalid input submission."}
    if not message and not images and not documents:
        return 400, {"ok": False, "error": "missing message"}
    mid = svc.stash_put(message, images, **metadata)
    return 200, {"id": mid}


def post_session_interrupt(
    body: dict, session_id: str, svc: SessionControlServices
) -> tuple[int, JsonPayload]:
    """POST /api/session/interrupt."""
    if svc is None:
        # Route-table rebuild tests may pass None; Stop must still cancel the
        # live server pilot instead of AttributeError on get_pilot().
        from .. import server as _srv
        svc = _srv._session_control_services()
    sid = (session_id or body.get("session_id") or "").strip()
    target = None
    if sid:
        target = svc.get_runners().get(sid)
        if target is None:
            return 404, {"ok": False, "error": "session runner not found"}
        target.interrupt()
    else:
        pilot = svc.get_pilot()
        if pilot is not None:
            pilot.interrupt()
        target = pilot
    # Snapshot Stop honesty notices (owned-command orphan / steer drop) so the
    # UI can paint them when the abandoned stream never live-flushes. Do not
    # drain — stream flush sites still own pending → ConvEvent("notice").
    notices: list[dict] = []
    if target is not None:
        peek = getattr(target, "peek_post_interrupt_notices", None)
        if callable(peek):
            try:
                notices = list(peek() or [])
            except Exception:
                notices = []
    payload: dict = {"ok": True}
    if notices:
        payload["notices"] = notices
    input_error = getattr(target, '_input_stop_error', None) if target is not None else None
    if input_error:
        return 503, {**payload, **input_error, 'stopped': True}
    return 200, payload


def post_session_rewind(body: dict, svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """POST /api/session/rewind."""
    if not svc.get_pilot():
        return 404, {"ok": False, "error": "no active session"}
    not_ready = svc.gate_active_pilot_ready()
    if not_ready is not None:
        return 409, not_ready
    # Re-fetch after gate: ensure_ready may have swapped out a deferred placeholder.
    pilot = svc.get_pilot()
    if not pilot:
        return 404, {"ok": False, "error": "no active session"}
    result = None
    if body.get("user_ordinal") is not None:
        try:
            user_ordinal = int(body.get("user_ordinal"))
        except (TypeError, ValueError):
            return 400, {"ok": False, "error": "user_ordinal must be an int"}
        hint = body.get("text") or body.get("text_hint")
        if isinstance(hint, str) and hint.strip():
            result = pilot.rewind_to_user_ordinal(user_ordinal, text_hint=hint)
        else:
            result = pilot.rewind_to_user_ordinal(user_ordinal)
    elif body.get("display_index") is not None:
        try:
            display_index = int(body.get("display_index"))
        except (TypeError, ValueError):
            return 400, {"ok": False, "error": "display_index must be an int"}
        result = pilot.rewind_to_display_index(display_index)
    else:
        return 400, {"ok": False, "error": "user_ordinal or display_index required"}
    if not result.get("ok"):
        code = 409 if result.get("code") == "busy" else 400
        return code, result
    try:
        svc.save_active_transcript()
    except Exception as e:
        svc.diag("server.rewind_persist", e)
    return 200, result


def post_session_rewind_restore(svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """POST /api/session/rewind/restore."""
    if not svc.get_pilot():
        return 404, {"ok": False, "error": "no active session"}
    not_ready = svc.gate_active_pilot_ready()
    if not_ready is not None:
        return 409, not_ready
    pilot = svc.get_pilot()
    if not pilot:
        return 404, {"ok": False, "error": "no active session"}
    result = pilot.restore_rewind_stash()
    if not result.get("ok"):
        code = 409 if result.get("code") == "busy" else 400
        return code, result
    try:
        svc.save_active_transcript()
    except Exception as e:
        svc.diag("server.rewind_restore_persist", e)
    try:
        data = pilot.export_transcript_data()
    except Exception:
        data = {}
    result["display"] = data.get("display") or []
    result["history"] = data.get("history") or []
    return 200, result


def _pilot_turn_busy(pilot: Any) -> bool:
    try:
        if hasattr(pilot, "is_turn_busy"):
            return bool(pilot.is_turn_busy())
    except Exception:
        return False
    return False


def _optional_expected_turn_id(body: dict) -> Optional[str]:
    raw = body.get("expected_turn_id")
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    return text or None


def _pilot_action_store(pilot: Any) -> Optional[SessionActionStore]:
    getter = getattr(pilot, "_action_store", None)
    if callable(getter):
        got = getter()
        if isinstance(got, SessionActionStore):
            return got
    return None


def _is_recover_turn(body: dict) -> bool:
    if body.get("recover") is True:
        return True
    kind = str(body.get("kind") or "").strip().lower()
    return kind == "recover"


def _input_admission_response(pilot, payload):
    if not payload.get('ok') or not isinstance(pilot, PromptQueueMixin):
        return payload
    input_id = payload.get('input_id') or (payload.get('item') or {}).get('input_id')
    if not input_id:
        return payload
    from ..input_receipts import session_input_store
    row = session_input_store(pilot).get(input_id)
    return {**payload, 'input_id': input_id, 'status': row['status'], 'reason': row['reason']}


def _queue_failure_response(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except PromptQueueError as exc:
            return 503, exc.payload()
    return wrapped


def _owned_input_route(fn):
    @wraps(fn)
    def owned(body, svc):
        from ..input_receipts import InputReceiptError
        pilot = svc.get_pilot()
        if pilot is None:
            return fn(body, svc, pilot)
        not_ready = svc.gate_active_pilot_ready()
        if not_ready is not None:
            return 409, not_ready
        pilot = svc.get_pilot()
        if pilot is None:
            return 404, {'error': 'no active session'}
        sid = getattr(pilot, 'harness_session_id', '')
        if body.get('session_id') is not None and body['session_id'] != sid:
            return 409, {'ok': False, 'code': 'session_changed',
                         'error': 'Active session changed. Your queue was not modified.'}
        def validate():
            if (svc.get_pilot() is not pilot
                    or getattr(pilot, 'harness_session_id', '') != sid
                    or (sid and svc.get_runners().get(sid) is not pilot)
                    or (sid and svc.get_sessions is not None and svc.get_sessions().active != sid)):
                raise InputReceiptError('input_session_changed', 'The active session changed; keep your draft.')
        try:
            with input_admission(pilot, svc.pilot_swap_lock, validate):
                return fn(body, svc, pilot)
        except InputReceiptError as exc:
            return (409 if exc.code == 'input_session_changed' else 503), exc.payload()
    return owned


@_queue_failure_response
@_owned_input_route
def post_session_steer(body: dict, svc: SessionControlServices, pilot=None) -> tuple[int, JsonPayload]:
    """POST /api/session/steer."""
    text = body.get("text") or ""
    images = body.get("images") or []
    if isinstance(images, str):
        images = [p for p in images.split("|") if p]
    if not isinstance(text, str) or (not text.strip() and not images and not body.get("documents")):
        return 400, {"error": "missing text"}
    if not svc.get_pilot():
        return 404, {"error": "no active session"}
    if isinstance(pilot, PromptQueueMixin) and not getattr(pilot, "harness_session_id", ""):
        return 409, {"ok": False, "code": "queue_session_unbound", "error": "Open a workspace or pick a project session before using its prompt queue."}
    if body.get("session_id") is not None and body["session_id"] != getattr(pilot, "harness_session_id", ""):
        return 409, {"ok": False, "code": "session_changed", "error": "Active session changed. Your queue was not modified."}
    if not pilot:
        return 404, {"error": "no active session"}
    if isinstance(pilot, PromptQueueMixin):
        pilot._input_upload_root = svc.upload_dir
    valid_imgs, err = _validate_upload_images(images, svc.upload_dir, session=pilot)
    if err is not None:
        return err

    turn_input_mode = body.get("turn_input_mode")
    normalized_turn_mode = None
    if turn_input_mode not in (None, ""):
        normalized_turn_mode = normalize_turn_input_mode(turn_input_mode)
        if normalized_turn_mode is None:
            return 400, {"ok": False, "error": "invalid turn_input_mode"}
    expected_turn_id = _optional_expected_turn_id(body)
    receipt_args = {}
    receipt = None
    if isinstance(pilot, PromptQueueMixin):
        from ..input_receipts import session_input_store
        with input_publication(pilot):
            receipt = session_input_store(pilot).admit(text, original_text=body.get('original_text', ORIGINAL_TEXT_UNSET), images=valid_imgs,
                        documents=body.get('documents'), retry_key=body.get('retry_key'), upload_root=svc.upload_dir)
        receipt_args['input_id'] = receipt['id']
        if receipt['status'] != 'accepted' or receipt['owner_instance'] != session_input_store(pilot).instance:
            return 200, _input_admission_response(pilot, {'ok': True, 'input_id': receipt['id'], 'status': receipt['status'], 'held': True})
    # RecoverTurn is admit(kind=recover) on this same endpoint — not a new UI.
    if _is_recover_turn(body):
        try:
            if hasattr(pilot, "admit_session_action"):
                action = pilot.admit_session_action(
                    ActionKind.RECOVER,
                    text,
                    images=valid_imgs,
                    expected_turn_id=expected_turn_id,
                    **receipt_args,
                )
            else:
                store = _pilot_action_store(pilot)
                if store is None:
                    return 400, {
                        "ok": False,
                        "error": "session lacks action store",
                        "code": "missing_action_store",
                    }
                action = store.admit(
                    ActionKind.RECOVER,
                    text,
                    images=valid_imgs,
                    expected_turn_id=expected_turn_id,
                    **receipt_args,
                )
        except SessionActionIllegalTransition as exc:
            return 400, {"ok": False, "error": str(exc), "code": exc.code}
        return 200, _input_admission_response(pilot, {
            "ok": True,
            "action": "recover",
            **receipt_args,
            "kind": action.kind.value,
        })
    # Optional delivery_mode: when set, route via shared DeliveryMode resolver.
    delivery_mode = body.get("delivery_mode")
    if delivery_mode:
        if normalize_delivery_mode(delivery_mode) is None:
            return 400, {"ok": False, "error": "invalid delivery_mode"}
        busy = _pilot_turn_busy(pilot)
        result = apply_delivery(
            pilot, text, session_busy=busy, requested=delivery_mode, images=valid_imgs,
            **receipt_args,
        )
        code = 200 if result.get("ok") else (503 if str(result.get("code", "")).startswith("queue_") else 400)
        if receipt is not None:
            result["input_id"] = receipt["id"]
        return code, _input_admission_response(pilot, result)
    if normalized_turn_mode is not None:
        busy = _pilot_turn_busy(pilot)
        store = _pilot_action_store(pilot)
        if store is None:
            return 400, {"ok": False, "error": "session lacks action store", "code": "missing_action_store"}
        # START admission and its playlist write publish together. Hold the
        # action lock so a failed write can restore the prior action snapshot
        # without erasing a concurrent steer or Stop.
        delivery_text = text
        if receipt is not None and not valid_imgs:
            delivery_text, _ = session_input_store(pilot).delivery_content(receipt['id'], text)
        convert_images = False
        with input_publication(pilot), getattr(pilot, "_steer_lock", None) or nullcontext():
            previous = store.snapshot()
            try:
                action = store.admit_turn_input(
                    delivery_text, normalized_turn_mode, expected_turn_id=expected_turn_id,
                    idle=not busy, images=valid_imgs, **receipt_args,
                )
                result = {"ok": True, "action": action.kind.value,
                          "kind": action.kind.value, "turn_input_mode": normalized_turn_mode, **receipt_args}
                if action.kind is ActionKind.STEER and valid_imgs and hasattr(pilot, 'steer_with_images'):
                    store.restore(previous)
                    convert_images = True
                if action.kind is ActionKind.START and hasattr(pilot, "enqueue_prompt"):
                    result["item"] = pilot.enqueue_prompt(text, images=valid_imgs, **({"input_id": action.id} if isinstance(pilot, PromptQueueMixin) else {}))
                    if not busy:
                        result["deferred"] = True
            except PromptQueueError:
                store.restore(previous)
                raise
            except SessionActionIllegalTransition as exc:
                return 400, {"ok": False, "error": str(exc), "code": exc.code}
        if convert_images:
            try:
                result['action'] = realized_steer_action(pilot.steer_with_images(
                    text, valid_imgs, **receipt_args, expected_turn_id=expected_turn_id))
            except SessionActionIllegalTransition as exc:
                return 400, {'ok': False, 'code': exc.code, 'error': str(exc)}
        return 200, _input_admission_response(pilot, result)
    if valid_imgs and hasattr(pilot, "steer_with_images"):
        try:
            extra = {'expected_turn_id': expected_turn_id} if expected_turn_id is not None and isinstance(pilot, PromptQueueMixin) else {}
            actual = realized_steer_action(pilot.steer_with_images(text, valid_imgs, **receipt_args, **extra))
        except SessionActionIllegalTransition as exc:
            return 400, {'ok': False, 'code': exc.code, 'error': str(exc)}
        return 200, _input_admission_response(pilot, {"ok": True, "action": actual, **receipt_args})
    if expected_turn_id is not None and hasattr(pilot, "enqueue_steer"):
        try:
            pilot.enqueue_steer(text, expected_turn_id=expected_turn_id, **receipt_args)
        except TypeError:
            pilot.enqueue_steer(text, **receipt_args)
        except SessionActionIllegalTransition as exc:
            return 400, {"ok": False, "error": str(exc), "code": exc.code}
        return 200, _input_admission_response(pilot, {"ok": True, "action": "enqueue_steer", **receipt_args})
    pilot.enqueue_steer(text, **receipt_args)
    return 200, _input_admission_response(pilot, {"ok": True, "action": "enqueue_steer", **receipt_args})


@_queue_failure_response
@_owned_input_route
def post_session_queue(body: dict, svc: SessionControlServices, pilot=None) -> tuple[int, JsonPayload]:
    """POST /api/session/queue."""
    if not svc.get_pilot():
        return 404, {"error": "no active session"}
    if isinstance(pilot, PromptQueueMixin) and not getattr(pilot, "harness_session_id", ""):
        return 409, {"ok": False, "code": "queue_session_unbound", "error": "Open a workspace or pick a project session before using its prompt queue."}
    if body.get("session_id") is not None and body["session_id"] != getattr(pilot, "harness_session_id", ""):
        return 409, {"ok": False, "code": "session_changed", "error": "Active session changed. Your queue was not modified."}
    if not pilot:
        return 404, {"error": "no active session"}
    if body.get('handoff'):
        with input_publication(pilot):
            item = pilot.handoff_prompt(str(body['handoff']))
        if not item:
            return 409, {'ok': False, 'code': 'input_held', 'error': 'No live input is available for handoff.'}
        return 200, _input_admission_response(pilot, {'ok': True, 'item': item})
    if body.get("clear") is True:
        with input_publication(pilot):
            n = pilot.clear_prompts()
        return 200, {"ok": True, "cleared": n}
    rid = (body.get("id") or "").strip() if isinstance(body.get("id"), str) else ""
    if rid:
        with input_publication(pilot):
            ok = pilot.remove_prompt(rid)
        return 200, {"ok": bool(ok), "id": rid}
    text = body.get("text") or ""
    if not isinstance(text, str) or (not text.strip() and not body.get("images") and not body.get("documents")):
        return 400, {"error": "missing text"}
    images = body.get("images") or []
    if isinstance(images, str):
        images = [p for p in images.split("|") if p]
    if isinstance(pilot, PromptQueueMixin):
        pilot._input_upload_root = svc.upload_dir
    valid_imgs, err = _validate_upload_images(images, svc.upload_dir, session=pilot)
    if err is not None:
        return err
    delivery_mode = body.get("delivery_mode")
    if delivery_mode:
        if normalize_delivery_mode(delivery_mode) is None:
            return 400, {"ok": False, "error": "invalid delivery_mode"}
        busy = False
        try:
            busy = bool(pilot.is_turn_busy()) if hasattr(pilot, "is_turn_busy") else False
        except Exception:
            busy = False
        receipt_args = {}
        if isinstance(pilot, PromptQueueMixin):
            from ..input_receipts import session_input_store
            store = session_input_store(pilot)
            with input_publication(pilot):
                receipt = store.admit(text, original_text=body.get('original_text', ORIGINAL_TEXT_UNSET), images=valid_imgs, documents=body.get('documents'),
                                      retry_key=body.get('retry_key'), upload_root=svc.upload_dir)
            if receipt['status'] != 'accepted' or receipt['owner_instance'] != store.instance:
                return 200, _input_admission_response(pilot, {'ok': True, 'input_id': receipt['id'], 'status': receipt['status'], 'held': True})
            receipt_args['input_id'] = receipt['id']
        result = apply_delivery(
            pilot, text, session_busy=busy, requested=delivery_mode, images=valid_imgs, **receipt_args,
        )
        result.update(receipt_args)
        code = 200 if result.get("ok") else (503 if str(result.get("code", "")).startswith("queue_") else 400)
        return code, _input_admission_response(pilot, result)
    try:
        item = pilot.enqueue_prompt(
            text, images=valid_imgs, model=getattr(pilot, "config", svc.cfg).driver,
            **({"original_text": body.get("original_text", ORIGINAL_TEXT_UNSET), "retry_key": body.get("retry_key"), "documents": body.get("documents"), "upload_root": svc.upload_dir} if isinstance(pilot, PromptQueueMixin) else {}),
        )
    except PromptQueueError:
        raise
    except Exception as e:
        return 500, {"ok": False, "error": str(e)}
    if not item or not item.get("id"):
        return 400, {"error": "enqueue failed"}
    return 200, _input_admission_response(pilot, {"ok": True, "item": item})


@_queue_failure_response
def post_session_queue_reorder(
    body: dict, svc: SessionControlServices
) -> tuple[int, JsonPayload]:
    """POST /api/session/queue/reorder."""
    not_ready = svc.gate_active_pilot_ready()
    if not_ready is not None:
        return 409, not_ready
    pilot = svc.get_pilot()
    if isinstance(pilot, PromptQueueMixin) and not getattr(pilot, "harness_session_id", ""):
        return 409, {"ok": False, "code": "queue_session_unbound", "error": "Open a workspace or pick a project session before using its prompt queue."}
    if body.get("session_id") is not None and body["session_id"] != getattr(pilot, "harness_session_id", ""):
        return 409, {"ok": False, "code": "session_changed", "error": "Active session changed. Your queue was not modified."}
    if not pilot:
        return 404, {"error": "no active session"}
    ids = body.get("ids") or []
    if not isinstance(ids, list):
        return 400, {"error": "ids must be a list"}
    items = pilot.reorder_prompts([str(x) for x in ids])
    return 200, {"ok": True, "items": items}


@_queue_failure_response
def get_session_queue(svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """GET /api/session/queue."""
    pilot = svc.get_pilot()
    if isinstance(pilot, PromptQueueMixin) and not getattr(pilot, "harness_session_id", ""):
        return 409, {"ok": False, "code": "queue_session_unbound", "error": "Open a workspace or pick a project session before using its prompt queue."}
    if pilot is not None and not hasattr(pilot, "list_prompts"):
        return 409, {"ok": False, "code": "pilot_not_ready", "error": "No project session is ready yet. Open a workspace or pick a project session."}
    session_id = getattr(pilot, "harness_session_id", "")
    recovery = pilot.prompt_queue_recovery() if hasattr(pilot, "prompt_queue_recovery") else []
    try:
        items = pilot.list_prompts() if pilot else []
    except PromptQueueError as exc:
        return 200, {**exc.payload(), "recovery": recovery, "session_id": session_id}
    try:
        receipts = pilot.input_receipts() if hasattr(pilot, "input_receipts") else []
    except PromptQueueError as exc:
        return 503, exc.payload()
    return 200, {"ok": True, "items": items, "receipts": receipts, "held_items": pilot.held_prompts() if hasattr(pilot, "held_prompts") else [], "recovery": recovery, "session_id": session_id}
