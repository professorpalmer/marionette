"""Wave 3: deferred cold attach (Hermes-style idle+transcript first)."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from harness.deferred_attach import (
    DeferredPilotPlaceholder,
    defer_cold_attach_enabled,
    is_deferred_placeholder,
    normalize_transcript_payload,
)
from harness.session_runners import LeaseExhaustedError, SessionRunnerRegistry
from harness.sessions import save_transcript


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
        _tokens_used=0,
        _tokens_in=0,
        _tokens_out=0,
        _tokens_cached=0,
        _worker_cost_usd=0.0,
        _worker_tokens_in=0,
        _worker_tokens_out=0,
    )


def _idle_runner(*, sid: str = "", history=None) -> SimpleNamespace:
    hist = history or {"history": [], "display": [], "job_ids": []}

    def _export():
        return hist

    return SimpleNamespace(
        _busy=threading.Lock(),
        _state="idle",
        _history=list(hist.get("history") or []),
        state_dir="/tmp/fake-runner",
        harness_session_id=sid,
        _auto_distill=False,
        _mcp=None,
        _session_store=None,
        export_transcript_data=_export,
        load_history=lambda h: None,
        _tokens_used=0,
        _tokens_in=0,
        _tokens_out=0,
        _tokens_cached=0,
        _worker_cost_usd=0.0,
        _worker_tokens_in=0,
        _worker_tokens_out=0,
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


def test_normalize_transcript_payload_shapes():
    assert normalize_transcript_payload(None) == {
        "history": [],
        "display": [],
        "job_ids": [],
    }
    assert normalize_transcript_payload([{"role": "user", "content": "hi"}])[
        "history"
    ][0]["content"] == "hi"
    assert normalize_transcript_payload(
        {"history": [1], "display": [2], "job_ids": ["j"]}
    ) == {"history": [1], "display": [2], "job_ids": ["j"]}


def test_defer_cold_attach_kill_switch(monkeypatch):
    monkeypatch.setenv("HARNESS_DEFER_COLD_ATTACH", "0")
    assert defer_cold_attach_enabled() is False
    monkeypatch.setenv("HARNESS_DEFER_COLD_ATTACH", "1")
    assert defer_cold_attach_enabled() is True


def test_warm_attach_does_not_call_factory():
    """Warm path must never invoke the factory (measured fast path)."""
    import harness.server as srv

    old_runners = srv._runners
    old_pilot = srv._pilot
    try:
        reg = SessionRunnerRegistry(max_concurrent_sessions=3)
        a = srv._sessions.create(title="A")
        sid = a["id"]
        runner = _idle_runner(sid=sid)
        reg.get_or_create(sid, lambda: runner)
        srv._runners = reg
        srv._pilot = runner

        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise AssertionError("factory must not run on warm attach")

        out = srv._attach_view(sid, factory=boom, defer_cold_build=True)
        assert out is runner
        assert calls["n"] == 0
        assert reg.get(sid) is runner
        assert srv._pilot is runner
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot


def test_attach_does_not_interrupt_other_busy_runners():
    """Attaching B must not cancel/interrupt busy runner A (already registry truth)."""
    import harness.server as srv

    old_runners = srv._runners
    old_pilot = srv._pilot
    try:
        reg = SessionRunnerRegistry(max_concurrent_sessions=3)
        a = srv._sessions.create(title="A")
        b = srv._sessions.create(title="B")
        sid_a, sid_b = a["id"], b["id"]

        runner_a = _busy_runner()
        interrupted = {"n": 0}

        def interrupt():
            interrupted["n"] += 1

        runner_a.interrupt = interrupt
        runner_a.cancel = interrupt
        runner_b = _idle_runner(sid=sid_b)

        reg.get_or_create(sid_a, lambda: runner_a)
        reg.set_active_view(sid_a)
        srv._runners = reg
        srv._pilot = runner_a
        srv._sessions.switch(sid_a)

        out = srv._attach_view(sid_b, factory=lambda: runner_b)
        assert out is runner_b
        assert reg.get(sid_a) is runner_a
        assert runner_a._busy.locked()
        assert interrupted["n"] == 0
        assert reg.active_view_id == sid_b
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot


def test_cold_deferred_attach_returns_placeholder_then_swaps(tmp_path, monkeypatch):
    """Cold defer: placeholder + transcript immediately; real pilot via latch."""
    import harness.server as srv

    monkeypatch.setenv("HARNESS_DEFER_COLD_ATTACH", "1")
    old_runners = srv._runners
    old_pilot = srv._pilot
    old_state = srv._cfg.state_dir
    try:
        state_dir = str(tmp_path)
        srv._cfg.state_dir = state_dir
        reg = SessionRunnerRegistry(max_concurrent_sessions=3)
        srv._runners = reg

        a = srv._sessions.create(title="Cold")
        sid = a["id"]
        marker = {
            "history": [{"role": "user", "content": "hello-cold"}],
            "display": [{"role": "user", "content": "hello-cold"}],
            "job_ids": [],
        }
        save_transcript(state_dir, sid, marker)

        built = threading.Event()
        real = _idle_runner(sid=sid, history=marker)

        def slow_build():
            # Simulate heavy ConversationalSession construction.
            time.sleep(0.05)
            built.set()
            return real

        with patch.object(srv, "_build_conversational_pilot", side_effect=slow_build):
            out = srv._attach_view(sid, defer_cold_build=True)

        assert is_deferred_placeholder(out)
        assert isinstance(out, DeferredPilotPlaceholder)
        assert out.export_transcript_data()["history"][0]["content"] == "hello-cold"
        # Latch is the contract: wait for real pilot without racing turns.
        ready = out.ensure_ready(timeout=5.0)
        assert built.is_set()
        assert ready is real
        assert reg.get(sid) is real
        assert srv._pilot is real
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        srv._cfg.state_dir = old_state


def test_deferred_build_preserves_post_attach_load_history(tmp_path, monkeypatch):
    """load_history on the placeholder must win over attach-time empty disk."""
    import harness.server as srv

    monkeypatch.setenv("HARNESS_DEFER_COLD_ATTACH", "1")
    old_runners = srv._runners
    old_pilot = srv._pilot
    old_state = srv._cfg.state_dir
    try:
        state_dir = str(tmp_path)
        srv._cfg.state_dir = state_dir
        reg = SessionRunnerRegistry(max_concurrent_sessions=3)
        srv._runners = reg

        a = srv._sessions.create(title="Cold")
        sid = a["id"]
        turns = [
            {"role": "user", "content": "late-load"},
            {"role": "assistant", "content": "ok"},
        ]
        loaded: list = []

        def capturing_load(messages):
            loaded.clear()
            if isinstance(messages, dict):
                loaded.extend(messages.get("history") or [])
            else:
                loaded.extend(list(messages or []))

        real = _idle_runner(sid=sid)
        real.load_history = capturing_load
        real.export_history = lambda: list(loaded)
        gate = threading.Event()

        def blocked_build():
            gate.wait(timeout=5.0)
            return real

        with patch.object(srv, "_build_conversational_pilot", side_effect=blocked_build):
            out = srv._attach_view(sid, defer_cold_build=True)
            assert is_deferred_placeholder(out)
            out.load_history(turns)
            assert out.export_history() == turns
        gate.set()
        ready = out.ensure_ready(timeout=5.0)
        assert ready is real
        assert loaded == turns
        assert reg.get(sid) is real
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        srv._cfg.state_dir = old_state


def test_switch_response_includes_idle_transcript(tmp_path, monkeypatch):
    """ /api/sessions/switch returns state + transcript without waiting on build."""
    import harness.server as srv

    monkeypatch.setenv("HARNESS_DEFER_COLD_ATTACH", "1")
    srv, httpd, port = _spin_server()
    old_runners = srv._runners
    old_pilot = srv._pilot
    old_state = srv._cfg.state_dir
    try:
        state_dir = str(tmp_path)
        srv._cfg.state_dir = state_dir
        reg = SessionRunnerRegistry(max_concurrent_sessions=3)

        a = srv._sessions.create(title="A")
        b = srv._sessions.create(title="B")
        sid_a, sid_b = a["id"], b["id"]
        runner_a = _idle_runner(sid=sid_a)
        reg.get_or_create(sid_a, lambda: runner_a)
        reg.set_active_view(sid_a)
        srv._runners = reg
        srv._pilot = runner_a
        srv._sessions.switch(sid_a)

        marker = {
            "history": [{"role": "assistant", "content": "from-disk"}],
            "display": [],
            "job_ids": [],
        }
        save_transcript(state_dir, sid_b, marker)

        gate = threading.Event()
        real_b = _idle_runner(sid=sid_b, history=marker)

        def blocked_build():
            gate.wait(timeout=5.0)
            return real_b

        with patch.object(srv, "_build_conversational_pilot", side_effect=blocked_build):
            resp = _post(port, "/api/sessions/switch", {"id": sid_b}, srv._TOKEN)
            assert resp.status == 200
            payload = json.loads(resp.read().decode())
            assert payload.get("ok") is True
            assert payload.get("active") == sid_b
            assert payload.get("state") == "attaching"
            assert payload.get("transcript", {}).get("history", [])[0]["content"] == "from-disk"
            assert is_deferred_placeholder(srv._pilot)
            # Build still blocked — proves switch did not wait on ConversationalSession.
            assert not gate.is_set()
        gate.set()
        srv._pilot.ensure_ready(timeout=5.0)
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        srv._cfg.state_dir = old_state
        httpd.shutdown()


def test_ensure_active_pilot_ready_blocks_until_swap(tmp_path, monkeypatch):
    import harness.server as srv

    monkeypatch.setenv("HARNESS_DEFER_COLD_ATTACH", "1")
    old_runners = srv._runners
    old_pilot = srv._pilot
    old_state = srv._cfg.state_dir
    try:
        state_dir = str(tmp_path)
        srv._cfg.state_dir = state_dir
        reg = SessionRunnerRegistry(max_concurrent_sessions=2)
        srv._runners = reg
        a = srv._sessions.create(title="A")
        sid = a["id"]

        real = _idle_runner(sid=sid)
        started = threading.Event()

        def slow_build():
            started.wait(timeout=5.0)
            time.sleep(0.02)
            return real

        with patch.object(srv, "_build_conversational_pilot", side_effect=slow_build):
            srv._attach_view(sid, defer_cold_build=True)
            assert is_deferred_placeholder(srv._pilot)
            started.set()
            ready = srv._ensure_active_pilot_ready(timeout=5.0)
            assert ready is real
            assert srv._pilot is real
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        srv._cfg.state_dir = old_state


def test_registry_replace_and_skip_deferred_eviction():
    reg = SessionRunnerRegistry(max_concurrent_sessions=1)
    ph = DeferredPilotPlaceholder(session_id="s1", state_dir="/tmp", transcript=[])
    reg.get_or_create("s1", lambda: ph)
    # At capacity with a building placeholder: cannot create s2 (no idle to evict).
    with pytest.raises(LeaseExhaustedError):
        reg.get_or_create("s2", lambda: _idle_runner(sid="s2"))
    real = _idle_runner(sid="s1")
    old = reg.replace("s1", real, notify=False)
    assert old is ph
    assert reg.get("s1") is real


def test_history_for_pilot_swap_prefers_placeholder_transcript():
    """Empty _history must not win over non-empty export_history / transcript."""
    import harness.server as srv

    turns = [
        {"role": "user", "content": "keep-me"},
        {"role": "assistant", "content": "ok"},
    ]
    ph = DeferredPilotPlaceholder(
        session_id="s1",
        state_dir="/tmp",
        transcript={"history": turns, "display": [], "job_ids": []},
    )
    assert ph._history == []
    assert srv._history_for_pilot_swap(ph) == turns


def test_perform_pilot_swap_preserves_deferred_transcript(tmp_path, monkeypatch):
    """Idle pilot swap must not wipe placeholder turns (T1)."""
    import harness.server as srv

    monkeypatch.setenv("HARNESS_DEFER_COLD_ATTACH", "1")
    old_runners = srv._runners
    old_pilot = srv._pilot
    old_state = srv._cfg.state_dir
    old_driver = srv._cfg.driver
    try:
        state_dir = str(tmp_path)
        srv._cfg.state_dir = state_dir
        reg = SessionRunnerRegistry(max_concurrent_sessions=3)
        srv._runners = reg

        a = srv._sessions.create(title="Swap")
        sid = a["id"]
        turns = [
            {"role": "user", "content": "keep-me"},
            {"role": "assistant", "content": "ok"},
        ]
        marker = {"history": turns, "display": turns[:1], "job_ids": []}
        save_transcript(state_dir, sid, marker)

        real = _idle_runner(sid=sid, history=marker)
        gate = threading.Event()

        def blocked_build():
            gate.wait(timeout=5.0)
            return real

        with patch.object(srv, "_build_conversational_pilot", side_effect=blocked_build):
            out = srv._attach_view(sid, defer_cold_build=True)
            assert is_deferred_placeholder(out)
            # Turns live on the placeholder transcript, not _history.
            assert out._history == []
            assert out.export_history() == turns
            gate.set()
            ready = out.ensure_ready(timeout=5.0)
            assert ready is real
            assert real._history == turns

        replacement = _idle_runner(sid=sid)

        def _make_replacement(*_a, **_k):
            return replacement

        with patch.object(srv, "ConversationalSession", side_effect=_make_replacement):
            srv._perform_pilot_swap(srv._cfg.driver or "test-driver")

        assert srv._pilot is replacement
        assert replacement._history == turns
        assert reg.get(sid) is replacement
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        srv._cfg.state_dir = old_state
        srv._cfg.driver = old_driver


def test_failed_deferred_attach_rebuilds_on_reattach(tmp_path, monkeypatch):
    """mark_failed must not stick forever — next attach drops and rebuilds (T2)."""
    import harness.server as srv

    monkeypatch.setenv("HARNESS_DEFER_COLD_ATTACH", "1")
    old_runners = srv._runners
    old_pilot = srv._pilot
    old_state = srv._cfg.state_dir
    try:
        state_dir = str(tmp_path)
        srv._cfg.state_dir = state_dir
        reg = SessionRunnerRegistry(max_concurrent_sessions=3)
        srv._runners = reg

        a = srv._sessions.create(title="Fail")
        sid = a["id"]
        builds = {"n": 0}

        def flaky_build():
            builds["n"] += 1
            if builds["n"] == 1:
                raise RuntimeError("cold build boom")
            return _idle_runner(sid=sid)

        with patch.object(srv, "_build_conversational_pilot", side_effect=flaky_build):
            out = srv._attach_view(sid, defer_cold_build=True)
            assert is_deferred_placeholder(out)
            with pytest.raises(RuntimeError, match="cold build boom"):
                out.ensure_ready(timeout=5.0)
            assert out.build_error is not None
            assert reg.get(sid) is out
            assert out.defer_building is False

            # Warm re-attach must drop the failed shell and start a fresh build.
            out2 = srv._attach_view(sid, defer_cold_build=True)
            assert is_deferred_placeholder(out2)
            assert out2 is not out
            assert out2.build_error is None
            ready = out2.ensure_ready(timeout=5.0)
            assert builds["n"] == 2
            assert reg.get(sid) is ready
            assert not is_deferred_placeholder(ready)
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        srv._cfg.state_dir = old_state


def test_building_placeholder_reports_busy_for_lease(tmp_path, monkeypatch):
    """Building shells report attaching (not running) but still hold a lease (T3)."""
    from harness.session_runners import _is_busy, build_lease_exhausted_payload

    ph = DeferredPilotPlaceholder(session_id="s1", state_dir="/tmp", transcript=[])
    assert ph.defer_building is True
    assert ph.state() == "building"
    assert ph.is_turn_busy() is True
    assert _is_busy(ph) is True
    # /api/reviews must not AttributeError while the shell is still building.
    with ph._pending_reviews_lock:
        assert list(ph._pending_reviews.values()) == []

    reg = SessionRunnerRegistry(max_concurrent_sessions=1)
    reg.get_or_create("s1", lambda: ph)
    # Composer must see "attaching" (not "running") so New Session does not
    # flash turn-thinking chrome; lease still treats the shell as busy.
    assert reg.status("s1") == "attaching"
    payload = build_lease_exhausted_payload(reg)
    assert payload["busy_session_ids"] == ["s1"]

    with pytest.raises(LeaseExhaustedError):
        reg.get_or_create("s2", lambda: _idle_runner(sid="s2"))


def test_mutation_apis_gate_on_deferred_ready(tmp_path, monkeypatch):
    """Rewind / compact / steer wait on ensure_ready (no AttributeError) (T4)."""
    import harness.server as srv

    monkeypatch.setenv("HARNESS_DEFER_COLD_ATTACH", "1")
    srv, httpd, port = _spin_server()
    old_runners = srv._runners
    old_pilot = srv._pilot
    old_state = srv._cfg.state_dir
    try:
        state_dir = str(tmp_path)
        srv._cfg.state_dir = state_dir
        reg = SessionRunnerRegistry(max_concurrent_sessions=3)
        srv._runners = reg

        a = srv._sessions.create(title="Mut")
        sid = a["id"]
        turns = [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ]
        save_transcript(
            state_dir,
            sid,
            {"history": turns, "display": turns, "job_ids": []},
        )

        # Real ConversationalSession so rewind/compact methods exist after ready.
        real = srv._build_conversational_pilot()
        real.load_history({"history": turns, "display": turns, "job_ids": []})
        real.harness_session_id = sid
        gate = threading.Event()

        def blocked_build():
            gate.wait(timeout=5.0)
            return real

        with patch.object(srv, "_build_conversational_pilot", side_effect=blocked_build):
            srv._attach_view(sid, defer_cold_build=True)
            assert is_deferred_placeholder(srv._pilot)
            srv._sessions.switch(sid)

            # Release build shortly after requests land so ensure_ready succeeds.
            def _release():
                time.sleep(0.05)
                gate.set()

            threading.Thread(target=_release, daemon=True).start()

            resp = _post(
                port,
                "/api/session/rewind",
                {"user_ordinal": 1},
                srv._TOKEN,
            )
            assert resp.status == 200
            body = json.loads(resp.read().decode())
            assert body.get("ok") is True
            assert not is_deferred_placeholder(srv._pilot)

            # Compact + steer against the now-ready pilot (no AttributeError).
            # The three-turn transcript is too small to actually shrink, so the
            # endpoint reports a truthful no-op instead of a false success.
            try:
                _post(port, "/api/session/compact", {}, srv._TOKEN)
                assert False, "tiny transcript should report no-op compaction"
            except urllib.error.HTTPError as e:
                assert e.code == 409
                noop = json.loads(e.read().decode())
                assert noop.get("ok") is False
                assert noop.get("compacted") is False

            resp3 = _post(
                port, "/api/session/steer", {"text": "nudge"}, srv._TOKEN
            )
            assert resp3.status == 200
            assert json.loads(resp3.read().decode()).get("ok") is True
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        srv._cfg.state_dir = old_state
        httpd.shutdown()


def test_mutation_apis_409_when_deferred_build_fails(tmp_path, monkeypatch):
    """Failed cold build → mutation APIs return clear 409, not AttributeError."""
    import harness.server as srv

    monkeypatch.setenv("HARNESS_DEFER_COLD_ATTACH", "1")
    srv, httpd, port = _spin_server()
    old_runners = srv._runners
    old_pilot = srv._pilot
    old_state = srv._cfg.state_dir
    try:
        state_dir = str(tmp_path)
        srv._cfg.state_dir = state_dir
        reg = SessionRunnerRegistry(max_concurrent_sessions=3)
        srv._runners = reg

        a = srv._sessions.create(title="FailMut")
        sid = a["id"]

        def boom():
            raise RuntimeError("build dead")

        with patch.object(srv, "_build_conversational_pilot", side_effect=boom):
            srv._attach_view(sid, defer_cold_build=True)
            srv._sessions.switch(sid)
            # Wait until mark_failed latches.
            deadline = time.time() + 5.0
            while time.time() < deadline:
                ph = srv._runners.get(sid)
                if (
                    is_deferred_placeholder(ph)
                    and getattr(ph, "build_error", None) is not None
                ):
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("build_error never set")

            try:
                _post(
                    port,
                    "/api/session/rewind",
                    {"user_ordinal": 0},
                    srv._TOKEN,
                )
                raise AssertionError("expected HTTPError 409")
            except urllib.error.HTTPError as e:
                assert e.code == 409
                err = json.loads(e.read().decode())
                assert err.get("code") == "pilot_not_ready"
                assert "build" in (err.get("error") or "").lower()
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        srv._cfg.state_dir = old_state
        httpd.shutdown()


def test_deferred_cold_attach_restores_pending_command_approval(tmp_path, monkeypatch):
    """Cold deferred hydrate must rebuild decidable pending DANGER approvals."""
    import hashlib

    import harness.server as srv
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    monkeypatch.setenv("HARNESS_DEFER_COLD_ATTACH", "1")
    old_runners = srv._runners
    old_pilot = srv._pilot
    old_state = srv._cfg.state_dir
    old_repo = srv._cfg.repo
    try:
        state_dir = str(tmp_path / "state")
        os.makedirs(state_dir, exist_ok=True)
        repo = str(tmp_path / "repo")
        os.makedirs(repo, exist_ok=True)
        srv._cfg.state_dir = state_dir
        srv._cfg.repo = repo
        reg = SessionRunnerRegistry(max_concurrent_sessions=3)
        srv._runners = reg

        created = srv._sessions.create(title="Approval")
        sid = created["id"]
        command = "ssh prod reboot"
        command_hash = hashlib.sha256(command.encode()).hexdigest()
        workspace = os.path.realpath(repo)
        marker = {
            "history": [{"role": "user", "content": "go"}],
            "display": [
                {"type": "message", "role": "user", "text": "go"},
                {
                    "type": "command_approval",
                    "id": "call-deferred",
                    "command": command,
                    "command_hash": command_hash,
                    "session_id": sid,
                    "workspace_root": workspace,
                    "category": "remote",
                    "reason": "ssh",
                    "matched": "ssh",
                    "status": "pending",
                },
            ],
            "job_ids": [],
        }
        save_transcript(state_dir, sid, marker)

        gate = threading.Event()

        def blocked_build():
            gate.wait(timeout=5.0)
            return ConversationalSession(
                HarnessConfig(repo=repo, state_dir=str(tmp_path / "st"))
            )

        with patch.object(srv, "_build_conversational_pilot", side_effect=blocked_build):
            out = srv._attach_view(sid, defer_cold_build=True)
            assert is_deferred_placeholder(out)
            display = out.export_transcript_data()["display"]
            assert any(
                isinstance(row, dict)
                and row.get("type") == "command_approval"
                and row.get("status") == "pending"
                and row.get("command_hash") == command_hash
                for row in display
            )
            gate.set()
            real = out.ensure_ready(timeout=5.0)

        assert isinstance(real, ConversationalSession)
        assert real.harness_session_id == sid
        assert command_hash in real._pending_command_approvals
        assert command_hash not in real._approved_commands
        decided = real.decide_command_approval(
            command_hash=command_hash,
            workspace_root=workspace,
            approve=True,
        )
        assert decided is not None
        assert command_hash not in real._pending_command_approvals
        assert command_hash in real._approved_commands
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        srv._cfg.state_dir = old_state
        srv._cfg.repo = old_repo
