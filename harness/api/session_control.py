"""Live-session control HTTP bodies (peeled from ``harness.server``).

Covers stash / interrupt / rewind / steer / prompt-queue, plus persist /
compact / state / context_at / swarm-results and restart-prepare (transcript
flush + resume latch). Process self-terminate for ``POST /api/restart``
stays on Handler.
"""

from __future__ import annotations

import os
import tempfile as _tf
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union


@dataclass
class SessionControlServices:
    """Explicit deps for session-control HTTP handlers."""

    cfg: Any
    get_pilot: Callable[[], Any]
    get_runners: Callable[[], Any]
    gate_active_pilot_ready: Callable[[], Optional[dict]]
    stash_put: Callable[[str, Any], str]
    save_active_transcript: Callable[[], None]
    upload_dir: str
    diag: Callable[..., Any]
    # persist / compact / state / context_at / swarm-results / restart-prepare
    get_sessions: Optional[Callable[[], Any]] = None
    save_transcript: Optional[Callable[..., None]] = None
    set_resume_latch: Optional[Callable[[], None]] = None
    persist_boot_usage: Optional[Callable[..., None]] = None
    consume_resume_pending: Optional[Callable[[bool], bool]] = None
    checkpoint_transcript: Optional[Callable[[], None]] = None
    context_at: Optional[Callable[..., Any]] = None


JsonPayload = Union[dict, list]


def _validate_upload_images(
    images: list, upload_dir: str
) -> tuple[Optional[list], Optional[tuple[int, dict]]]:
    valid_imgs = []
    upload_dir_real = os.path.realpath(upload_dir)
    for p in images:
        if not p:
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
            svc.save_transcript(
                svc.cfg.state_dir or _tf.gettempdir(),
                sessions.active,
                pilot.export_transcript_data(),
            )
        if svc.set_resume_latch is not None:
            svc.set_resume_latch()
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


