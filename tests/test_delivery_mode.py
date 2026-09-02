"""DeliveryMode — auto | steer | follow_up | interrupt shared vocabulary."""
from __future__ import annotations

from harness.delivery_mode import (
    DeliveryAction,
    DeliveryMode,
    apply_delivery,
    deliver_schedule_to_session,
    delivery_mode_action_kinds,
    normalize_delivery_mode,
    realized_steer_action,
    resolve_delivery,
    schedule_should_inject,
)
from harness.schedule_core import Schedule
from harness.session_actions import ActionKind, DeliveryPolicy, SessionActionStore


class _FakeSession:
    def __init__(self):
        self.steers = []
        self.prompts = []
        self.auto_calls = []
        self._busy = False

    def is_turn_busy(self):
        return self._busy

    def enqueue_steer(self, text):
        self.steers.append(text)

    def enqueue_prompt(self, text, images=None, model=None):
        item = {"id": "p1", "text": text, "images": images or [], "model": model or ""}
        self.prompts.append(item)
        return item

    def run_auto(self, objective, budget=None):
        self.auto_calls.append(objective)
        return iter(())


class _FakeSessionWithInterrupt(_FakeSession):
    def __init__(self):
        super().__init__()
        self.interrupts = []

    def interrupt(self):
        self.interrupts.append(True)


def test_delivery_mode_steer_when_busy():
    assert resolve_delivery(True, "steer") == DeliveryAction.ENQUEUE_STEER.value
    session = _FakeSession()
    session._busy = True
    result = apply_delivery(session, "nudge left", session_busy=True, requested="steer")
    assert result["ok"] is True
    assert result["action"] == "enqueue_steer"
    assert session.steers == ["nudge left"]


def test_realized_steer_action_maps_vision_queue():
    assert realized_steer_action("enqueue_prompt") == "enqueue_prompt"
    assert realized_steer_action("enqueue_steer") == "enqueue_steer"
    assert realized_steer_action(None) == "enqueue_steer"
    assert realized_steer_action("") == "enqueue_steer"


def test_delivery_mode_steer_vision_images_reports_queue():
    """Requested steer + native-vision images must report enqueue_prompt."""

    class _VisionSession(_FakeSession):
        def steer_with_images(self, text, images=None):
            self.enqueue_prompt(text or "(see attached image)", images=images)
            return "enqueue_prompt"

    session = _VisionSession()
    result = apply_delivery(
        session,
        "look at this",
        session_busy=True,
        requested="steer",
        images=["/tmp/shot.png"],
    )
    assert result["ok"] is True
    assert result["action"] == "enqueue_prompt"
    assert result["requested_action"] == "enqueue_steer"
    assert session.steers == []
    assert session.prompts[0]["text"] == "look at this"


def test_delivery_mode_follow_up_queues():
    assert resolve_delivery(True, "follow_up") == DeliveryAction.ENQUEUE_PROMPT.value
    session = _FakeSession()
    result = apply_delivery(
        session, "next turn please", session_busy=True, requested="follow_up",
    )
    assert result["ok"] is True
    assert result["action"] == "enqueue_prompt"
    assert session.prompts[0]["text"] == "next turn please"


def test_delivery_mode_auto_path():
    assert resolve_delivery(False, "auto") == DeliveryAction.RUN_AUTO.value
    assert normalize_delivery_mode("AUTO") == DeliveryMode.AUTO.value
    session = _FakeSession()
    result = apply_delivery(session, "go", session_busy=False, requested="auto")
    assert result["ok"] is True
    assert result["action"] == "run_auto"
    assert result.get("deferred") is True


def test_schedule_busy_session_honors_delivery_mode():
    schedule = Schedule(
        id="abc",
        name="busy-inject",
        objective="Keep polishing the PR",
        cron="0 * * * *",
        delivery_mode="steer",
    )
    session = _FakeSession()
    session._busy = True
    assert schedule_should_inject(schedule, True) is True
    delivered = deliver_schedule_to_session(schedule, session, session_busy=True)
    assert delivered["ok"] is True
    assert delivered["action"] == "enqueue_steer"
    assert session.steers == ["Keep polishing the PR"]

    # Unset delivery_mode keeps legacy spawn behavior (no inject).
    legacy = Schedule(
        id="def",
        name="legacy",
        objective="spawn me",
        cron="0 * * * *",
        delivery_mode="",
    )
    assert schedule_should_inject(legacy, True) is False
    assert deliver_schedule_to_session(legacy, session, session_busy=True)["spawn"] is True


