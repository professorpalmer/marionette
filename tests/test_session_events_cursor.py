"""Tests for harness.api.session_events.read_events_since."""

from __future__ import annotations

from harness.api.session_control import SessionControlServices
from harness.api.session_events import (
    SessionEventStore,
    read_events_since,
    reset_default_store_for_tests,
)
from harness.api.sse import SseEventRing, SseServices


class _FakePilot:
    def __init__(self, state: str = "idle", pending: bool = False):
        self._state = state
        self._pending = pending

    def state(self):
        return self._state

    def has_pending_swarms(self):
        return self._pending

    def session_goal_dict(self):
        return {}


class _FakeRunners:
    def __init__(self, statuses: dict, active_view_id: str = ""):
        self._statuses = statuses
        self.active_view_id = active_view_id

    def statuses(self):
        return dict(self._statuses)


def _sse_svc(rings: dict, default_sid: str = "s1"):
    gens = {sid: ring.generation for sid, ring in rings.items()}

    def ring_lookup(session_id, generation):
        ring = rings.get(session_id)
        if ring is None:
            return None
        if generation is not None and int(generation) != ring.generation:
            return None
        return ring

    return SseServices(
        ring_lookup=ring_lookup,
        current_generation=lambda sid: gens.get(sid),
        default_session_id=lambda: default_sid,
    )


def _sc_svc(runners: dict, state: str = "idle", pending: bool = False):
    pilot = _FakePilot(state=state, pending=pending)
    run = _FakeRunners(runners)
    return SessionControlServices(
        cfg=None,
        get_pilot=lambda: pilot,
        get_runners=lambda: run,
        gate_active_pilot_ready=lambda: None,
        stash_put=lambda *_a, **_k: "",
        save_active_transcript=lambda: None,
        upload_dir="/tmp",
        diag=lambda *_a, **_k: None,
    )


def test_read_events_since_mirrors_ring_and_advances_cursor():
    reset_default_store_for_tests()
    store = SessionEventStore()
    ring = SseEventRing("s1", 1)
    ring.append("message_delta", {"text": "a"})
    ring.append("message_delta", {"text": "b"})
    sse = _sse_svc({"s1": ring})
    sc = _sc_svc({"s1": "running"}, state="thinking")

    code, payload = read_events_since(
        "s1",
        0,
        sse_svc=sse,
        session_control_svc=sc,
        store=store,
    )
    assert code == 200
    assert payload["ok"] is True
    assert payload["cursor"] >= 2
    kinds = [e["kind"] for e in payload["events"]]
    assert "stream" in kinds
    assert "runners" in kinds
    stream = [e for e in payload["events"] if e["kind"] == "stream"]
    assert stream[0]["data"]["kind"] == "message_delta"
    assert stream[0]["data"]["data"]["text"] == "a"

    hw = payload["cursor"]
    code2, payload2 = read_events_since(
        "s1",
        hw,
        sse_svc=sse,
        session_control_svc=sc,
        store=store,
    )
    assert code2 == 200
    assert payload2["events"] == []
    assert payload2["cursor"] == hw


def test_read_events_since_session_switch_isolation():
    """Events for session A must not appear under B's cursor."""
    store = SessionEventStore()
    ring_a = SseEventRing("A", 1)
    ring_a.append("message_delta", {"text": "from-A"})
    ring_b = SseEventRing("B", 1)
    ring_b.append("message_delta", {"text": "from-B"})
    sse = _sse_svc({"A": ring_a, "B": ring_b}, default_sid="A")
    sc = _sc_svc({"A": "running", "B": "idle"})

    _, a_payload = read_events_since(
        "A", 0, sse_svc=sse, session_control_svc=sc, store=store,
    )
    _, b_payload = read_events_since(
        "B", 0, sse_svc=sse, session_control_svc=sc, store=store,
    )
    a_texts = [
        e["data"]["data"].get("text")
        for e in a_payload["events"]
        if e["kind"] == "stream"
    ]
    b_texts = [
        e["data"]["data"].get("text")
        for e in b_payload["events"]
        if e["kind"] == "stream"
    ]
    assert "from-A" in a_texts
    assert "from-B" not in a_texts
    assert "from-B" in b_texts
    assert "from-A" not in b_texts


def test_read_events_since_runners_fingerprint_dedupes():
    store = SessionEventStore()
    sse = _sse_svc({})
    sc = _sc_svc({"s1": "idle"}, state="idle")
    _, p1 = read_events_since(
        "s1", 0, sse_svc=sse, session_control_svc=sc, store=store,
    )
    runners_events = [e for e in p1["events"] if e["kind"] == "runners"]
    assert len(runners_events) == 1
    hw = p1["cursor"]
    _, p2 = read_events_since(
        "s1", hw, sse_svc=sse, session_control_svc=sc, store=store,
    )
    assert p2["events"] == []

    sc2 = _sc_svc({"s1": "running"}, state="thinking")
    _, p3 = read_events_since(
        "s1", hw, sse_svc=sse, session_control_svc=sc2, store=store,
    )
    assert any(e["kind"] == "runners" for e in p3["events"])


def test_store_since_overflow_sets_gap():
    store = SessionEventStore(cap=3)
    for i in range(8):
        store.append("s1", "stream", {"n": i})
    batch = store.since("s1", 2)
    assert batch["gap"] is True
    assert batch["events"] == []
    assert batch["cursor"] == 8


def test_read_events_since_overflow_emits_cursor_gap_miss():
    reset_default_store_for_tests()
    store = SessionEventStore(cap=2)
    for i in range(6):
        store.append("s1", "stream", {"n": i})
    sse = _sse_svc({})
    sc = _sc_svc({})
    code, payload = read_events_since(
        "s1", 1, sse_svc=sse, session_control_svc=sc, store=store,
    )
    assert code == 200
    assert payload["gap"] is True
    misses = [e for e in payload["events"] if e["kind"] == "ring_miss"]
    assert misses
    assert misses[0]["data"]["code"] == "cursor_gap"

    replay = store.since("s1", 0)
    assert replay["gap"] is False
    assert replay["events"]
