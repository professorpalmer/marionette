"""Post-turn distill/wiki must not flip runners=running / Still working."""
from __future__ import annotations

import tempfile
import threading
import time

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.session_runners import SessionRunnerRegistry, _is_busy


def test_submit_housekeeping_does_not_count_as_pending_swarm():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    started = threading.Event()
    release = threading.Event()

    def _block(*_a):
        started.set()
        release.wait(timeout=5)

    assert s._submit_housekeeping(_block) is True
    assert started.wait(timeout=2), "housekeeping thread did not start"
    assert s.has_pending_swarms() is False
    assert s.state() != "awaiting_swarm"
    assert _is_busy(s) is False

    reg = SessionRunnerRegistry(max_concurrent_sessions=2)
    reg.get_or_create("s1", lambda: s)
    assert reg.status("s1") == "idle"

    release.set()


def test_submit_swarm_still_counts_as_pending():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    release = threading.Event()

    def _block(*_a):
        release.wait(timeout=5)

    assert s._submit_swarm(_block) is True
    # Give the pool a tick to register the future.
    deadline = time.time() + 2
    while time.time() < deadline and not s.has_pending_swarms():
        time.sleep(0.01)
    assert s.has_pending_swarms() is True
    release.set()
    deadline = time.time() + 2
    while time.time() < deadline and s.has_pending_swarms():
        time.sleep(0.01)
    assert s.has_pending_swarms() is False


def test_assistant_done_before_slow_ingest(monkeypatch):
    """UI must see assistant_done before a slow wiki ingest runs."""
    from pmharness.drivers.openai_compat import DriverResponse

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    order: list[str] = []

    def _slow_ingest(*_a, **_k):
        order.append("ingest")
        time.sleep(0.05)

    monkeypatch.setattr(s, "_maybe_ingest", _slow_ingest)
    monkeypatch.setattr(
        s, "_submit_housekeeping",
        lambda fn, *a, **k: (order.append("housekeep"), fn(*a, **k), True)[2],
    )

    class _Done:
        name = "done"

        def complete(self, prompt, *, system=None, tools=None):
            return DriverResponse(
                text='{"say":"All set.","actions":[]}',
                tokens_out=4,
                latency_ms=1.0,
            )

    s.pilot = _Done()
    kinds = []
    for ev in s.send("hi"):
        kinds.append(ev.kind)
        if ev.kind == "assistant_done":
            order.append("done")

    assert "assistant_done" in kinds
    assert order.index("done") < order.index("ingest")
