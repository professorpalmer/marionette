"""Explicit Stop must settle the UI for good: runners report idle, drain still
emits swarm_result badges, but keep-alive pilot_resume is suppressed until the
next real user send."""
from __future__ import annotations

import tempfile
import threading
import time

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.session_runners import SessionRunnerRegistry


def _session():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    return ConversationalSession(cfg)


def test_interrupt_reports_not_busy_while_lock_still_held():
    s = _session()
    s._busy.acquire(blocking=False)
    s._busy_since = time.monotonic()
    s._state = "executing"
    s.interrupt()
    assert s._state == "idle"
    assert s.is_turn_busy() is False
    assert s._stop_holds_idle is True
    assert s._interrupted_swarms is True
    # Abandoned generator still owns the lock -- but status surface is idle.
    assert s._busy.locked()


def test_runners_registry_idle_after_interrupt():
    s = _session()
    s._busy.acquire(blocking=False)
    s._busy_since = time.monotonic()
    s._state = "thinking"
    reg = SessionRunnerRegistry(max_concurrent_sessions=2)
    reg.get_or_create("s1", lambda: s)
    assert reg.status("s1") == "running"
    s.interrupt()
    assert reg.status("s1") == "idle"


def test_drain_after_interrupt_emits_result_without_pilot_resume():
    s = _session()
    s.interrupt()
    s._swarm_results.put({
        "job_id": "stop1",
        "objective": "do a thing",
        "result": {
            "applied": True,
            "files": ["a.py"],
            "summary": "done",
        },
    })
    # Drain needs the busy lock; after interrupt the abandoned turn may still
    # hold it. Release so the poll path can drain (mirrors force-recovery /
    # turn finally).
    if s._busy.locked():
        try:
            s._busy.release()
        except RuntimeError:
            pass
        s._busy_since = 0.0
    events = list(s.drain_swarm_results())
    kinds = [e.kind for e in events]
    assert "swarm_result" in kinds
    assert "pilot_resume" not in kinds
    assert not any(
        m.get("role") == "user" and "[background job" in str(m.get("content") or "")
        for m in s._history
    )


def test_resume_send_refused_while_stop_holds_idle():
    s = _session()
    s.interrupt()
    events = list(s.send("", resume=True))
    assert events == []


def test_new_user_send_clears_stop_hold():
    s = _session()
    s.interrupt()
    # Simulate abandoned lock still held past interrupt grace so send can recover.
    s._busy.acquire(blocking=False)
    s._busy_since = time.monotonic() - 1.0
    s._state = "idle"
    events = list(s.send("hello again"))
    busy = [e for e in events if e.kind == "error" and "busy" in str(e.data.get("error", ""))]
    assert not busy
    assert s._stop_holds_idle is False
    assert s._interrupted_swarms is False
