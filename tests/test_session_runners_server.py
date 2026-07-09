"""Phase B2: SessionRunnerRegistry wired into harness/server switch + open."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from harness.session_runners import LeaseExhaustedError, SessionRunnerRegistry


def _busy_runner() -> SimpleNamespace:
    lock = threading.Lock()
    lock.acquire()
    return SimpleNamespace(
        _busy=lock,
        _state="executing",
        _history=[],
        state_dir="/tmp/fake-runner",
        harness_session_id="",
        _auto_distill=False,
        _mcp=None,
        _session_store=None,
        export_transcript_data=lambda: {"history": [], "display": []},
        load_history=lambda _h: None,
    )


def _idle_runner() -> SimpleNamespace:
    return SimpleNamespace(
        _busy=threading.Lock(),
        _state="idle",
        _history=[],
        state_dir="/tmp/fake-runner",
        harness_session_id="",
        _auto_distill=False,
        _mcp=None,
        _session_store=None,
        export_transcript_data=lambda: {"history": [], "display": []},
        load_history=lambda _h: None,
    )


def _spin_server():
    import harness.server as srv

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return srv, httpd, port


def _post(port, path, body, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Harness-Token": token,
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=10)


def _get(port, path, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"X-Harness-Token": token},
    )
    return urllib.request.urlopen(req, timeout=10)


def test_switch_view_succeeds_while_both_runners_busy():
    """Two busy runners under lease: switch view must not 409; both retained."""
    srv, httpd, port = _spin_server()
    old_runners = srv._runners
    old_pilot = srv._pilot
    try:
        reg = SessionRunnerRegistry(max_concurrent_sessions=3)
        a = srv._sessions.create(title="A")
        b = srv._sessions.create(title="B")
        sid_a, sid_b = a["id"], b["id"]

        runner_a = _busy_runner()
        runner_b = _busy_runner()
        reg.get_or_create(sid_a, lambda: runner_a)
        reg.get_or_create(sid_b, lambda: runner_b)
        reg.set_active_view(sid_a)
        srv._runners = reg
        srv._pilot = runner_a
        srv._sessions.switch(sid_a)

        resp = _post(port, "/api/sessions/switch", {"id": sid_b}, srv._TOKEN)
        assert resp.status == 200
        payload = json.loads(resp.read().decode())
        assert payload.get("ok") is True
        assert payload.get("active") == sid_b

        assert reg.get(sid_a) is runner_a
        assert reg.get(sid_b) is runner_b
        assert reg.active_view_id == sid_b
        assert srv._pilot is runner_b
        assert set(reg.ids()) == {sid_a, sid_b}
        assert reg.statuses() == {sid_a: "running", sid_b: "running"}
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        httpd.shutdown()


def test_switch_returns_409_when_lease_exhausted():
    """Switch to a new session when every lease slot is busy -> 409."""
    srv, httpd, port = _spin_server()
    old_runners = srv._runners
    old_pilot = srv._pilot
    try:
        reg = SessionRunnerRegistry(max_concurrent_sessions=2)
        a = srv._sessions.create(title="A")
        b = srv._sessions.create(title="B")
        c = srv._sessions.create(title="C")
        sid_a, sid_b, sid_c = a["id"], b["id"], c["id"]

        runner_a = _busy_runner()
        runner_b = _busy_runner()
        reg.get_or_create(sid_a, lambda: runner_a)
        reg.get_or_create(sid_b, lambda: runner_b)
        reg.set_active_view(sid_a)
        srv._runners = reg
        srv._pilot = runner_a
        srv._sessions.switch(sid_a)

        # Target C is not in the registry; creating it would need a free slot.
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(port, "/api/sessions/switch", {"id": sid_c}, srv._TOKEN)
        assert ei.value.code == 409
        err = json.loads(ei.value.read().decode())
        assert "lease" in (err.get("error") or "").lower() or err.get("code") == "lease_exhausted"

        assert set(reg.ids()) == {sid_a, sid_b}
        assert reg.get(sid_c) is None
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        httpd.shutdown()


def test_workspace_open_returns_409_when_lease_exhausted(tmp_path):
    """Opening a workspace that needs a new runner while lease is full -> 409."""
    srv, httpd, port = _spin_server()
    old_runners = srv._runners
    old_pilot = srv._pilot
    old_repo = srv._cfg.repo
    try:
        reg = SessionRunnerRegistry(max_concurrent_sessions=2)
        # Fill lease with two busy runners for existing sessions.
        a = srv._sessions.create(title="A", repo=str(tmp_path / "a"), workspace_root=str(tmp_path / "a"))
        b = srv._sessions.create(title="B", repo=str(tmp_path / "b"), workspace_root=str(tmp_path / "b"))
        sid_a, sid_b = a["id"], b["id"]
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        target = tmp_path / "c"
        target.mkdir()

        reg.get_or_create(sid_a, _busy_runner)
        reg.get_or_create(sid_b, _busy_runner)
        reg.set_active_view(sid_a)
        srv._runners = reg
        srv._pilot = reg.get(sid_a)
        srv._sessions.switch(sid_a)
        srv._cfg.repo = str(tmp_path / "a")

        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(port, "/api/workspace/open", {"path": str(target)}, srv._TOKEN)
        assert ei.value.code == 409
        err = json.loads(ei.value.read().decode())
        assert err.get("code") == "lease_exhausted" or "lease" in (err.get("error") or "").lower()
        assert set(reg.ids()) == {sid_a, sid_b}
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        srv._cfg.repo = old_repo
        httpd.shutdown()


def test_session_state_exposes_runner_statuses():
    srv, httpd, port = _spin_server()
    old_runners = srv._runners
    old_pilot = srv._pilot
    try:
        reg = SessionRunnerRegistry(max_concurrent_sessions=3)
        a = srv._sessions.create(title="A")
        sid = a["id"]
        runner = _idle_runner()
        # ConversationalSession.state() is used by /api/session/state
        runner.state = lambda: "idle"
        runner.has_pending_swarms = lambda: False
        reg.get_or_create(sid, lambda: runner)
        reg.set_active_view(sid)
        srv._runners = reg
        srv._pilot = runner
        srv._sessions.switch(sid)

        resp = _get(port, f"/api/session/state?token={srv._TOKEN}", srv._TOKEN)
        payload = json.loads(resp.read().decode())
        assert "runners" in payload
        assert payload["runners"].get(sid) == "idle"
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        httpd.shutdown()
