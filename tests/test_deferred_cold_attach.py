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
            assert payload.get("state") == "idle"
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
