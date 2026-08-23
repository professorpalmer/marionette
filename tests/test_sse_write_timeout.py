"""SSE write timeout must detach slow clients without stalling the pump."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

from harness.api.sse import SseEventRing, _sse_ring_clear_for_tests, sse_pump, sse_write


class _BlockingWfile:
    """Blocks on write after the first successful frame."""

    def __init__(self) -> None:
        self.writes = 0

    def write(self, _payload: bytes) -> None:
        self.writes += 1
        if self.writes > 1:
            time.sleep(5.0)

    def flush(self) -> None:
        if self.writes > 1:
            time.sleep(5.0)


def test_sse_write_returns_false_when_write_blocks_past_deadline(monkeypatch):
    monkeypatch.setattr("harness.api.sse._SSE_WRITE_TIMEOUT_S", 0.05)
    wfile = _BlockingWfile()
    assert sse_write(wfile, b"data: first\n\n") is True
    assert sse_write(wfile, b"data: second\n\n") is False


def test_sse_pump_continues_and_ring_appends_after_slow_client_detach(monkeypatch):
    monkeypatch.setattr("harness.api.sse._SSE_WRITE_TIMEOUT_S", 0.05)
    _sse_ring_clear_for_tests()
    ring = SseEventRing("sess-slow", generation=1, cap=32, ttl=60.0)
    wfile = _BlockingWfile()

    events = [
        SimpleNamespace(kind="token", data={"t": "a"}, turn=1),
        SimpleNamespace(kind="token", data={"t": "b"}, turn=1),
        SimpleNamespace(kind="assistant_done", data={}, turn=1),
    ]

    detached = sse_pump(
        wfile,
        iter(events),
        lambda ev: ("data: %s\n\n" % json.dumps({"kind": ev.kind})).encode(),
        ring=ring,
    )
    assert detached is True
    kinds = [e["kind"] for e in ring.since(0)["events"]]
    assert kinds.count("token") == 2
    assert "assistant_done" in kinds
    assert "done" in kinds
