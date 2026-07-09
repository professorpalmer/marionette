"""Phase A multi-session: SSE view detach must not cancel an in-flight turn.

Closing EventSource / navigating away used to call _pilot.cancel() (auto) or
gen.close() mid-yield (chat), which aborted the turn. Hermes-style detach keeps
the generator draining so the pilot finishes and releases _busy; only explicit
Stop (/api/session/interrupt) cancels.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from harness.conversation import ConvEvent


def _server():
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _post(port, path, body, headers):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=10)


def _slow_turn_events(gate: threading.Event, finished: list, n: int = 5):
    """Yield n events, blocking after the first until gate is set (client detached)."""
    try:
        for i in range(n):
            yield ConvEvent("assistant", {"text": f"chunk-{i}"})
            if i == 0:
                # Give the client a chance to read the first frame and close.
                gate.wait(timeout=5.0)
                time.sleep(0.05)
        yield ConvEvent("assistant_done", {"text": "done"})
    finally:
        finished.append(True)


def test_chat_stream_client_disconnect_does_not_cancel_turn():
    """Closing the chat SSE mid-turn must not set _cancel; the turn must finish."""
    first_frame = threading.Event()
    finished = []
    cancel_calls = []

    def send_side_effect(*_a, **_k):
        return _slow_turn_events(first_frame, finished)

    mock_pilot = MagicMock()
    mock_pilot.send.side_effect = send_side_effect
    mock_pilot.drain_swarm_results.return_value = []
    mock_pilot.cancel.side_effect = lambda: cancel_calls.append("cancel")
    mock_pilot._cancel = threading.Event()
    mock_pilot._busy = threading.Lock()

    with patch("harness.server._pilot", mock_pilot), \
         patch("harness.server._pilot_preflight", return_value=None), \
         patch("harness.server._finalize_turn"):
        httpd, port, srv = _server()
        try:
            sess = srv._sessions.create()
            srv._sessions._active = sess["id"]
            url = f"http://127.0.0.1:{port}/api/chat?message=hi&token={srv._TOKEN}"
            req = urllib.request.Request(url, method="GET")
            resp = urllib.request.urlopen(req, timeout=10)
            # Read one SSE frame, then drop the connection (view detach).
            line = resp.readline()
            assert line, "expected at least one SSE frame before detach"
            first_frame.set()
            resp.close()

            # Turn must keep running to completion without cancel().
            deadline = time.time() + 5.0
            while not finished and time.time() < deadline:
                time.sleep(0.05)
            assert finished, "in-flight turn did not finish after UI detach"
            assert not cancel_calls, f"detach must not cancel: {cancel_calls}"
            assert not mock_pilot._cancel.is_set()
        finally:
            first_frame.set()
            httpd.shutdown()


def test_auto_stream_client_disconnect_does_not_cancel_turn():
    """Closing /api/auto SSE must not call _pilot.cancel() (the old BrokenPipe path)."""
    first_frame = threading.Event()
    finished = []
    cancel_calls = []

    def run_auto_side_effect(*_a, **_k):
        return _slow_turn_events(first_frame, finished)

    mock_pilot = MagicMock()
    mock_pilot.run_auto.side_effect = run_auto_side_effect
    mock_pilot.cancel.side_effect = lambda: cancel_calls.append("cancel")
    mock_pilot._cancel = threading.Event()
    mock_pilot.export_transcript_data.return_value = {"history": []}

    with patch("harness.server._pilot", mock_pilot), \
         patch("harness.server._finalize_turn"), \
         patch("harness.server.AutoBudget") as mock_budget:
        mock_budget.from_env.return_value = MagicMock()
        httpd, port, srv = _server()
        try:
            sess = srv._sessions.create()
            srv._sessions._active = sess["id"]
            url = f"http://127.0.0.1:{port}/api/auto?objective=go&token={srv._TOKEN}"
            req = urllib.request.Request(url, method="GET")
            resp = urllib.request.urlopen(req, timeout=10)
            line = resp.readline()
            assert line, "expected at least one SSE frame before detach"
            first_frame.set()
            resp.close()

            deadline = time.time() + 5.0
            while not finished and time.time() < deadline:
                time.sleep(0.05)
            assert finished, "auto turn did not finish after UI detach"
            assert not cancel_calls, f"auto detach must not cancel: {cancel_calls}"
            assert not mock_pilot._cancel.is_set()
        finally:
            first_frame.set()
            httpd.shutdown()


def test_explicit_interrupt_still_cancels():
    """Stop button path: /api/session/interrupt must still cancel the pilot."""
    httpd, port, srv = _server()
    try:
        srv._pilot._cancel.clear()
        assert not srv._pilot._cancel.is_set()
        resp = _post(port, "/api/session/interrupt", {}, {
            "Content-Type": "application/json",
            "X-Harness-Token": srv._TOKEN,
        })
        assert resp.status == 200
        assert json.loads(resp.read().decode()).get("ok") is True
        assert srv._pilot._cancel.is_set()
    finally:
        srv._pilot._cancel.clear()
        httpd.shutdown()


def test_opening_session_with_trailing_user_does_not_arm_resume_latch():
    """Ghost-resume regression: idle + trailing user turn => resume_pending false.

    Opening/switching to a past session that ends on a user message must not
    look like a self-edit restart latch. Mirrors Conversation.tsx: only
    resume_pending true schedules api.resume.
    """
    httpd, port, srv = _server()
    saved = list(srv._pilot._history)
    saved_latch = srv._resume_latch
    try:
        srv._clear_resume_latch()
        srv._pilot._history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "unanswered from a past session"},
        ]
        assert srv._pilot.has_pending_user_turn() is True
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/session/state?token={srv._TOKEN}",
            timeout=5,
        )
        data = json.loads(resp.read().decode("utf-8"))
        assert data["resume_pending"] is False
        assert data["state"] == "idle"
    finally:
        srv._pilot._history = saved
        if saved_latch:
            srv._set_resume_latch()
        else:
            srv._clear_resume_latch()
        httpd.shutdown()


def test_sse_write_treats_connection_aborted_as_detach():
    """Windows EventSource close often raises ConnectionAbortedError, not Reset/BrokenPipe."""
    import harness.server as srv

    handler = object.__new__(srv.Handler)
    writes = {"n": 0}

    class _BoomWfile:
        def write(self, _payload):
            writes["n"] += 1
            raise ConnectionAbortedError("simulated Windows client close")

        def flush(self):
            return None

    handler.wfile = _BoomWfile()
    assert handler._sse_write(b"data: {}\n\n") is False
    assert writes["n"] == 1