def test_delivery_mode_follow_up_on_schedule_busy():
    schedule = Schedule(
        id="ghi",
        name="follow",
        objective="queue this",
        cron="0 * * * *",
        delivery_mode="follow_up",
    )
    session = _FakeSession()
    session._busy = True
    delivered = deliver_schedule_to_session(schedule, session, session_busy=True)
    assert delivered["action"] == "enqueue_prompt"
    assert session.prompts[0]["text"] == "queue this"


def test_delivery_mode_interrupt_when_busy():
    assert resolve_delivery(True, "interrupt") == DeliveryAction.INTERRUPT_THEN_QUEUE.value
    session = _FakeSessionWithInterrupt()
    session._busy = True
    result = apply_delivery(
        session, "stop and do this", session_busy=True, requested="interrupt",
    )
    assert result["ok"] is True
    assert result["action"] == "interrupt_then_queue"
    assert result.get("interrupted") is True
    assert session.interrupts == [True]
    assert session.prompts[0]["text"] == "stop and do this"


def test_delivery_mode_interrupt_when_idle():
    assert resolve_delivery(False, "interrupt") == DeliveryAction.INTERRUPT_THEN_QUEUE.value
    session = _FakeSession()
    session._busy = False
    result = apply_delivery(
        session, "just queue", session_busy=False, requested="interrupt",
    )
    assert result["ok"] is True
    assert result["action"] == "interrupt_then_queue"
    assert "interrupted" not in result
    assert session.prompts[0]["text"] == "just queue"
    assert normalize_delivery_mode("interrupt") == DeliveryMode.INTERRUPT.value


def test_delivery_mode_maps_to_session_action_kinds():
    assert delivery_mode_action_kinds("steer") == (ActionKind.STEER,)
    assert delivery_mode_action_kinds("follow_up") == (ActionKind.MAILBOX,)
    assert delivery_mode_action_kinds("interrupt") == (
        ActionKind.REDIRECT,
        ActionKind.MAILBOX,
    )
    assert delivery_mode_action_kinds("auto") == ()


def test_delivery_mode_steer_admits_kind_steer():
    session = _FakeSession()
    session._session_actions = SessionActionStore()
    session._busy = True
    result = apply_delivery(session, "nudge left", session_busy=True, requested="steer")
    assert result["ok"] is True
    assert result["action"] == "enqueue_steer"
    assert session.steers == ["nudge left"]
    admitted = list(session._session_actions)
    assert [a.kind for a in admitted] == [ActionKind.STEER]
    assert admitted[0].delivery is DeliveryPolicy.NEXT_TURN_BOUNDARY


def test_delivery_mode_follow_up_admits_mailbox_when_run_idle():
    session = _FakeSession()
    session._session_actions = SessionActionStore()
    result = apply_delivery(
        session, "next turn please", session_busy=True, requested="follow_up",
    )
    assert result["ok"] is True
    assert result["action"] == "enqueue_prompt"
    assert session.prompts[0]["text"] == "next turn please"
    admitted = list(session._session_actions)
    assert [a.kind for a in admitted] == [ActionKind.MAILBOX]
    assert admitted[0].delivery is DeliveryPolicy.WHEN_RUN_IDLE


def test_delivery_mode_interrupt_admits_redirect_then_mailbox():
    session = _FakeSessionWithInterrupt()
    session._session_actions = SessionActionStore()
    session._busy = True
    result = apply_delivery(
        session, "stop and do this", session_busy=True, requested="interrupt",
    )
    assert result["ok"] is True
    assert result["action"] == "interrupt_then_queue"
    assert session.interrupts == [True]
    assert session.prompts[0]["text"] == "stop and do this"
    admitted = list(session._session_actions)
    assert [a.kind for a in admitted] == [ActionKind.REDIRECT, ActionKind.MAILBOX]
    assert admitted[1].text == "stop and do this"
    assert admitted[1].delivery is DeliveryPolicy.WHEN_RUN_IDLE
