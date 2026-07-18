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
    assert payload.get("gap") is False


def test_sse_ring_cap_prune_reports_cursor_gap():
    """After cap eviction, since inside the hole must not look contiguous."""
    server._sse_ring_clear_for_tests()
    ring = server.SseEventRing("sess-gap-cap", generation=1, cap=3, ttl=60.0)
    for i in range(5):
        ring.append("token", {"i": i})
    # Retained cursors are 3,4,5. Client last saw cursor 1 → hole at 2.
    gap = ring.since(1)
    assert gap["gap"] is True
    assert gap["events"] == []
    assert gap["retained"] == 3
    assert gap["cursor"] == 5
    # Contiguous since (oldest retained == since+1) is fine.
    ok = ring.since(2)
    assert ok["gap"] is False
    assert [e["cursor"] for e in ok["events"]] == [3, 4, 5]


def test_sse_ring_ttl_evicts_expired(monkeypatch):
    server._sse_ring_clear_for_tests()
    ring = server.SseEventRing("sess-c", generation=1, cap=50, ttl=0.05)
    ring.append("token", {"n": 1})
    time.sleep(0.08)
    ring.append("token", {"n": 2})
    payload = ring.since(0)
    assert [e["data"]["n"] for e in payload["events"]] == [2]
    assert payload.get("gap") is False


def test_sse_ring_empty_after_prune_reports_cursor_gap():
    """Empty retained with high-water still ahead of since is a gap, not catch-up."""
    server._sse_ring_clear_for_tests()
    ring = server.SseEventRing("sess-gap-empty", generation=1, cap=2, ttl=0.05)
    ring.append("token", {"n": 1})
    ring.append("token", {"n": 2})
    time.sleep(0.08)
    # Force TTL prune via since(0); retained empties but cursor high-water remains.
    emptied = ring.since(0)
    assert emptied["retained"] == 0
    assert emptied["cursor"] == 2
    assert emptied.get("gap") is False  # since=0 never reports gap
    behind = ring.since(1)
    assert behind["gap"] is True
    assert behind["events"] == []
    assert behind["cursor"] == 2
    # Fully caught up with an empty ring is not a gap.
    caught_up = ring.since(2)
    assert caught_up["gap"] is False
    assert caught_up["events"] == []


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


def test_api_chat_events_cursor_gap_after_cap_prune():
    """Cap prune holes return cursor_gap — never ok:true with skipped cursors."""
    server._sse_ring_clear_for_tests()
    ring = server._sse_ring_begin("sess-gap")
    # Override cap on the live ring so five appends leave a hole after since=1.
    ring.cap = 3
    for i in range(5):
        ring.append("token", {"i": i})

    httpd, port = _api_server()
    try:
        body = json.loads(
            _get(
                port,
                "/api/chat/events?session=sess-gap&since=1&generation=%d" % ring.generation,
                token=server._TOKEN,
            ).read().decode()
        )
        assert body["ok"] is False
        assert body["code"] == "cursor_gap"
        assert body["missed"] is True
        assert body["available"] is False
        assert body["events"] == []
        assert body["generation"] == ring.generation
        assert body["cursor"] == 5
        assert body["retained"] == 3

        # Contiguous since still replays successfully.
        ok = json.loads(
            _get(
                port,
                "/api/chat/events?session=sess-gap&since=2&generation=%d" % ring.generation,
                token=server._TOKEN,
            ).read().decode()
        )
        assert ok["ok"] is True
        assert ok.get("missed") is False
        assert [e["cursor"] for e in ok["events"]] == [3, 4, 5]
    finally:
        httpd.shutdown()


def test_api_chat_events_mid_tool_batch_cursor_gap_then_retained_tail():
    """Detached mid-tool-batch: gap refuses fake catch-up; since=0 returns tool tail."""
    server._sse_ring_clear_for_tests()
    ring = server._sse_ring_begin("sess-tools")
    ring.cap = 3
    # Early frames (thinking + first tools) will be pruned by the cap.
    ring.append("thinking", {"text": "plan"})
    ring.append("action_start", {"id": "a1", "goal": "read a", "kind": "read_file"})
    ring.append("action_result", {"id": "a1", "ok": True})
    ring.append("action_start", {"id": "a2", "goal": "read b", "kind": "read_file"})
    ring.append("action_start", {"id": "a3", "goal": "run", "kind": "run_command"})
    # Retained cursors are 3,4,5 (a1 result + a2/a3 starts). Client last saw 1.

    httpd, port = _api_server()
    try:
        gap = json.loads(
            _get(
                port,
                "/api/chat/events?session=sess-tools&since=1&generation=%d"
                % ring.generation,
                token=server._TOKEN,
            ).read().decode()
        )
        assert gap["ok"] is False
        assert gap["code"] == "cursor_gap"
        assert gap["missed"] is True
        assert gap["available"] is False
        assert gap["events"] == []
        # Must not invent the pruned thinking/action_start frames.
        assert gap["cursor"] == 5
        assert gap["retained"] == 3

        # After client resets since→0 (hydrate path), retained tool tail only.
        tail = json.loads(
            _get(
                port,
                "/api/chat/events?session=sess-tools&since=0&generation=%d"
                % ring.generation,
                token=server._TOKEN,
            ).read().decode()
        )
        assert tail["ok"] is True
        assert tail.get("missed") is False
        kinds = [e["kind"] for e in tail["events"]]
        assert kinds == ["action_result", "action_start", "action_start"]
        assert [e["data"]["id"] for e in tail["events"]] == ["a1", "a2", "a3"]
        assert "thinking" not in kinds
    finally:
        httpd.shutdown()


def test_api_chat_events_long_ring_miss_after_ttl_empty():
    """Long detach: TTL empties retained frames; behind since is cursor_gap, not ok empty."""
    server._sse_ring_clear_for_tests()
    ring = server._sse_ring_begin("sess-long")
    ring.ttl = 0.05
    ring.append("action_start", {"id": "a1", "goal": "old"})
    ring.append("action_result", {"id": "a1", "ok": True})
    time.sleep(0.08)

    httpd, port = _api_server()
    try:
        # Client still pinned mid-turn after the ring aged out.
        gap = json.loads(
            _get(
                port,
                "/api/chat/events?session=sess-long&since=1&generation=%d"
                % ring.generation,
                token=server._TOKEN,
            ).read().decode()
        )
        assert gap["ok"] is False
        assert gap["code"] == "cursor_gap"
        assert gap["missed"] is True
        assert gap["available"] is False
        assert gap["events"] == []
        assert gap["retained"] == 0
        assert gap["cursor"] == 2

        # Full ring eviction (session gone) is ring_miss — still not fake catch-up.
        server._sse_ring_clear_for_tests()
        miss = json.loads(
            _get(
                port,
                "/api/chat/events?session=sess-long&since=0",
                token=server._TOKEN,
            ).read().decode()
        )
        assert miss["ok"] is False
        assert miss["code"] == "ring_miss"
        assert miss["missed"] is True
        assert miss["available"] is False
        assert miss["events"] == []
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