def post_session_compact(svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """POST /api/session/compact.

    Manual "Compact now": force a compaction attempt and report success ONLY
    when a real ``compaction`` event was emitted (history actually shrank).
    No-ops -- history too small to split, degenerate summary, insufficient
    reduction -- return 409 with ``ok: false`` so the UI can offer a retry
    instead of flashing a false "Compacted".
    """
    not_ready = svc.gate_active_pilot_ready()
    if not_ready is not None:
        return 409, not_ready
    pilot = svc.get_pilot()
    before = pilot._estimate_context_tokens()
    events = list(pilot._maybe_compact_history(force=True))
    compacted = any(_event_kind(ev) == "compaction" for ev in events)
    after = pilot._estimate_context_tokens()
    if not compacted:
        return 409, {
            "ok": False,
            "compacted": False,
            "before_tokens": before,
            "after_tokens": after,
            "error": "no compaction occurred (history too small or summary rejected)",
        }
    sessions = svc.get_sessions() if svc.get_sessions is not None else None
    if sessions is not None and sessions.active and svc.save_transcript is not None:
        svc.save_transcript(
            svc.cfg.state_dir or _tf.gettempdir(),
            sessions.active,
            pilot.export_transcript_data(),
        )
    _record_post_compaction_snapshot(pilot, svc)
    return 200, {
        "ok": True,
        "compacted": True,
        "before_tokens": before,
        "after_tokens": after,
    }


def get_session_state(svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """GET /api/session/state."""
    pilot = svc.get_pilot()
    runners = svc.get_runners()
    state = pilot.state()
    resume_pending = False
    if svc.consume_resume_pending is not None:
        resume_pending = svc.consume_resume_pending(state == "idle")
    return 200, {
        "state": state,
        "pending_swarms": pilot.has_pending_swarms(),
        "resume_pending": resume_pending,
        "runners": runners.statuses(),
        # Active VIEW id so StatusBar can distinguish this session's
        # runner from background sessions still executing under the lease.
        "active_view_id": runners.active_view_id,
    }


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
    if not message and not images:
        return 400, {"error": "missing message"}
    mid = svc.stash_put(message, images)
    return 200, {"id": mid}


def post_session_interrupt(
    body: dict, session_id: str, svc: SessionControlServices
) -> tuple[int, JsonPayload]:
    """POST /api/session/interrupt."""
    sid = (session_id or body.get("session_id") or "").strip()
    if sid:
        target = svc.get_runners().get(sid)
        if target is None:
            return 404, {"ok": False, "error": "session runner not found"}
        target.interrupt()
    else:
        pilot = svc.get_pilot()
        if pilot is not None:
            pilot.interrupt()
    return 200, {"ok": True}


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


def post_session_steer(body: dict, svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """POST /api/session/steer."""
    text = (body.get("text") or "").strip()
    images = body.get("images") or []
    if isinstance(images, str):
        images = [p for p in images.split("|") if p]
    if not text and not images:
        return 400, {"error": "missing text"}
    if not svc.get_pilot():
        return 404, {"error": "no active session"}
    not_ready = svc.gate_active_pilot_ready()
    if not_ready is not None:
        return 409, not_ready
    pilot = svc.get_pilot()
    if not pilot:
        return 404, {"error": "no active session"}
    valid_imgs, err = _validate_upload_images(images, svc.upload_dir)
    if err is not None:
        return err
    if valid_imgs and hasattr(pilot, "steer_with_images"):
        pilot.steer_with_images(text, valid_imgs)
    else:
        pilot.enqueue_steer(text)
    return 200, {"ok": True}


def post_session_queue(body: dict, svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """POST /api/session/queue."""
    if not svc.get_pilot():
        return 404, {"error": "no active session"}
    not_ready = svc.gate_active_pilot_ready()
    if not_ready is not None:
        return 409, not_ready
    pilot = svc.get_pilot()
    if not pilot:
        return 404, {"error": "no active session"}
    if body.get("clear") is True:
        try:
            n = pilot.clear_prompts()
        except Exception:
            n = 0
        return 200, {"ok": True, "cleared": n}
    rid = (body.get("id") or "").strip() if isinstance(body.get("id"), str) else ""
    if rid:
        try:
            ok = pilot.remove_prompt(rid)
        except Exception:
            ok = False
        return 200, {"ok": bool(ok), "id": rid}
    text = (body.get("text") or "").strip()
    if not text:
        return 400, {"error": "missing text"}
    images = body.get("images") or []
    if isinstance(images, str):
        images = [p for p in images.split("|") if p]
    valid_imgs, err = _validate_upload_images(images, svc.upload_dir)
    if err is not None:
        return err
    try:
        item = pilot.enqueue_prompt(
            text, images=valid_imgs, model=svc.cfg.driver,
        )
    except Exception as e:
        return 500, {"error": str(e)}
    if not item or not item.get("id"):
        return 400, {"error": "enqueue failed"}
    return 200, {"ok": True, "item": item}


def post_session_queue_reorder(
    body: dict, svc: SessionControlServices
) -> tuple[int, JsonPayload]:
    """POST /api/session/queue/reorder."""
    pilot = svc.get_pilot()
    if not pilot:
        return 404, {"error": "no active session"}
    ids = body.get("ids") or []
    if not isinstance(ids, list):
        return 400, {"error": "ids must be a list"}
    try:
        items = pilot.reorder_prompts([str(x) for x in ids])
    except Exception:
        try:
            items = pilot.list_prompts()
        except Exception:
            items = []
    return 200, {"ok": True, "items": items}


def get_session_queue(svc: SessionControlServices) -> tuple[int, JsonPayload]:
    """GET /api/session/queue."""
    pilot = svc.get_pilot()
    try:
        items = pilot.list_prompts() if pilot else []
    except Exception:
        items = []
    return 200, {"items": items}
