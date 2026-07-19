"""Phase B2: SessionRunnerRegistry wired into harness/server switch + open."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from harness.session_runners import LeaseExhaustedError, SessionRunnerRegistry
from harness.sessions import SessionStore, load_transcript


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


def test_switch_returns_409_when_lease_exhausted(tmp_path):
    """Switch to a new session when every lease slot is busy -> 409.

    Must roll back SessionStore.active and _cfg.repo (unlike a successful
    switch, which advances both before attach).
    """
    srv, httpd, port = _spin_server()
    old_runners = srv._runners
    old_pilot = srv._pilot
    old_repo = srv._cfg.repo
    old_env_repo = os.environ.get("HARNESS_REPO")
    try:
        repo_a = tmp_path / "a"
        repo_c = tmp_path / "c"
        repo_a.mkdir()
        repo_c.mkdir()

        reg = SessionRunnerRegistry(max_concurrent_sessions=2)
        a = srv._sessions.create(title="A", repo=str(repo_a), workspace_root=str(repo_a))
        b = srv._sessions.create(title="B", repo=str(repo_a), workspace_root=str(repo_a))
        c = srv._sessions.create(title="C", repo=str(repo_c), workspace_root=str(repo_c))
        sid_a, sid_b, sid_c = a["id"], b["id"], c["id"]

        runner_a = _busy_runner()
        runner_b = _busy_runner()
        reg.get_or_create(sid_a, lambda: runner_a)
        reg.get_or_create(sid_b, lambda: runner_b)
        reg.set_active_view(sid_a)
        srv._runners = reg
        srv._pilot = runner_a
        srv._sessions.switch(sid_a)
        srv._cfg.repo = str(repo_a)
        os.environ["HARNESS_REPO"] = str(repo_a)

        # Target C is not in the registry; creating it would need a free slot.
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(port, "/api/sessions/switch", {"id": sid_c}, srv._TOKEN)
        assert ei.value.code == 409
        err = json.loads(ei.value.read().decode())
        assert err.get("code") == "lease_exhausted"
        assert err.get("max_concurrent") == 2
        assert err.get("active_count") == 2
        assert set(err.get("busy_session_ids") or []) == {sid_a, sid_b}
        titles = err.get("busy_session_titles") or []
        assert set(titles) == {"A", "B"}

        assert set(reg.ids()) == {sid_a, sid_b}
        assert reg.get(sid_c) is None
        # Rollback: store + repo must not stay pointed at the failed target.
        assert srv._sessions.active == sid_a
        assert srv._cfg.repo == str(repo_a)
        assert os.environ.get("HARNESS_REPO") == str(repo_a)
        assert srv._pilot is runner_a
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        srv._cfg.repo = old_repo
        if old_env_repo is None:
            os.environ.pop("HARNESS_REPO", None)
        else:
            os.environ["HARNESS_REPO"] = old_env_repo
        httpd.shutdown()


def test_create_returns_409_and_rolls_back_when_lease_exhausted():
    """Create must not leave SessionStore.active on an unattached session."""
    srv, httpd, port = _spin_server()
    old_runners = srv._runners
    old_pilot = srv._pilot
    try:
        reg = SessionRunnerRegistry(max_concurrent_sessions=2)
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

        before_ids = {s["id"] for s in srv._sessions.list()}

        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(port, "/api/sessions/create", {"title": "C"}, srv._TOKEN)
        assert ei.value.code == 409
        err = json.loads(ei.value.read().decode())
        assert err.get("code") == "lease_exhausted"
        assert err.get("max_concurrent") == 2
        assert err.get("active_count") == 2
        assert set(err.get("busy_session_ids") or []) == {sid_a, sid_b}
        titles = err.get("busy_session_titles") or []
        assert len(titles) == 2

        after_ids = {s["id"] for s in srv._sessions.list()}
        assert after_ids == before_ids
        assert srv._sessions.active == sid_a
        assert set(reg.ids()) == {sid_a, sid_b}
        assert srv._pilot is runner_a
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        httpd.shutdown()


def test_checkpoint_binds_to_turn_session_not_active_view(tmp_path):
    """Mid-turn checkpoint after a view switch must write A's transcript, not B's."""
    import harness.server as srv

    old_sessions = srv._sessions
    old_pilot = srv._pilot
    old_cfg_state = srv._cfg.state_dir
    old_cfg_repo = srv._cfg.repo
    try:
        state_dir = str(tmp_path)
        store = SessionStore(str(tmp_path / "harness_sessions.json"))
        a = store.create(title="A")
        b = store.create(title="B")
        sid_a, sid_b = a["id"], b["id"]
        store.switch(sid_a)

        marker_a = {"history": [{"role": "assistant", "content": "from-A"}], "display": []}
        marker_b = {"history": [{"role": "assistant", "content": "from-B"}], "display": []}

        pilot_a = SimpleNamespace(
            harness_session_id=sid_a,
            export_transcript_data=lambda: marker_a,
        )
        pilot_b = SimpleNamespace(
            harness_session_id=sid_b,
            export_transcript_data=lambda: marker_b,
        )

        srv._sessions = store
        srv._cfg.state_dir = state_dir
        # Simulate: turn started on A, then UI switched active view to B.
        turn_ctx = {"session_id": sid_a, "pilot": pilot_a}
        store.switch(sid_b)
        srv._pilot = pilot_b

        srv._checkpoint_transcript(turn_ctx)

        loaded_a = load_transcript(state_dir, sid_a)
        loaded_b = load_transcript(state_dir, sid_b)
        assert loaded_a == marker_a
        assert loaded_b == []  # B must not be overwritten by A's checkpoint
    finally:
        srv._sessions = old_sessions
        srv._pilot = old_pilot
        srv._cfg.state_dir = old_cfg_state
        srv._cfg.repo = old_cfg_repo


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
        assert err.get("code") == "lease_exhausted"
        assert err.get("max_concurrent") == 2
        assert err.get("active_count") == 2
        assert set(err.get("busy_session_ids") or []) == {sid_a, sid_b}
        titles = err.get("busy_session_titles") or []
        assert len(titles) == 2
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

        resp = _get(port, "/api/session/state", srv._TOKEN)
        payload = json.loads(resp.read().decode())
        assert "runners" in payload
        assert payload["runners"].get(sid) == "idle"
        assert payload.get("active_view_id") == sid
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        httpd.shutdown()


def test_pilot_config_repo_frozen_when_workspace_view_mutates(tmp_path):
    """Mutating _cfg.repo / HARNESS_REPO must not retarget a busy runner's config."""
    srv, httpd, port = _spin_server()
    old_repo = srv._cfg.repo
    old_env = os.environ.get("HARNESS_REPO")
    old_pilot = srv._pilot
    try:
        repo_a = tmp_path / "frozen-a"
        repo_b = tmp_path / "view-b"
        repo_a.mkdir()
        repo_b.mkdir()

        srv._cfg.repo = str(repo_a)
        os.environ["HARNESS_REPO"] = str(repo_a)
        pilot = srv._build_conversational_pilot()
        assert pilot.config is not srv._cfg
        assert pilot.config.repo == str(repo_a)

        # Simulate /api/workspace/open mutating the active-VIEW pointers only.
        srv._cfg.repo = str(repo_b)
        os.environ["HARNESS_REPO"] = str(repo_b)

        assert pilot.config.repo == str(repo_a)
        assert srv._cfg.repo == str(repo_b)
    finally:
        srv._pilot = old_pilot
        srv._cfg.repo = old_repo
        if old_env is None:
            os.environ.pop("HARNESS_REPO", None)
        else:
            os.environ["HARNESS_REPO"] = old_env
        httpd.shutdown()
