"""Pilot preferences must not cross independent session boundaries."""

import json
import pytest


def test_deferred_publication_waits_for_swap_lock(owned_server, monkeypatch):
    import threading
    import harness.api.attach as attach

    srv = owned_server
    sid = srv._sessions.create()["id"]
    callbacks = {}
    monkeypatch.setenv("HARNESS_DEFER_COLD_ATTACH", "1")
    monkeypatch.setattr(attach, "schedule_deferred_build",
                        lambda build, **kwargs: callbacks.update(kwargs))
    placeholder = srv._attach_view(sid, defer_cold_build=True)
    real = srv._build_conversational_pilot(config=srv._runner_config_snapshot())
    entered = threading.Event()
    finished = threading.Event()

    def publish():
        entered.set()
        try:
            callbacks["on_done"](real)
        finally:
            finished.set()

    worker = threading.Thread(target=publish, daemon=True)
    try:
        with srv._pilot_swap_lock:
            worker.start()
            assert entered.wait(1)
            assert not finished.wait(0.1)
            assert srv._runners.get(sid) is placeholder
        worker.join(timeout=2)
        assert finished.is_set()
        assert srv._runners.get(sid) is real
    finally:
        worker.join(timeout=2)


class Handler:
    def _send(self, code, body):
        self.code = code
        self.body = json.loads(body) if isinstance(body, str) else body


@pytest.mark.parametrize("sid", ["", "foreign"])
def test_http_swap_rejects_missing_or_foreign_session(owned_server, sid):
    srv = owned_server
    before = srv._cfg.driver
    h = Handler()
    srv.Handler._swap_pilot(h, "wrong-model", sid)
    assert h.code == 409
    assert srv._cfg.driver == before


def test_scoped_config_rejects_old_view(owned_server):
    srv = owned_server
    a = srv._sessions.active
    b = srv._sessions.create()["id"]
    srv._attach_view(b, load_transcript_on_create=False)
    h = Handler()
    srv.Handler._get_config(h, a)
    assert h.code == 409
    srv.Handler._get_config(h, b)
    assert h.code == 200
    assert h.body["session_id"] == b


def test_swap_rechecks_session_after_readiness(owned_server, monkeypatch):
    srv = owned_server
    a = srv._sessions.active
    before = srv._cfg.driver
    def switch():
        b = srv._sessions.create()["id"]
        return srv._attach_view(b, load_transcript_on_create=False)
    monkeypatch.setattr(srv, "_ensure_active_pilot_ready", switch)
    h = Handler()
    srv.Handler._swap_pilot(h, "wrong-model", a)
    assert h.code == 409
    assert srv._cfg.driver == before


def test_reasoning_preferences_do_not_write_environment(owned_server, monkeypatch):
    import os
    srv = owned_server
    monkeypatch.setenv("HARNESS_CODEX_REASONING_EFFORT", "low")
    a = srv._sessions.active
    h = Handler()
    srv.Handler._set_pilot_preferences(h, {"session_id": a, "reasoning_effort": "high"})
    assert h.code == 200
    assert os.environ["HARNESS_CODEX_REASONING_EFFORT"] == "low"
    assert srv._sessions.pilot_preferences(a)["reasoning_effort"] == "high"
    b = srv._sessions.create()["id"]
    srv._attach_view(b, load_transcript_on_create=False)
    srv.Handler._set_pilot_preferences(h, {"session_id": a, "reasoning_effort": "max"})
    assert h.code == 409
    assert srv._sessions.pilot_preferences(b).get("reasoning_effort") != "high"


def test_deferred_driver_survives_reload_eviction_and_reaches_driver(owned_server, monkeypatch):
    from harness.sessions import SessionStore
    srv = owned_server
    a_id, a = srv._sessions.active, srv._pilot
    a._busy.acquire()
    try:
        h = Handler()
        srv.Handler._swap_pilot(h, "stub-oracle", a_id)
        assert h.code == 200 and h.body["deferred"]
    finally:
        a._busy.release()
    b_id = srv._sessions.create()["id"]
    srv._attach_view(b_id, load_transcript_on_create=False)
    srv.Handler._swap_pilot(h, "stub-oracle-v2", b_id)
    assert h.code == 200
    srv._runners.drop(a_id, notify=False)
    monkeypatch.setattr(srv, "_sessions", SessionStore(srv._sessions.path))
    srv._sessions.switch(a_id)
    restored = srv._attach_view(a_id, load_transcript_on_create=False)
    assert restored.config.driver == "stub-oracle"
    assert restored.pilot.name == "stub-oracle"
    assert srv._ensure_pilot_matches_driver()
    assert srv._sessions.pilot_preferences(b_id)["driver"] == "stub-oracle-v2"


def test_busy_driver_stays_put_until_its_next_turn(owned_server):
    srv = owned_server
    a_id, a = srv._sessions.active, srv._pilot
    original = a.pilot
    a._busy.acquire()
    try:
        h = Handler()
        srv.Handler._swap_pilot(h, "stub-oracle", a_id)
        b_id = srv._sessions.create()["id"]
        srv._attach_view(b_id, load_transcript_on_create=False)
        srv.Handler._swap_pilot(h, "stub-oracle-v2", b_id)
        srv._sessions.switch(a_id)
        srv._attach_view(a_id, load_transcript_on_create=False)
        assert a.pilot is original
        assert not srv._ensure_pilot_matches_driver()
    finally:
        a._busy.release()
    assert srv._ensure_pilot_matches_driver()
    assert srv._pilot.pilot.name == "stub-oracle"


