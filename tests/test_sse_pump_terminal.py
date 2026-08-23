"""sse_pump must emit a terminal error frame when the turn generator raises."""
from __future__ import annotations

import json
from types import SimpleNamespace

from harness.api.sse import SseEventRing, sse_pump, _sse_ring_clear_for_tests


class _Wfile:
    def __init__(self):
        self.chunks = []

    def write(self, payload):
        self.chunks.append(payload)

    def flush(self):
        pass

    def kinds(self):
        out = []
        for raw in self.chunks:
            line = raw.decode()
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[6:].strip())
            out.append(payload.get("kind"))
        return out

    def last_error(self):
        for raw in reversed(self.chunks):
            line = raw.decode()
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[6:].strip())
            if payload.get("kind") == "error":
                return payload
        return None


def _frame(ev):
    frame = {"kind": ev.kind, "data": getattr(ev, "data", {}) or {}}
    if getattr(ev, "turn", None) is not None:
        frame["turn"] = ev.turn
    return ("data: %s\n\n" % json.dumps(frame)).encode()


def _boom_gen():
    yield SimpleNamespace(kind="message_delta", data={"text": "partial"}, turn=0)
    raise RuntimeError("mid-turn boom")


def test_sse_pump_emits_error_and_done_when_generator_raises():
    _sse_ring_clear_for_tests()
    ring = SseEventRing("sess-boom", generation=1, cap=32, ttl=60.0)
    wfile = _Wfile()
    detached = sse_pump(wfile, _boom_gen(), _frame, ring=ring)
    assert detached is False
    assert wfile.kinds() == ["message_delta", "error", "done"]
    err = wfile.last_error()
    assert err["data"]["error"] == "mid-turn boom"
    assert err["data"]["terminal_cause"] == "transport_error"
    kinds = [e["kind"] for e in ring.since(0)["events"]]
    assert kinds == ["message_delta", "error", "done"]
    assert ring.since(0)["events"][1]["cursor"] == 2


def test_sse_pump_write_done_false_still_emits_error_not_done():
    """stream_chat owns framing done; pump still must emit error."""
    _sse_ring_clear_for_tests()
    ring = SseEventRing("sess-chat", generation=1, cap=32, ttl=60.0)
    wfile = _Wfile()
    sse_pump(wfile, _boom_gen(), _frame, write_done=False, ring=ring)
    assert wfile.kinds() == ["message_delta", "error"]
    kinds = [e["kind"] for e in ring.since(0)["events"]]
    assert kinds == ["message_delta", "error"]
    assert "done" not in kinds


def test_sse_pump_clean_path_unchanged():
    _sse_ring_clear_for_tests()
    ring = SseEventRing("sess-ok", generation=1, cap=32, ttl=60.0)
    wfile = _Wfile()

    def _ok():
        yield SimpleNamespace(kind="assistant_done", data={"turns": 1}, turn=0)

    detached = sse_pump(wfile, _ok(), _frame, ring=ring)
    assert detached is False
    assert wfile.kinds() == ["assistant_done", "done"]
    assert [e["kind"] for e in ring.since(0)["events"]] == ["assistant_done", "done"]
