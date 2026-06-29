"""The per-session _busy lock must self-heal: if a previous turn's stream was
abandoned without releasing it (hard crash / unclosed generator), a leaked lock
would otherwise wedge the pilot forever ("stopped doing anything")."""
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
