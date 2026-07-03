"""The per-session _busy lock must self-heal: if a previous turn's stream was
abandoned without releasing it (hard crash / unclosed generator), a leaked lock
would otherwise wedge the pilot forever ("stopped doing anything")."""
import os
import time
import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def _session():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    return ConversationalSession(cfg)


def test_stale_busy_lock_is_recovered():
    s = _session()
    # Simulate a LEAKED lock: held, but the turn is idle and was acquired long ago.
    s._busy.acquire(blocking=False)
    s._busy_since = time.monotonic() - 5.0  # held for 5s
    s._state = "idle"  # no live stream

    events = list(s.send("hello"))
    # Must NOT be the busy error -- the stale lock should have been recovered and
    # the turn actually run.
    busy = [e for e in events if e.kind == "error" and "busy" in str(e.data.get("error", ""))]
    assert not busy, f"stale lock not recovered: {busy}"


def test_genuinely_busy_lock_still_rejected():
    s = _session()
    # A FRESH lock (just acquired, mid-stream) must still reject re-entry.
    s._busy.acquire(blocking=False)
    s._busy_since = time.monotonic()  # just now
    s._state = "thinking"  # actively streaming

    events = list(s.send("hello"))
    busy = [e for e in events if e.kind == "error" and "busy" in str(e.data.get("error", ""))]
    assert busy, "a genuinely in-flight turn must reject re-entry"


def test_reap_recovers_wedged_nonidle_turn(monkeypatch):
    """The 1.5s idle recovery only fires when state=='idle'. A turn WEDGED mid-call
    (state != idle, hung provider) would otherwise hold _busy forever and starve
    drain_swarm_results. The hard-deadline reaper must recover it (audit #6)."""
    monkeypatch.setenv("HARNESS_TURN_DEADLINE_SECONDS", "1")
    s = _session()
    s._busy.acquire(blocking=False)
    s._mark_busy_acquired()
    s._busy_since = time.monotonic() - 100.0  # wedged well past the deadline
    s._state = "thinking"  # NOT idle -- the case the 1.5s path misses

    assert s._reap_stuck_turn() is True
    # Lock is free again: a fresh acquire succeeds.
    assert s._busy.acquire(blocking=False) is True
    s._busy.release()


def test_reap_leaves_healthy_turn_alone(monkeypatch):
    """A turn within the deadline must never be reaped, even a long one."""
    monkeypatch.setenv("HARNESS_TURN_DEADLINE_SECONDS", "600")
    s = _session()
    s._busy.acquire(blocking=False)
    s._mark_busy_acquired()  # _busy_since = now
    s._state = "thinking"

    assert s._reap_stuck_turn() is False
    # Still held -- a re-acquire must fail.
    assert s._busy.acquire(blocking=False) is False


def test_reaped_turn_release_cannot_steal_a_later_turns_lock(monkeypatch):
    """Generation guard: after turn A is reaped and turn B takes the lock, A's own
    finally (_release_busy) must be a no-op so it cannot free B's lock and break
    the single-writer invariant."""
    monkeypatch.setenv("HARNESS_TURN_DEADLINE_SECONDS", "1")
    s = _session()

    # Turn A acquires and is then wedged + reaped.
    s._busy.acquire(blocking=False)
    gen_a = s._mark_busy_acquired()
    s._busy_since = time.monotonic() - 100.0
    s._state = "thinking"
    assert s._reap_stuck_turn() is True

    # Turn B legitimately takes the freed lock.
    assert s._busy.acquire(blocking=False) is True
    gen_b = s._mark_busy_acquired()
    assert gen_b != gen_a

    # Turn A's delayed finally must NOT release B's lock.
    s._release_busy(gen_a)
    assert s._busy.acquire(blocking=False) is False  # B still holds it

    # B's own release works normally.
    s._release_busy(gen_b)
    assert s._busy.acquire(blocking=False) is True
    s._busy.release()
