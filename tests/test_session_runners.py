"""Unit tests for SessionRunnerRegistry (Phase B slice 1 — no server wiring)."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from harness.session_runners import LeaseExhaustedError, SessionRunnerRegistry


def _busy_runner() -> SimpleNamespace:
    lock = threading.Lock()
    lock.acquire()
    return SimpleNamespace(_busy=lock, _state="executing")


def _idle_runner() -> SimpleNamespace:
    return SimpleNamespace(_busy=threading.Lock(), _state="idle")


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
