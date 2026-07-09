"""Resume latch: ghost-resume must not fire on mere session view.

`/api/session/state.resume_pending` is an EXPLICIT one-shot latch armed by the
self-edit restart path (`/api/session/persist` or `/api/restart`), not the
generic "transcript ends on a user turn" heuristic. These tests pin both sides
of that contract and keep the self-dev restart flow green.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer


def _make_session():
    from harness.conversation import ConversationalSession
    from harness.config import HarnessConfig
    return ConversationalSession(HarnessConfig())


def test_has_pending_user_turn_true_when_reply_owed():
    s = _make_session()
    s._history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert s.has_pending_user_turn() is True


def test_has_pending_user_turn_false_after_assistant_reply():
    s = _make_session()
    s._history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert s.has_pending_user_turn() is False


def test_has_pending_user_turn_false_on_empty_transcript():
    s = _make_session()
    s._history = [{"role": "system", "content": "sys"}]
    assert s.has_pending_user_turn() is False


def _spin_server():
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return srv, httpd, port


def _session_state(port, token):
    resp = urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/session/state?token={token}", timeout=5)
    return json.loads(resp.read().decode("utf-8"))


def test_trailing_user_turn_alone_does_not_report_resume_pending():
    """Idle pilot + unanswered user turn is NOT enough -- latch must be armed."""
    srv, httpd, port = _spin_server()
    saved = list(srv._pilot._history)
    saved_latch = srv._resume_latch
    try:
        srv._clear_resume_latch()
        srv._pilot._history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "please continue"},
        ]
        assert srv._pilot.has_pending_user_turn() is True
        data = _session_state(port, srv._TOKEN)
        assert data["resume_pending"] is False
        # Second poll still false (nothing to consume).
        data2 = _session_state(port, srv._TOKEN)
        assert data2["resume_pending"] is False
    finally:
        srv._pilot._history = saved
        if saved_latch:
            srv._set_resume_latch()
        else:
            srv._clear_resume_latch()
        httpd.shutdown()


def test_session_state_reports_resume_pending_after_explicit_latch():
    """Self-dev restart flow: persist arms the latch; idle state reports true once."""
    srv, httpd, port = _spin_server()
    saved = list(srv._pilot._history)
    try:
        srv._clear_resume_latch()
        srv._pilot._history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "please continue"},
        ]
        # Arm via the same endpoint Electron calls before respawn.
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/session/persist?token={srv._TOKEN}",
            data=b"{}",
            headers={"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        assert json.loads(resp.read().decode("utf-8"))["ok"] is True

        data = _session_state(port, srv._TOKEN)
        assert data["resume_pending"] is True

        # One-shot: consumed on report so a later view cannot re-fire.
        data2 = _session_state(port, srv._TOKEN)
        assert data2["resume_pending"] is False
    finally:
        srv._pilot._history = saved
        srv._clear_resume_latch()
        httpd.shutdown()


def test_session_persist_endpoint_writes_transcript(tmp_path):
    from harness.sessions import load_transcript
    srv, httpd, port = _spin_server()
    saved_hist = list(srv._pilot._history)
    saved_active = srv._sessions._active
    saved_state_dir = srv._cfg.state_dir
    try:
        srv._cfg.state_dir = str(tmp_path)
        srv._sessions._active = "sess-persist-test"
        srv._pilot._history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "remember me"},
        ]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/session/persist?token={srv._TOKEN}",
            data=b"{}",
            headers={"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        assert resp.status == 200
        assert json.loads(resp.read().decode("utf-8"))["ok"] is True

        restored = load_transcript(str(tmp_path), "sess-persist-test")
        hist = restored.get("history") if isinstance(restored, dict) else restored
        assert any(m.get("content") == "remember me" for m in hist)
    finally:
        srv._pilot._history = saved_hist
        srv._sessions._active = saved_active
        srv._cfg.state_dir = saved_state_dir
        srv._clear_resume_latch()
        httpd.shutdown()