def test_background_session_applies_deferred_driver_without_switch(owned_server):
    srv = owned_server
    a_id, a = srv._sessions.active, srv._pilot
    a._busy.acquire()
    try:
        h = Handler()
        srv.Handler._swap_pilot(h, "stub-oracle", a_id)
    finally:
        a._busy.release()
    b_id = srv._sessions.create()["id"]
    b = srv._attach_view(b_id, load_transcript_on_create=False)
    srv.Handler._swap_pilot(h, "stub-oracle-v2", b_id)
    b = srv._pilot
    assert srv._ensure_session_driver(a_id)
    assert srv._runners.get(a_id).pilot.name == "stub-oracle"
    assert srv._pilot is b
    assert srv._cfg.driver == "stub-oracle-v2"
    assert srv._sessions.active == b_id


def test_concurrent_turn_reasoning_reaches_streaming_provider_body(owned_server, monkeypatch):
    import threading
    from pmharness.drivers.codex_responses import CodexResponsesDriver
    from pmharness.drivers.base import DriverResponse
    from harness.reasoning_effort import current_swarm_reasoning_effort
    srv = owned_server
    a_id, a = srv._sessions.active, srv._pilot
    b_id = srv._sessions.create()["id"]
    b = srv._attach_view(b_id, load_transcript_on_create=False)
    srv._sessions.pilot_preferences(a_id, updates={"reasoning_effort": "high", "swarm_reasoning_effort": "max"})
    srv._sessions.pilot_preferences(b_id, updates={"reasoning_effort": "low", "swarm_reasoning_effort": "medium"})
    barrier = threading.Barrier(2)
    bodies, workers, errors = {}, {}, []
    class RecordingDriver(CodexResponsesDriver):
        def chat(self, *args, **kwargs):
            raise AssertionError("streaming expected")
        def chat_stream(self, messages, **kwargs):
            barrier.wait(timeout=5)
            if self.name == a_id:
                srv._sessions.pilot_preferences(a_id, updates={"reasoning_effort": "none", "swarm_reasoning_effort": "low"})
            bodies[self.name] = self._build_body(messages)
            workers[self.name] = current_swarm_reasoning_effort()
            return DriverResponse(text="Already complete.", meta={"finish_reason": "completed"})
    for sid, runner in ((a_id, a), (b_id, b)):
        runner.pilot = RecordingDriver(sid, "gpt-test")
    def send(runner):
        try:
            list(runner.send("already complete"))
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=send, args=(runner,)) for runner in (a, b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert not errors
    assert bodies[a_id]["reasoning"]["effort"] == "high"
    assert bodies[b_id]["reasoning"]["effort"] == "low"
    assert workers == {a_id: "max", b_id: "medium"}
    assert srv._sessions.pilot_preferences(a_id)["reasoning_effort"] == "none"


def test_swarm_submission_preserves_turn_reasoning_context(owned_server, monkeypatch):
    import queue
    from harness.reasoning_effort import _turn_efforts, current_swarm_reasoning_effort

    session = owned_server._pilot
    monkeypatch.setattr(session, "_resource_pressure_admit", lambda **_: True)
    monkeypatch.setattr(session, "_swarm_at_capacity", lambda: False)
    result = queue.Queue()
    token = _turn_efforts.set(("high", "max"))
    try:
        assert session._submit_swarm(lambda: result.put(current_swarm_reasoning_effort()))
    finally:
        _turn_efforts.reset(token)
    assert result.get(timeout=5) == "max"


def test_return_to_busy_session_restores_its_pilot_choice(owned_server):
    srv = owned_server
    a_id = srv._sessions.active
    a = srv._pilot
    a_driver = a.config.driver
    a._busy.acquire()
    try:
        b_id = srv._sessions.create()["id"]
        b = srv._attach_view(b_id, load_transcript_on_create=False)
        # Defer a different selection in B, keeping the test provider-free.
        b._busy.acquire()
        try:
            h = Handler()
            srv.Handler._swap_pilot(h, "different-pilot-for-B")
            assert h.code == 200 and h.body["deferred"] is True
            srv._sessions.switch(a_id)
            srv._attach_view(a_id, load_transcript_on_create=False)
            assert srv._pilot is a
            assert a.config.driver == a_driver
            assert srv._cfg.driver == a_driver
        finally:
            b._busy.release()
    finally:
        a._busy.release()


def test_deferred_choice_survives_visiting_another_session(owned_server):
    srv = owned_server
    a_id, a = srv._sessions.active, srv._pilot
    a._busy.acquire()
    try:
        h = Handler()
        srv.Handler._swap_pilot(h, "next-pilot-for-A")
        assert h.code == 200 and h.body["deferred"] is True
        b_id = srv._sessions.create()["id"]
        # Factory avoids building the intentionally nonexistent model spec.
        from dataclasses import replace
        b = srv._attach_view(b_id, factory=lambda: srv._build_conversational_pilot(
            config=replace(a.config)), load_transcript_on_create=False)
        b._busy.acquire()
        try:
            srv.Handler._swap_pilot(h, "next-pilot-for-B")
            srv._sessions.switch(a_id)
            srv._attach_view(a_id, load_transcript_on_create=False)
            assert a.config.driver != "next-pilot-for-A"
            assert srv._cfg.driver == "next-pilot-for-A"
        finally:
            b._busy.release()
    finally:
        a._busy.release()
