"""Resume latch: ghost-resume must not fire on mere session view.

`/api/session/state.resume_pending` is an EXPLICIT one-shot latch armed by the
self-edit restart path (`/api/session/persist` or `/api/restart`), not the
generic "transcript ends on a user turn" heuristic. These tests pin both sides
of that contract and keep the self-dev restart flow green.

HTTP coverage uses a real ``ThreadingHTTPServer``. On Windows (especially under
full-suite load) a fire-and-forget serve thread plus unclosed responses can
leave handler threads / sockets racing the next test's global mutations, which
surfaces as a client timeout on ``/api/session/persist``. This module owns the
server lifecycle end-to-end so teardown always joins cleanly.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from typing import Optional

# Modest platform-safe client timeout: enough for slow CI disks, short enough
# that a true deadlock fails the job instead of hanging for minutes.
_HTTP_TIMEOUT = 15.0 if sys.platform == "win32" else 5.0
_JOIN_TIMEOUT = 5.0
_READY_TIMEOUT = 5.0
_STRESS_ROUNDS = 8 if sys.platform == "win32" else 12
_SERVE_THREAD_NAME = "test-self-dev-restart-httpd"


class _TestThreadingHTTPServer(ThreadingHTTPServer):
    """Request-handler threads must not outlive teardown (Windows CI hang)."""

    daemon_threads = True


def _make_session():
    from harness.conversation import ConversationalSession
    from harness.config import HarnessConfig
    return ConversationalSession(HarnessConfig())


def _wait_server_ready(port: int, token: str) -> None:
    """Block until the accept loop answers a side-effect-light GET."""
    deadline = time.monotonic() + _READY_TIMEOUT
    last_err: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/config",
                headers={"X-Harness-Token": token},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:
            last_err = exc
            time.sleep(0.05)
    raise RuntimeError(
        f"harness test server not ready on 127.0.0.1:{port}: {last_err!r}"
    )


@contextmanager
def _harness_http_server():
    """Start Handler on an ephemeral port; always shutdown + close + join."""
    import harness.server as srv

    httpd = _TestThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(
        target=httpd.serve_forever,
        name=_SERVE_THREAD_NAME,
        daemon=True,
    )
    thread.start()
    try:
        _wait_server_ready(port, srv._TOKEN)
        yield srv, port
    finally:
        try:
            httpd.shutdown()
        finally:
            try:
                httpd.server_close()
            except OSError:
                pass
            thread.join(timeout=_JOIN_TIMEOUT)


def _session_state(
    port: int,
    token: str,
    *,
    consume_resume: bool = False,
    rearm_resume: bool = False,
    session_id: Optional[str] = None,
) -> dict:
    from urllib.parse import quote

    params = []
    if consume_resume:
        params.append("consume_resume=1")
    if rearm_resume:
        params.append("rearm_resume=1")
    if session_id:
        params.append(f"session_id={quote(session_id)}")
    qs = ("?" + "&".join(params)) if params else ""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/session/state{qs}",
        headers={"X-Harness-Token": token},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_session_persist(port: int, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/session/persist",
        data=b"{}",
        headers={"Content-Type": "application/json", "X-Harness-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


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


def test_trailing_user_turn_alone_does_not_report_resume_pending():
    """Idle pilot + unanswered user turn is NOT enough -- latch must be armed."""
    import harness.server as srv

    saved = list(srv._pilot._history)
    saved_latch = srv._resume_latch
    saved_active = srv._sessions._active
    try:
        with _harness_http_server() as (srv, port):
            srv._clear_resume_latch()
            sid = "sess-resume-idle"
            srv._sessions._active = sid
            srv._pilot._history = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "please continue"},
            ]
            assert srv._pilot.has_pending_user_turn() is True
            data = _session_state(port, srv._TOKEN, session_id=sid)
            assert data["resume_pending"] is False
            # Second poll still false (nothing to consume).
            data2 = _session_state(port, srv._TOKEN, session_id=sid)
            assert data2["resume_pending"] is False
    finally:
        # Restore only after the serve thread and handlers have settled.
        srv._pilot._history = saved
        srv._sessions._active = saved_active
        if saved_latch:
            srv._set_resume_latch()
        else:
            srv._clear_resume_latch()


def test_session_state_reports_resume_pending_after_explicit_latch():
    """Self-dev restart: persist arms latch; peek is sticky; consume is one-shot."""
    import harness.server as srv

    saved = list(srv._pilot._history)
    saved_active = srv._sessions._active
    try:
        with _harness_http_server() as (srv, port):
            srv._clear_resume_latch()
            sid = "sess-resume-latch"
            srv._sessions._active = sid
            srv._pilot._history = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "please continue"},
            ]
            # Arm via the same endpoint Electron calls before respawn.
            status, payload = _post_session_persist(port, srv._TOKEN)
            assert status == 200
            assert payload["ok"] is True
            assert srv._resume_latch_session_id == sid

            # Plain state polls (StatusBar / runners / switch) must peek only.
            data = _session_state(port, srv._TOKEN, session_id=sid)
            assert data["resume_pending"] is True
            data_peek2 = _session_state(port, srv._TOKEN, session_id=sid)
            assert data_peek2["resume_pending"] is True
            # Wrong session must not see or steal the latch.
            assert _session_state(
                port, srv._TOKEN, session_id="sess-other"
            )["resume_pending"] is False
            assert _session_state(
                port,
                srv._TOKEN,
                consume_resume=True,
                session_id="sess-other",
            )["resume_pending"] is False
            assert srv._resume_latch is True

            # Conversation resume path consumes once.
            data_consume = _session_state(
                port, srv._TOKEN, consume_resume=True, session_id=sid
            )
            assert data_consume["resume_pending"] is True
            data2 = _session_state(
                port, srv._TOKEN, consume_resume=True, session_id=sid
            )
            assert data2["resume_pending"] is False
            data_peek_after = _session_state(port, srv._TOKEN, session_id=sid)
            assert data_peek_after["resume_pending"] is False
    finally:
        srv._pilot._history = saved
        srv._sessions._active = saved_active
        srv._clear_resume_latch()


def test_resume_latch_survives_same_app_run_reload(monkeypatch, tmp_path):
    """Self-edit backend respawn: same HARNESS_APP_RUN_ID still adopts the latch."""
    import harness.server as srv

    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-same")
    monkeypatch.setattr(srv._cfg, "state_dir", str(tmp_path), raising=False)
    srv._clear_resume_latch()
    try:
        srv._set_resume_latch("sess-a")
        assert srv._resume_latch is True
        assert srv._resume_latch_session_id == "sess-a"
        srv._resume_latch = False
        srv._resume_latch_session_id = ""
        srv._load_resume_latch()
        assert srv._resume_latch is True
        assert srv._resume_latch_session_id == "sess-a"
        assert (tmp_path / ".resume_latch").is_file()
    finally:
        srv._clear_resume_latch()


def test_resume_latch_rejected_after_app_relaunch(monkeypatch, tmp_path):
    """Update / full quit: new HARNESS_APP_RUN_ID must clear a leftover latch."""
    import harness.server as srv

    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-old")
    monkeypatch.setattr(srv._cfg, "state_dir", str(tmp_path), raising=False)
    srv._clear_resume_latch()
    try:
        srv._set_resume_latch("sess-a")
        assert (tmp_path / ".resume_latch").is_file()
        monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-new")
        srv._resume_latch = False
        srv._resume_latch_session_id = ""
        srv._load_resume_latch()
        assert srv._resume_latch is False
        assert srv._resume_latch_session_id == ""
        assert not (tmp_path / ".resume_latch").exists()
    finally:
        srv._clear_resume_latch()


def test_resume_latch_session_scoped_peek_and_consume(monkeypatch, tmp_path):
    """Latch armed for A must not peek true for B; B consume must not clear A."""
    import harness.server as srv

    monkeypatch.setattr(srv._cfg, "state_dir", str(tmp_path), raising=False)
    srv._clear_resume_latch()
    try:
        srv._set_resume_latch("sess-a")
        assert srv._peek_resume_pending(True, "sess-a") is True
        assert srv._peek_resume_pending(True, "sess-b") is False
        assert srv._peek_resume_pending(True, "") is False
        assert srv._consume_resume_pending(True, "sess-b") is False
        assert srv._peek_resume_pending(True, "sess-a") is True
        assert srv._consume_resume_pending(True, "sess-a") is True
        assert srv._peek_resume_pending(True, "sess-a") is False
    finally:
        srv._clear_resume_latch()


def test_legacy_resume_latch_without_session_id_sole_session_only(
    monkeypatch, tmp_path
):
    """v1 disk latch missing session_id: honor only when a single session exists."""
    import harness.server as srv
    import json as _json

    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-legacy")
    monkeypatch.setattr(srv._cfg, "state_dir", str(tmp_path), raising=False)
    latch = tmp_path / ".resume_latch"
    latch.write_text(
        _json.dumps(
            {"v": 1, "armed": True, "app_run_id": "run-legacy"},
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    rows = [{"id": "only-one"}, {"id": "two"}]
    monkeypatch.setattr(
        srv._sessions, "rows", lambda: list(rows), raising=False
    )
    try:
        srv._resume_latch = False
        srv._resume_latch_session_id = ""
        srv._load_resume_latch()
        assert srv._resume_latch is False

        rows[:] = [{"id": "only-one"}]
        latch.write_text(
            _json.dumps(
                {"v": 1, "armed": True, "app_run_id": "run-legacy"},
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        srv._resume_latch = False
        srv._resume_latch_session_id = ""
        srv._load_resume_latch()
        assert srv._resume_latch is True
        assert srv._resume_latch_session_id == "only-one"
        assert srv._peek_resume_pending(True, "only-one") is True
        assert srv._peek_resume_pending(True, "other") is False
    finally:
        srv._clear_resume_latch()


def test_legacy_plain_resume_latch_rejected_when_app_run_id_set(
    monkeypatch, tmp_path
):
    """Pre-fence ``1`` files cannot ghost-resume after an Electron relaunch."""
    import harness.server as srv

    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-electron")
    monkeypatch.setattr(srv._cfg, "state_dir", str(tmp_path), raising=False)
    latch = tmp_path / ".resume_latch"
    latch.write_text("1\n", encoding="utf-8")
    try:
        srv._resume_latch = False
        srv._resume_latch_session_id = ""
        srv._load_resume_latch()
        assert srv._resume_latch is False
        assert not latch.exists()
    finally:
        srv._clear_resume_latch()


def test_session_persist_endpoint_writes_transcript(tmp_path):
    from harness.sessions import load_transcript
    import harness.server as srv

    saved_hist = list(srv._pilot._history)
    saved_active = srv._sessions._active
    saved_state_dir = srv._cfg.state_dir
    try:
        with _harness_http_server() as (srv, port):
            srv._cfg.state_dir = str(tmp_path)
            srv._sessions._active = "sess-persist-test"
            srv._pilot._history = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "remember me"},
            ]
            status, payload = _post_session_persist(port, srv._TOKEN)
            assert status == 200
            assert payload["ok"] is True

            restored = load_transcript(str(tmp_path), "sess-persist-test")
            hist = restored.get("history") if isinstance(restored, dict) else restored
            assert any(m.get("content") == "remember me" for m in hist)
    finally:
        srv._pilot._history = saved_hist
        srv._sessions._active = saved_active
        srv._cfg.state_dir = saved_state_dir
        srv._clear_resume_latch()


def test_http_server_lifecycle_stress_no_leaks():
    """Repeated start/request/teardown must not leak serve threads or latch state."""
    import harness.server as srv

    saved_hist = list(srv._pilot._history)
    saved_latch = srv._resume_latch
    try:
        for round_idx in range(_STRESS_ROUNDS):
            with _harness_http_server() as (srv, port):
                srv._clear_resume_latch()
                data = _session_state(
                    port, srv._TOKEN, session_id=srv._sessions.active or "s"
                )
                assert "resume_pending" in data
                status, payload = _post_session_persist(port, srv._TOKEN)
                assert status == 200, f"persist failed on stress round {round_idx}"
                assert payload["ok"] is True
            leftover = [
                t
                for t in threading.enumerate()
                if t.name == _SERVE_THREAD_NAME and t.is_alive()
            ]
            assert not leftover, (
                f"serve thread leaked after stress round {round_idx}: {leftover!r}"
            )
    finally:
        srv._pilot._history = saved_hist
        if saved_latch:
            srv._set_resume_latch()
        else:
            srv._clear_resume_latch()
