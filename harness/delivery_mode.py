from __future__ import annotations

"""DeliveryMode enum — shared vocabulary for composer inject and schedule→busy.

Modes: auto | steer | follow_up | interrupt
Actions: run_auto | enqueue_steer | enqueue_prompt | interrupt_then_queue
"""

from enum import Enum
from typing import Any, Optional, Sequence, Tuple

from .schedule_core import schedule_fire_prompt
from .session_actions import (
    ActionKind,
    DeliveryPolicy,
    SessionActionIllegalTransition,
    SessionActionStore,
    WakePolicy,
)


class DeliveryMode(str, Enum):
    AUTO = "auto"
    STEER = "steer"
    FOLLOW_UP = "follow_up"
    INTERRUPT = "interrupt"


class DeliveryAction(str, Enum):
    RUN_AUTO = "run_auto"
    ENQUEUE_STEER = "enqueue_steer"
    ENQUEUE_PROMPT = "enqueue_prompt"
    INTERRUPT_THEN_QUEUE = "interrupt_then_queue"


def normalize_delivery_mode(requested: Optional[str]) -> Optional[str]:
    """Return a canonical mode, or None when unset/invalid (caller keeps legacy)."""
    if requested is None:
        return None
    mode = str(requested).strip().lower().replace("-", "_")
    if mode in ("followup",):
        mode = DeliveryMode.FOLLOW_UP.value
    if mode in (
        DeliveryMode.AUTO.value,
        DeliveryMode.STEER.value,
        DeliveryMode.FOLLOW_UP.value,
        DeliveryMode.INTERRUPT.value,
    ):
        return mode
    return None


