"""Session /loop — interval vs self_paced, distinct from cron and SessionGoal."""
from __future__ import annotations

import json

import pytest

from harness.session_actions import ActionKind, DeliveryPolicy, SessionActionStore, WakePolicy
from harness.session_loop import (
    LoopMode,
    SessionLoop,
    SessionLoopError,
    fire_session_loop,
    start_session_loop,
    stop_session_loop,
    tick_session_loop,
)


def test_interval_requires_seconds():
    loop = SessionLoop()
    with pytest.raises(SessionLoopError) as exc:
        loop.start("interval", "continue")
    assert exc.value.code == "interval_requires_seconds"
    with pytest.raises(SessionLoopError) as exc:
        loop.start("interval", "continue", interval_seconds=0)
    assert exc.value.code == "interval_requires_seconds"


def test_unknown_mode_and_missing_prompt():
    loop = SessionLoop()
    with pytest.raises(SessionLoopError) as exc:
        loop.start("ralph", "x")
    assert exc.value.code == "unknown_loop_mode"
    with pytest.raises(SessionLoopError) as exc:
        loop.start("self_paced", "   ")
    assert exc.value.code == "missing_prompt"


def test_self_paced_fires_on_idle_edge_once():
    loop = SessionLoop()
    store = SessionActionStore()
    loop.start("self_paced", "next beat", now=10.0)
    assert fire_session_loop(loop, store, idle=False, now=10.5) is None
    action = fire_session_loop(loop, store, idle=True, now=11.0)
    assert action is not None
    assert action.kind is ActionKind.MAILBOX
    assert action.delivery is DeliveryPolicy.WHEN_RUN_IDLE
    assert action.wake is WakePolicy.ON_IDLE
    assert action.text == "next beat"
    assert fire_session_loop(loop, store, idle=True, now=12.0) is None


def test_self_paced_fires_immediately_when_started_idle():
    loop = SessionLoop()
    store = SessionActionStore()
    loop.start(LoopMode.SELF_PACED, "go", now=1.0)
    action = fire_session_loop(loop, store, idle=True, now=1.0)
    assert action is not None
    assert [a.kind for a in store] == [ActionKind.MAILBOX]


def test_interval_waits_then_fires_when_idle():
    loop = SessionLoop()
    store = SessionActionStore()
    loop.start("interval", "tick", interval_seconds=5, now=100.0)
    assert fire_session_loop(loop, store, idle=True, now=103.0) is None
    action = fire_session_loop(loop, store, idle=True, now=105.0)
    assert action is not None
    assert action.text == "tick"
    assert fire_session_loop(loop, store, idle=True, now=106.0) is None
    later = fire_session_loop(loop, store, idle=True, now=110.0)
    assert later is not None


def test_interval_does_not_fire_while_busy():
    loop = SessionLoop()
    store = SessionActionStore()
    loop.start("interval", "tick", interval_seconds=1, now=0.0)
    assert fire_session_loop(loop, store, idle=False, now=5.0) is None
    assert list(store) == []


def test_snapshot_restore_is_json_safe():
    loop = SessionLoop()
    loop.start("interval", "again", interval_seconds=2.5, until=99.0, now=9.0)
    encoded = json.dumps(loop.to_dict())
    other = SessionLoop.from_dict(json.loads(encoded))
    assert other.enabled is True
    assert other.mode is LoopMode.INTERVAL
    assert other.prompt == "again"
    assert other.interval_seconds == 2.5
    assert other.until == 99.0
    assert other.started_at == 9.0


def test_until_deadline_stops_the_loop():
    loop = SessionLoop()
    store = SessionActionStore()
    loop.start("interval", "tick", interval_seconds=1, until=104.0, now=100.0)
    assert fire_session_loop(loop, store, idle=True, now=101.0) is not None
    assert fire_session_loop(loop, store, idle=True, now=104.0) is None
    assert loop.enabled is False


def test_repeated_response_digest_stops_the_loop():
    loop = SessionLoop()
    store = SessionActionStore()
    loop.start("self_paced", "next beat", now=1.0)
    assert loop.note_response("same answer") is True
    assert loop.note_response("same answer") is False
    assert loop.enabled is False
    assert fire_session_loop(loop, store, idle=True, now=2.0) is None


def test_tick_session_loop_enqueues_prompt_and_stop_disables():
    class Host:
        def __init__(self) -> None:
            self._loop_state = SessionLoop()
            self._session_actions = SessionActionStore()
            self.prompts = []

        def enqueue_prompt(self, text, images=None, model=None):
            self.prompts.append(text)
            return {"text": text}

    host = Host()
    start_session_loop(host, "self_paced", "again", now=1.0)
    assert tick_session_loop(host, idle=True, now=1.0) is True
    assert host.prompts == ["again"]
    stop_session_loop(host)
    assert tick_session_loop(host, idle=True, now=2.0) is False
    assert host.prompts == ["again"]
