from __future__ import annotations

from types import SimpleNamespace

from harness.pilot import PilotAction, PilotError, from_wire
from harness.pilot_wait import (
    dispatch_wait_action,
    format_wait_status,
    keep_alive_wait_slice,
    note_keep_alive_wait,
    parse_wait_seconds,
    pending_jobs_keep_alive,
)


def test_parse_wait_seconds_clamps_and_falls_back():
    assert parse_wait_seconds(2) == 2.0
    assert parse_wait_seconds(0) == 0.1
    assert parse_wait_seconds(99) == 30.0
    assert parse_wait_seconds("nope") == 2.0
    assert parse_wait_seconds(None) == 2.0


def test_from_wire_wait_and_validate():
    act = from_wire("wait", {"seconds": 3})
    assert act.kind == "wait"
    assert act.arguments.get("seconds") == 3
    try:
        PilotAction(kind="wait", arguments={"seconds": "nope"}).validate()
    except PilotError:
        pass
    else:
        raise AssertionError("invalid wait seconds must raise")


def test_pending_jobs_keep_alive():
    assert pending_jobs_keep_alive(SimpleNamespace()) is False
    assert pending_jobs_keep_alive(
        SimpleNamespace(has_pending_swarms=lambda: True)
    ) is True
    q = SimpleNamespace(empty=lambda: False)
    assert pending_jobs_keep_alive(SimpleNamespace(_swarm_results=q)) is False
    cancel = SimpleNamespace(is_set=lambda: True)
    assert pending_jobs_keep_alive(
        SimpleNamespace(has_pending_swarms=lambda: True, _cancel=cancel)
    ) is False


def test_keep_alive_wait_slice_is_instant_with_fake_clock():
    times = [0.0]

    def mono():
        return times[0]

    def sleep(dt):
        times[0] += dt

    session = SimpleNamespace(
        has_pending_swarms=lambda: times[0] < 1.5,
        drain_swarm_results=lambda **_k: iter(()),
        _cancel=SimpleNamespace(is_set=lambda: False),
    )
    events = list(keep_alive_wait_slice(
        session, seconds=2.0, sleep=sleep, monotonic=mono,
    ))
    kinds = [ev.kind for ev in events]
    assert "notice" in kinds
    assert any(ev.data.get("kind") == "wait" for ev in events)


def test_dispatch_wait_action_returns_status():
    times = [0.0]

    def mono():
        return times[0]

    def sleep(dt):
        times[0] += max(dt, 0.1)

    appended = []
    session = SimpleNamespace(
        has_pending_swarms=lambda: False,
        drain_swarm_results=lambda **_k: iter(()),
        _cancel=SimpleNamespace(is_set=lambda: False),
        _append_action_result=lambda *a, **k: appended.append((a, k)),
    )
    act = from_wire("wait", {"seconds": 1})
    events = list(dispatch_wait_action(
        session, act, "w1", True, sleep=sleep, monotonic=mono,
    ))
    assert events[-1].kind == "action_result"
    assert events[-1].data.get("settled") is True
    assert appended


def test_format_wait_status_settled():
    session = SimpleNamespace(has_pending_swarms=lambda: False)
    text = format_wait_status(session, 2.0, True)
    assert "settled" in text.lower()


def test_note_keep_alive_wait_caps():
    session = SimpleNamespace()
    assert note_keep_alive_wait(session) is True
    session._keep_alive_waits = 90
    assert note_keep_alive_wait(session) is False

