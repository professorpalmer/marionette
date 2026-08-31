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


def test_sse_pump_writes_before_blocking_on_event():
    """Live SSE must not wait on checkpoint/on_event (Still working hang)."""
    _sse_ring_clear_for_tests()
    ring = SseEventRing("sess-write-first", generation=1, cap=32, ttl=60.0)
    wfile = _Wfile()
    seen = {"kinds_at_on_event": None}

    def on_event(ev):
        seen["kinds_at_on_event"] = list(wfile.kinds())
        if ev.kind == "assistant_done" and not wfile.kinds():
            raise AssertionError("assistant_done checkpoint ran before the SSE write")

    def _ok():
        yield SimpleNamespace(kind="message_delta", data={"text": "hi"}, turn=0)
        yield SimpleNamespace(kind="assistant_done", data={"turns": 1}, turn=0)

    detached = sse_pump(wfile, _ok(), _frame, on_event=on_event, ring=ring)
    assert detached is False
    assert wfile.kinds() == ["message_delta", "assistant_done", "done"]
    assert seen["kinds_at_on_event"][-1] == "assistant_done"


def test_stream_chat_writes_resume_before_done(monkeypatch):
    from harness.api.sse import _sse_ring_clear_for_tests
    from harness.api.streams import StreamServices, stream_chat
    _sse_ring_clear_for_tests()

    written = []

    class _Wfile:
        def write(self, payload):
            written.append(payload)
            return len(payload)

        def flush(self):
            return None

    class _Handler:
        def __init__(self):
            self.wfile = _Wfile()

        def send_response(self, *_a, **_k):
            return None

        def send_header(self, *_a, **_k):
            return None

        def end_headers(self):
            return None

        def _cors(self):
            return None

    class _Pilot:
        harness_session_id = "s-resume"

        def send(self, *_a, **_k):
            yield SimpleNamespace(kind="assistant_done", data={"text": "hi"})

        def drain_swarm_results(self):
            yield SimpleNamespace(kind="swarm_result", data={"ok": True})
            yield SimpleNamespace(kind="pilot_resume", data={"ok": True})

    monkeypatch.setattr("harness.hooks.run_hooks", lambda *_a, **_k: None)
    svc = StreamServices(
        cfg=SimpleNamespace(repo=""),
        sessions=SimpleNamespace(active="s-resume", set_title_if_default=lambda *_a: None),
        get_pilot=lambda: _Pilot(),
        get_session=lambda: None,
        ensure_pilot_matches_driver=lambda: None,
        maybe_refresh_codegraph=lambda *_a: None,
        pilot_preflight=lambda: None,
        checkpoint_transcript=lambda *_a: None,
        finalize_turn=lambda *_a: None,
        upload_dir="/tmp",
        auto_budget_from_env=lambda: None,
    )
    stream_chat(_Handler(), "hello", None, svc)
    kinds = []
    for raw in written:
        line = raw.decode()
        if not line.startswith("data: "):
            continue
        kinds.append(json.loads(line[6:].strip()).get("kind"))
    assert "swarm_result" in kinds
    assert "pilot_resume" in kinds
    assert "done" in kinds
    assert kinds.index("swarm_result") < kinds.index("done")
    assert kinds.index("pilot_resume") < kinds.index("done")
    assert kinds[-1] == "done"
