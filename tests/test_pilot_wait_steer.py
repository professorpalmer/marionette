from __future__ import annotations

import threading

from harness.pilot import PilotAction
from harness.pilot_wait import WAIT_SLICE_SEC, dispatch_wait_action
from harness.steer_mixin import SteerMixin


class _Host(SteerMixin):
    """Minimal session: real steer queue, one live background job."""

    def __init__(self) -> None:
        self._steer_lock = threading.Lock()
        self._cancel = threading.Event()
        self.results = []

    def has_pending_swarms(self) -> bool:
        return True

    def _append_action_result(self, act, aid, body, is_native, ok=True):
        self.results.append((aid, body, ok))


class _Clock:
    def __init__(self, on_sleep=None) -> None:
        self.now = 0.0
        self.sleeps = 0
        self.on_sleep = on_sleep

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.sleeps)


def _run_wait(host, clock):
    act = PilotAction(kind="wait", arguments={"seconds": 600})
    events = list(dispatch_wait_action(
        host, act, "a-wait", True,
        sleep=clock.sleep, monotonic=clock.monotonic,
    ))
    return [e for e in events if e.kind == "action_result"]


def test_steer_during_wait_ends_it_within_one_tick_and_stays_queued():
    host = _Host()

    def steer_on_first_tick(n):
        if n == 1:
            host.enqueue_steer("stop and check the logs")

    clock = _Clock(steer_on_first_tick)
    results = _run_wait(host, clock)
    assert clock.sleeps == 1
    assert clock.now <= WAIT_SLICE_SEC
    assert len(results) == 1
    message = results[0].data["message"]
    assert "ended early" in message
    assert "new user message" in message
    assert results[0].data["settled"] is False
    assert host.results == [("a-wait", message, True)]
    assert host.has_pending_steer() is True
    assert host.drain_steer() == ["stop and check the logs"]


def test_no_steer_keeps_the_full_wait():
    host = _Host()
    assert host.has_pending_steer() is False
    clock = _Clock()
    results = _run_wait(host, clock)
    assert clock.now >= 30.0
    message = results[0].data["message"]
    assert "Jobs still running" in message
    assert "ended early" not in message


def test_cancel_wins_over_a_pending_steer():
    host = _Host()

    def cancel_and_steer(n):
        if n == 1:
            host.enqueue_steer("new direction")
            host._cancel.set()

    clock = _Clock(cancel_and_steer)
    results = _run_wait(host, clock)
    assert results[0].data["error"] == "(wait interrupted)"
    assert host.results == [("a-wait", "(wait interrupted)", False)]
    assert host.has_pending_steer() is True


def test_worker_mail_on_the_steer_boundary_keeps_the_wait():
    from harness.session_actions import ActionKind, DeliveryPolicy

    host = _Host()

    def mail_on_first_tick(n):
        if n == 1:
            host._action_store().admit(
                ActionKind.MAILBOX, "job done", delivery=DeliveryPolicy.NEXT_TURN_BOUNDARY,
            )

    clock = _Clock(mail_on_first_tick)
    results = _run_wait(host, clock)
    assert host.has_pending_steer() is False
    assert clock.now >= 30.0
    assert "ended early" not in results[0].data["message"]
