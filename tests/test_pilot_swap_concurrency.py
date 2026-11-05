"""Concurrency test for pilot-swap / rebuild interleaving.

Two threads hammer the pilot-rebind path at the same time (mirroring a
/api/pilot swap firing while a workspace-switch rebuild runs). The swap lock
must serialize the history-copy/rebind steps so the final _pilot is a single
consistent object with its carried-over _history intact and no torn state.
"""
import threading

import harness.server as srv

import pytest

pytestmark = pytest.mark.usefixtures("owned_server")


def test_concurrent_rebuild_no_torn_state(monkeypatch):
    # The import-time driver can come from the host config and need a live CLI login.
    monkeypatch.setattr(srv._cfg, "driver", "stub-oracle-v2")
    # Seed a known history to carry across rebinds.
    marker = [{"role": "user", "content": "carry-me"}]
    srv._pilot._history = list(marker)

    errors = []
    barrier = threading.Barrier(2)

    def worker():
        try:
            barrier.wait()
            for _ in range(25):
                srv._rebuild_pilot_and_session()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"rebuild raised under concurrency: {errors}"

    # Single consistent object: the global _pilot and the session's pilot view
    # agree, and history carried over intact (not empty / torn).
    p = srv._pilot
    assert p is not None
    assert p._history == marker
    assert p._mcp is srv._mcp
    # _session was rebound in lockstep and points at the pilot's store.
    assert srv._session.state_dir == p.state_dir


def test_swap_defers_busy_without_taking_lock():
    # A busy turn must stage the preference fast, without entering the swap lock.
    import json as _json
    srv._pilot._busy.acquire()
    prev_driver = srv._cfg.driver
    try:
        # Hold the swap lock so that IF _swap_pilot tried to acquire it for a
        # rebuild, it would block; deferred staging must short-circuit before that.
        with srv._pilot_swap_lock:
            captured = {}

            class FakeHandler:
                def _send(self, code, body):
                    captured["code"] = code
                    captured["body"] = _json.loads(body) if isinstance(body, str) else body
                    return None

            live_before = srv._pilot
            srv.Handler._swap_pilot(FakeHandler(), "some-model")
            assert captured["code"] == 200
            assert captured["body"].get("deferred") is True
            assert captured["body"].get("driver") == "some-model"
            assert srv._cfg.driver == "some-model"
            assert srv._pilot is live_before
    finally:
        srv._cfg.driver = prev_driver
        srv._pilot._busy.release()


def test_state_isolation_preserves_previous_queue_owner(owned_server, tmp_path, monkeypatch):
    from tests.conftest import _isolate_server_session_state

    old_pilot = owned_server._pilot
    old_store = owned_server._sessions
    old_registry = owned_server._runners
    old_config = owned_server._cfg
    old_pilot.enqueue_prompt("belongs to previous test")
    owner = old_pilot._prompt_queue_owner
    with monkeypatch.context() as isolated:
        _isolate_server_session_state(owned_server, tmp_path, isolated)
        assert owned_server._sessions.active is None
        assert owned_server._runners.active_view_id is None
        session = owned_server._sessions.create()
        fresh = owned_server._attach_view(session["id"], load_transcript_on_create=False)
        assert owned_server._ensure_active_pilot_ready() is fresh
        assert fresh is not old_pilot
        assert fresh.list_prompts() == []
        assert fresh._session_store is owned_server._sessions
        assert old_pilot._session_store is old_store
        assert old_pilot._prompt_queue_owner == owner
        assert [item["text"] for item in old_pilot.list_prompts()] == ["belongs to previous test"]
        owned_server._sessions.flush()
    assert owned_server._pilot is old_pilot
    assert owned_server._sessions is old_store
    assert owned_server._runners is old_registry
    assert owned_server._cfg is old_config
    assert owned_server._ensure_active_pilot_ready() is old_pilot
