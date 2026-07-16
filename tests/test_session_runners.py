"""Unit tests for SessionRunnerRegistry (Phase B slice 1 — no server wiring)."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from harness.session_runners import (
    LeaseExhaustedError,
    SessionRunnerRegistry,
    _is_busy,
    build_lease_exhausted_payload,
)


def _busy_runner() -> SimpleNamespace:
    lock = threading.Lock()
    lock.acquire()
    return SimpleNamespace(_busy=lock, _state="executing")


def _idle_runner() -> SimpleNamespace:
    return SimpleNamespace(_busy=threading.Lock(), _state="idle")


def _awaiting_swarm_runner() -> SimpleNamespace:
    """Mirrors ConversationalSession: _state idle, turn free, swarms pending."""
    return SimpleNamespace(
        _busy=threading.Lock(),
        _state="idle",
        is_turn_busy=lambda: False,
        has_pending_swarms=lambda: True,
        state=lambda: "awaiting_swarm",
        _swarm_futures={"fut-1"},
    )


def test_two_runners_both_busy_one_active_view():
    reg = SessionRunnerRegistry(max_concurrent_sessions=3)
    a = reg.get_or_create("a", _busy_runner)
    b = reg.get_or_create("b", _busy_runner)
    reg.set_active_view("a")

    assert len(reg) == 2
    assert set(reg.ids()) == {"a", "b"}
    assert reg.get("a") is a
    assert reg.get("b") is b
    assert reg.active_view_id == "a"
    assert reg.status("a") == "running"
    assert reg.status("b") == "running"
    assert reg.statuses() == {"a": "running", "b": "running"}

    reg.set_active_view("b")
    assert reg.active_view_id == "b"
    assert reg.get("a") is a  # other busy runner retained


def test_lease_eviction_drops_idle_when_over_cap():
    reg = SessionRunnerRegistry(max_concurrent_sessions=2)
    reg.get_or_create("idle-old", _idle_runner)
    reg.get_or_create("busy", _busy_runner)
    assert len(reg) == 2

    created = reg.get_or_create("new", _idle_runner)
    assert created is not None
    assert "idle-old" not in reg.ids()
    assert reg.get("idle-old") is None
    assert reg.get("busy") is not None
    assert reg.get("new") is created
    assert len(reg) == 2


def test_lease_exhausted_when_all_slots_busy():
    reg = SessionRunnerRegistry(max_concurrent_sessions=2)
    reg.get_or_create("a", _busy_runner)
    reg.get_or_create("b", _busy_runner)

    with pytest.raises(LeaseExhaustedError):
        reg.get_or_create("c", _idle_runner)

    assert set(reg.ids()) == {"a", "b"}
    assert reg.get("c") is None


def test_drop_removes_runner():
    reg = SessionRunnerRegistry(max_concurrent_sessions=3)
    reg.get_or_create("x", _idle_runner)
    reg.set_active_view("x")
    assert reg.status("x") == "idle"

    reg.drop("x")
    assert reg.get("x") is None
    assert "x" not in reg.ids()
    assert len(reg) == 0
    assert reg.status("x") == "missing"


def test_drop_on_drop_callback_and_notify_false():
    seen = []

    def on_drop(sid, runner):
        seen.append((sid, runner))

    reg = SessionRunnerRegistry(max_concurrent_sessions=3, on_drop=on_drop)
    r = reg.get_or_create("x", _idle_runner)
    dropped = reg.drop("x")
    assert dropped is r
    assert seen == [("x", r)]

    seen.clear()
    r2 = reg.get_or_create("y", _idle_runner)
    reg.drop("y", notify=False)
    assert seen == []
    assert reg.get("y") is None
    assert r2 is not None
    assert reg.active_view_id is None


def test_awaiting_swarm_reports_running_via_statuses():
    """Pending swarms must not look idle: _state can stay idle while swarms run."""
    reg = SessionRunnerRegistry(max_concurrent_sessions=3)
    reg.get_or_create("swarm", _awaiting_swarm_runner)
    assert reg.status("swarm") == "running"
    assert reg.statuses() == {"swarm": "running"}


def test_is_busy_pending_swarms_despite_idle_turn_and_state():
    runner = _awaiting_swarm_runner()
    assert _is_busy(runner) is True


def test_is_busy_awaiting_swarm_via_state_when_has_pending_missing():
    """state() == awaiting_swarm is enough when has_pending_swarms is absent."""
    runner = SimpleNamespace(
        _busy=threading.Lock(),
        _state="idle",
        is_turn_busy=lambda: False,
        state=lambda: "awaiting_swarm",
    )
    assert _is_busy(runner) is True


def test_is_busy_swarm_futures_attr_fallback():
    """Non-empty _swarm_futures counts as busy without public swarm helpers."""
    runner = SimpleNamespace(
        _busy=threading.Lock(),
        _state="idle",
        is_turn_busy=lambda: False,
        _swarm_futures={"a"},
    )
    assert _is_busy(runner) is True


def test_is_busy_stop_holds_idle_still_idle_without_swarms():
    """is_turn_busy False must not resurrect via locked _busy (Stop path)."""
    lock = threading.Lock()
    lock.acquire()
    runner = SimpleNamespace(
        _busy=lock,
        _state="thinking",
        is_turn_busy=lambda: False,
        has_pending_swarms=lambda: False,
    )
    assert _is_busy(runner) is False


def test_is_busy_stop_holds_idle_but_pending_swarm_still_busy():
    lock = threading.Lock()
    lock.acquire()
    runner = SimpleNamespace(
        _busy=lock,
        _state="thinking",
        is_turn_busy=lambda: False,
        has_pending_swarms=lambda: True,
    )
    assert _is_busy(runner) is True


def test_awaiting_swarm_runner_not_evicted_as_idle():
    """Lease eviction must retain swarm-waiting runners (they are busy)."""
    reg = SessionRunnerRegistry(max_concurrent_sessions=2)
    reg.get_or_create("swarm", _awaiting_swarm_runner)
    reg.get_or_create("busy", _busy_runner)

    with pytest.raises(LeaseExhaustedError):
        reg.get_or_create("new", _idle_runner)

    assert set(reg.ids()) == {"swarm", "busy"}


def test_build_lease_exhausted_payload_includes_capacity_and_busy_ids():
    reg = SessionRunnerRegistry(max_concurrent_sessions=2)
    reg.get_or_create("a", _busy_runner)
    reg.get_or_create("b", _busy_runner)
    payload = build_lease_exhausted_payload(
        reg,
        error="session runner lease exhausted: all concurrent sessions are busy",
        titles_by_id={"a": "Alpha", "b": "Beta"},
    )
    assert payload["code"] == "lease_exhausted"
    assert payload["ok"] is False
    assert payload["max_concurrent"] == 2
    assert payload["active_count"] == 2
    assert payload["busy_session_ids"] == ["a", "b"]
    assert payload["busy_session_titles"] == ["Alpha", "Beta"]
    assert "lease exhausted" in payload["error"].lower()


def test_defer_building_status_is_attaching_not_running():
    """New Session must not flash composer thinking during cold attach."""
    from harness.deferred_attach import DeferredPilotPlaceholder

    ph = DeferredPilotPlaceholder(session_id="s-attach", state_dir="/tmp", transcript=[])
    reg = SessionRunnerRegistry(max_concurrent_sessions=4)
    reg.get_or_create("s-attach", lambda: ph)
    assert reg.status("s-attach") == "attaching"
    assert _is_busy(ph) is True


def test_defer_building_placeholder_counts_as_busy_for_lease():
    """Building deferred shells must not look idle in statuses / busy lists."""
    from harness.deferred_attach import DeferredPilotPlaceholder

    ph = DeferredPilotPlaceholder(session_id="build", state_dir="/tmp", transcript=[])
    assert _is_busy(ph) is True
    assert ph.state() == "building"

    reg = SessionRunnerRegistry(max_concurrent_sessions=1)
    reg.get_or_create("build", lambda: ph)
    assert reg.status("build") == "attaching"
    payload = build_lease_exhausted_payload(reg, titles_by_id={"build": "Building"})
    assert payload["busy_session_ids"] == ["build"]
    assert payload["busy_session_titles"] == ["Building"]

    with pytest.raises(LeaseExhaustedError):
        reg.get_or_create("other", _idle_runner)
