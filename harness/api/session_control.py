"""Live-session control HTTP bodies (peeled from ``harness.server``).

Covers stash / interrupt / rewind / steer / prompt-queue. Persist / restart /
compact stay on Handler (resume latch + process lifecycle).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Union


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
    pilot = svc.get_pilot()
    if not pilot:
        return 404, {"ok": False, "error": "no active session"}
    not_ready = svc.gate_active_pilot_ready()
    if not_ready is not None:
        return 409, not_ready
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
    pilot = svc.get_pilot()
    if not pilot:
        return 404, {"ok": False, "error": "no active session"}
    not_ready = svc.gate_active_pilot_ready()
    if not_ready is not None:
        return 409, not_ready
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
    pilot = svc.get_pilot()
    if not pilot:
        return 404, {"error": "no active session"}
    not_ready = svc.gate_active_pilot_ready()
    if not_ready is not None:
        return 409, not_ready
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
    pilot = svc.get_pilot()
    if not pilot:
        return 404, {"error": "no active session"}
    not_ready = svc.gate_active_pilot_ready()
    if not_ready is not None:
        return 409, not_ready
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
