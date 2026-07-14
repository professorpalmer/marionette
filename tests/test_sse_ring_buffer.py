"""Hermetic tests for mid-turn SSE reattach ring-buffer retain/replay.

Python 3.9 safe. Does not touch the live EventSource path in test_streaming.py.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import harness.server as server


def _api_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def _get(port, path, token=None):
    headers = {}
    if token:
        headers["X-Harness-Token"] = token
    req = urllib.request.Request(
        "http://127.0.0.1:%s%s" % (port, path),
        headers=headers,
        method="GET",
    )
    return urllib.request.urlopen(req, timeout=10)


def test_sse_ring_retain_and_since_replay():
    server._sse_ring_clear_for_tests()
    ring = server.SseEventRing("sess-a", generation=1, cap=10, ttl=60.0)
    c1 = ring.append("token", {"text": "hello"})
    c2 = ring.append("action_result", {"ok": True})
    c3 = ring.append("done", {})
    assert c1 == 1 and c2 == 2 and c3 == 3

    all_ev = ring.since(0)
    assert all_ev["generation"] == 1
    assert all_ev["cursor"] == 3
    assert [e["kind"] for e in all_ev["events"]] == ["token", "action_result", "done"]
    assert all_ev["events"][0]["data"]["text"] == "hello"

    mid = ring.since(1)
    assert [e["cursor"] for e in mid["events"]] == [2, 3]
    assert mid["events"][0]["kind"] == "action_result"

    empty = ring.since(3)
    assert empty["events"] == []
    assert empty["cursor"] == 3


def test_sse_ring_cap_evicts_oldest():
    server._sse_ring_clear_for_tests()
    ring = server.SseEventRing("sess-b", generation=1, cap=3, ttl=60.0)
    for i in range(5):
        ring.append("token", {"i": i})
    payload = ring.since(0)
    assert payload["retained"] == 3
    assert [e["data"]["i"] for e in payload["events"]] == [2, 3, 4]
    # Cursor ids keep advancing even after eviction.
    assert payload["cursor"] == 5
    assert payload["events"][0]["cursor"] == 3


def test_sse_ring_ttl_evicts_expired(monkeypatch):
    server._sse_ring_clear_for_tests()
    ring = server.SseEventRing("sess-c", generation=1, cap=50, ttl=0.05)
    ring.append("token", {"n": 1})
    time.sleep(0.08)
    ring.append("token", {"n": 2})
    payload = ring.since(0)
    assert [e["data"]["n"] for e in payload["events"]] == [2]


def test_sse_ring_begin_bumps_generation_and_lookup():
    server._sse_ring_clear_for_tests()
    r1 = server._sse_ring_begin("sess-d")
    r1.append("token", {"g": 1})
    r2 = server._sse_ring_begin("sess-d")
    r2.append("token", {"g": 2})
    assert r2.generation == r1.generation + 1
    assert server._sse_ring_lookup("sess-d") is r2
    assert server._sse_ring_lookup("sess-d", r1.generation) is None
    assert server._sse_ring_lookup("sess-d", r2.generation) is r2
    assert server._sse_ring_lookup("missing") is None


def test_sse_pump_retains_events_after_detach():
    """Detached pump keeps draining and retains frames in the ring."""
    from types import MethodType

    server._sse_ring_clear_for_tests()
    ring = server.SseEventRing("sess-pump", generation=1, cap=32, ttl=60.0)

    events = [
        SimpleNamespace(kind="token", data={"t": "a"}, turn=1),
        SimpleNamespace(kind="token", data={"t": "b"}, turn=1),
        SimpleNamespace(kind="assistant_done", data={}, turn=1),
    ]

    class _FakeWfile:
        def __init__(self):
            self.writes = []

        def write(self, payload):
            if len(self.writes) >= 1:
                raise BrokenPipeError("detach")
            self.writes.append(payload)

        def flush(self):
            pass

    handler = SimpleNamespace(wfile=_FakeWfile())
    handler._sse_write = MethodType(server.Handler._sse_write, handler)
    handler._sse_pump = MethodType(server.Handler._sse_pump, handler)

    detached = handler._sse_pump(
        iter(events),
        lambda ev: ("data: %s\n\n" % json.dumps({"kind": ev.kind})).encode(),
        ring=ring,
    )
    assert detached is True
    payload = ring.since(0)
    kinds = [e["kind"] for e in payload["events"]]
    assert kinds.count("token") == 2
    assert "assistant_done" in kinds
    assert "done" in kinds
    assert payload["retained"] >= 3


def test_api_chat_events_replay_endpoint():
    server._sse_ring_clear_for_tests()
    ring = server._sse_ring_begin("sess-api")
    ring.append("token", {"text": "hi"})
    ring.append("action_result", {"ok": True})

    httpd, port = _api_server()
    try:
        # Missing token -> 403
        try:
            _get(port, "/api/chat/events?session=sess-api&since=0")
            assert False, "expected 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        body = json.loads(
            _get(
                port,
                "/api/chat/events?session=sess-api&since=1&generation=%d" % ring.generation,
                token=server._TOKEN,
            ).read().decode()
        )
        assert body["ok"] is True
        assert body.get("missed") is False
        assert body.get("available") is True
        assert body["session_id"] == "sess-api"
        assert body["generation"] == ring.generation
        assert len(body["events"]) == 1
        assert body["events"][0]["kind"] == "action_result"

        empty_sess = json.loads(
            _get(
                port,
                "/api/chat/events?session=no-such&since=0",
                token=server._TOKEN,
            ).read().decode()
        )
        assert empty_sess["ok"] is False
        assert empty_sess["code"] == "ring_miss"
        assert empty_sess["missed"] is True
        assert empty_sess["available"] is False
        assert empty_sess["events"] == []
        assert empty_sess["cursor"] == 0

        # Stale generation must not look like a successful empty replay.
        stale = json.loads(
            _get(
                port,
                "/api/chat/events?session=sess-api&since=0&generation=%d"
                % (ring.generation + 99),
                token=server._TOKEN,
            ).read().decode()
        )
        assert stale["ok"] is False
        assert stale["code"] == "generation_mismatch"
        assert stale["missed"] is True
        assert stale["available"] is False
        assert stale["events"] == []
        assert stale["generation"] == ring.generation
    finally:
        httpd.shutdown()


def test_api_chat_events_ring_miss_and_generation_mismatch():
    """Missing / stale rings return ok:false with a distinct code (not ok:true empty)."""
    server._sse_ring_clear_for_tests()
    ring = server._sse_ring_begin("sess-miss")
    ring.append("token", {"text": "x"})

    httpd, port = _api_server()
    try:
        miss = json.loads(
            _get(
                port,
                "/api/chat/events?session=absent&since=0",
                token=server._TOKEN,
            ).read().decode()
        )
        assert miss["ok"] is False
        assert miss["code"] == "ring_miss"
        assert miss["missed"] is True
        assert miss["available"] is False
        assert miss["events"] == []
        assert miss["cursor"] == 0

        mismatch = json.loads(
            _get(
                port,
                "/api/chat/events?session=sess-miss&since=0&generation=999",
                token=server._TOKEN,
            ).read().decode()
        )
        assert mismatch["ok"] is False
        assert mismatch["code"] == "generation_mismatch"
        assert mismatch["missed"] is True
        assert mismatch["available"] is False
        assert mismatch["generation"] == ring.generation
        assert mismatch["events"] == []
    finally:
        httpd.shutdown()


def test_usage_response_cache_ttl_hit(monkeypatch):
    # _usage_cache_get bypasses under PYTEST_CURRENT_TEST; clear it so this
    # unit test can exercise the real TTL hit/miss path.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    server._usage_cache_clear_for_tests()
    payload = {"session": {"tokens_used": 1}, "jobs": []}
    server._usage_cache_put("k1", payload)
    assert server._usage_cache_get("k1") == payload
    # Expire by rewriting with past expiry via lock internals.
    with server._usage_response_lock:
        server._usage_response_cache["k1"] = (time.monotonic() - 1.0, payload)
    assert server._usage_cache_get("k1") is None