def _try_interrupt_session(session: Any) -> bool:
    """Call the first available stop/interrupt hook. Never raises."""
    for name in ("interrupt", "request_interrupt", "stop"):
        fn = getattr(session, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass
            return True
    return False


def delivery_mode_action_kinds(requested: Optional[str]) -> Tuple[ActionKind, ...]:
    """Map HTTP DeliveryMode onto SessionAction kinds (domain vocabulary)."""
    mode = normalize_delivery_mode(requested)
    if mode == DeliveryMode.STEER.value:
        return (ActionKind.STEER,)
    if mode == DeliveryMode.FOLLOW_UP.value:
        return (ActionKind.MAILBOX,)
    if mode == DeliveryMode.INTERRUPT.value:
        return (ActionKind.REDIRECT, ActionKind.MAILBOX)
    return ()


def _session_action_store(session: Any) -> Optional[SessionActionStore]:
    store = getattr(session, "_session_actions", None)
    if isinstance(store, SessionActionStore):
        return store
    return None


def _steer_text_already_admitted(session: Any, text: str) -> bool:
    store = _session_action_store(session)
    if store is None:
        return False
    cleaned = (text or "").strip()
    for action in store:
        if action.kind is ActionKind.STEER and action.text == cleaned:
            return True
    return False


def _admit_delivery_kinds(
    session: Any,
    kinds: Sequence[ActionKind],
    text: str,
    images: Optional[list] = None,
) -> None:
    """Record domain actions. Never raises into the HTTP result path."""
    if not kinds:
        return
    admit = getattr(session, "admit_session_action", None)
    store = _session_action_store(session)
    if not callable(admit) and store is None:
        return
    cleaned = (text or "").strip()
    imgs = list(images or [])
    for kind in kinds:
        kwargs = {}
        payload = cleaned
        if kind is ActionKind.REDIRECT:
            payload = ""
            kwargs["delivery"] = DeliveryPolicy.NEXT_TURN_BOUNDARY
            kwargs["wake"] = WakePolicy.ON_ADMIT
        elif kind is ActionKind.MAILBOX:
            kwargs["delivery"] = DeliveryPolicy.WHEN_RUN_IDLE
            kwargs["wake"] = WakePolicy.ON_IDLE
        elif kind is ActionKind.STEER:
            kwargs["delivery"] = DeliveryPolicy.NEXT_TURN_BOUNDARY
            kwargs["wake"] = WakePolicy.NONE
        if imgs:
            kwargs["images"] = imgs
        try:
            if callable(admit):
                admit(kind, payload, **kwargs)
            elif store is not None:
                store.admit(kind, payload, **kwargs)
        except SessionActionIllegalTransition:
            return
        except Exception:
            return


def realized_steer_action(result: Any) -> str:
    """Map ``steer_with_images`` return (or None) to the action taken.

    Vision busy-Enter queues a follow-up (``enqueue_prompt``). Everything
    else, including a missing/legacy ``None`` return, is a mid-turn steer.
    """
    if result == DeliveryAction.ENQUEUE_PROMPT.value:
        return DeliveryAction.ENQUEUE_PROMPT.value
    return DeliveryAction.ENQUEUE_STEER.value


def resolve_delivery(session_busy: bool, requested: Optional[str]) -> str:
    """Map (busy, mode) → action for run_auto / enqueue_steer / enqueue_prompt / interrupt_then_queue.

    ``session_busy`` influences steer (mid-turn inject when busy; when idle,
    steer still maps to enqueue_steer so the host can stage a redirect). Auto
    always maps to run_auto; follow_up always queues a next-turn prompt;
    interrupt stops the current turn when busy, then queues the text.
    """
    mode = normalize_delivery_mode(requested) or DeliveryMode.AUTO.value
    if mode == DeliveryMode.STEER.value:
        return DeliveryAction.ENQUEUE_STEER.value
    if mode == DeliveryMode.FOLLOW_UP.value:
        return DeliveryAction.ENQUEUE_PROMPT.value
    if mode == DeliveryMode.INTERRUPT.value:
        return DeliveryAction.INTERRUPT_THEN_QUEUE.value
    # auto
    _ = session_busy  # reserved for future busy-auto policy; action stays run_auto
    return DeliveryAction.RUN_AUTO.value


def apply_delivery(
    session: Any,
    text: str,
    *,
    session_busy: bool,
    requested: Optional[str] = None,
    images: Optional[list] = None,
) -> dict:
    """Apply resolve_delivery against a live session. Returns a result dict."""
    action = resolve_delivery(session_busy, requested)
    cleaned = (text or "").strip()
    imgs = list(images or [])
    if action == DeliveryAction.ENQUEUE_STEER.value:
        if not cleaned and not imgs:
            return {"ok": False, "error": "missing text", "action": action}
        if imgs and hasattr(session, "steer_with_images"):
            actual = realized_steer_action(session.steer_with_images(cleaned, imgs))
            result = {"ok": True, "action": actual}
            if actual != action:
                result["requested_action"] = action
            return result
        elif hasattr(session, "enqueue_steer"):
            session.enqueue_steer(cleaned)
        else:
            return {"ok": False, "error": "session lacks enqueue_steer", "action": action}
        if not _steer_text_already_admitted(session, cleaned):
            _admit_delivery_kinds(
                session, delivery_mode_action_kinds(requested), cleaned, imgs,
            )
        return {"ok": True, "action": action}
    if action == DeliveryAction.ENQUEUE_PROMPT.value:
        if not cleaned:
            return {"ok": False, "error": "missing text", "action": action}
        if not hasattr(session, "enqueue_prompt"):
            return {"ok": False, "error": "session lacks enqueue_prompt", "action": action}
        item = session.enqueue_prompt(cleaned, images=imgs)
        _admit_delivery_kinds(
            session, delivery_mode_action_kinds(requested), cleaned, imgs,
        )
        return {"ok": True, "action": action, "item": item}
    if action == DeliveryAction.INTERRUPT_THEN_QUEUE.value:
        if not cleaned:
            return {"ok": False, "error": "missing text", "action": action}
        interrupted = False
        if session_busy:
            interrupted = _try_interrupt_session(session)
        if not hasattr(session, "enqueue_prompt"):
            return {"ok": False, "error": "session lacks enqueue_prompt", "action": action}
        # Domain: redirect, then mailbox the text. HTTP still uses the existing
        # interrupt hook + prompt playlist (no second interrupt path).
        _admit_delivery_kinds(
            session, delivery_mode_action_kinds(requested), cleaned, imgs,
        )
        item = session.enqueue_prompt(cleaned, images=imgs)
        result: dict = {"ok": True, "action": action, "item": item}
        if interrupted:
            result["interrupted"] = True
        return result
    # run_auto
    if not cleaned:
        return {"ok": False, "error": "missing text", "action": action}
    if session_busy:
        # Cannot start run_auto on a busy session; stage as follow-up prompt.
        if hasattr(session, "enqueue_prompt"):
            item = session.enqueue_prompt(cleaned, images=imgs)
            return {
                "ok": True,
                "action": DeliveryAction.ENQUEUE_PROMPT.value,
                "requested_action": action,
                "item": item,
                "note": "session busy; queued follow-up instead of run_auto",
            }
        return {"ok": False, "error": "session busy", "action": action}
    run_auto = getattr(session, "run_auto", None)
    if not callable(run_auto):
        return {"ok": False, "error": "session lacks run_auto", "action": action}
    return {"ok": True, "action": action, "deferred": True, "text": cleaned}


def schedule_should_inject(schedule: Any, session_busy: bool) -> bool:
    """True when a schedule opts into busy-session inject via delivery_mode."""
    mode = normalize_delivery_mode(getattr(schedule, "delivery_mode", None))
    return bool(mode) and bool(session_busy)


def deliver_schedule_to_session(
    schedule: Any,
    session: Any,
    *,
    session_busy: bool,
) -> dict:
    """Inject schedule.objective into a busy target session per delivery_mode.

    When delivery_mode is unset, callers keep the legacy spawn + run_auto path.
    """
    mode = normalize_delivery_mode(getattr(schedule, "delivery_mode", None))
    if not mode:
        return {"ok": False, "error": "no delivery_mode", "spawn": True}
    if not session_busy:
        return {"ok": False, "error": "session not busy", "spawn": True}
    objective = schedule_fire_prompt(schedule).strip()
    return apply_delivery(
        session,
        objective,
        session_busy=True,
        requested=mode,
    )
