"""Session switch/create must never move one session's pilot state into another."""

import json


class Handler:
    def _send(self, code, body):
        self.code = code
        self.body = json.loads(body) if isinstance(body, str) else body


P_HISTORY = [
    {"role": "user", "content": "private question for P"},
    {"role": "assistant", "content": "answer for P"},
]


def _seed_active_history(srv):
    pilot = srv._ensure_active_pilot_ready()
    pilot.load_history({"history": list(P_HISTORY), "display": [], "job_ids": []})
    return pilot


def test_save_active_transcript_skips_store_active_without_view(owned_server):
    """Store moved to cold A while the view (and _pilot) is still on P."""
    from harness.sessions import load_transcript

    srv = owned_server
    p = srv._sessions.active
    _seed_active_history(srv)
    a = srv._sessions.create()["id"]
    assert srv._sessions.active == a
    assert srv._runners.active_view_id == p
    assert srv._runners.get(a) is None

    srv._save_active_transcript()

    saved = load_transcript(srv._sessions_state_dir(), a)
    history = saved.get("history") if isinstance(saved, dict) else saved
    assert not history


def test_save_active_transcript_persists_active_view(owned_server):
    from harness.sessions import load_transcript

    srv = owned_server
    p = srv._sessions.active
    _seed_active_history(srv)

    srv._save_active_transcript()

    saved = load_transcript(srv._sessions_state_dir(), p)
    history = saved.get("history") if isinstance(saved, dict) else saved
    assert [m.get("content") for m in history] == [m["content"] for m in P_HISTORY]


def test_session_create_does_not_wipe_repointed_pilot(owned_server, monkeypatch, tmp_path):
    """A concurrent switch back to P after create's attach must keep P's history."""
    import harness.api.attach as attach
    from harness.api.sessions import post_sessions_create

    srv = owned_server
    p = srv._sessions.active
    p_pilot = _seed_active_history(srv)
    monkeypatch.setattr(srv._cfg, "repo", str(tmp_path))
    monkeypatch.setattr(attach, "schedule_deferred_build", lambda build, **kwargs: None)
    real_attach = srv._attach_view

    def attach_then_repoint(sid, **kwargs):
        runner = real_attach(sid, **kwargs)
        # Another request switches the view back to P before create returns.
        real_attach(p, load_transcript_on_create=False)
        return runner

    monkeypatch.setattr(srv, "_attach_view", attach_then_repoint)
    status, _ = post_sessions_create({"title": "New"}, srv._session_services())
    assert status == 200
    assert srv._pilot is p_pilot
    assert [m.get("content") for m in p_pilot.export_history()] == [
        m["content"] for m in P_HISTORY
    ]


def test_failed_placeholder_swap_recovers_session(owned_server, monkeypatch):
    import harness.api.attach as attach
    from harness.api.session_control import get_session_queue
    from harness.conversation import ConversationalSession
    from harness.deferred_attach import is_deferred_placeholder

    srv = owned_server
    sid = srv._sessions.create()["id"]
    callbacks = {}
    monkeypatch.setenv("HARNESS_DEFER_COLD_ATTACH", "1")
    monkeypatch.setattr(attach, "schedule_deferred_build",
                        lambda build, **kwargs: callbacks.update(kwargs))
    placeholder = srv._attach_view(sid, defer_cold_build=True)
    placeholder.load_history({"history": list(P_HISTORY), "display": [], "job_ids": []})
    callbacks["on_error"](RuntimeError("hydrate exploded"))
    assert placeholder.build_error is not None
    assert srv._pilot is placeholder

    status, payload = get_session_queue(sid, srv._session_control_services())
    assert status == 503
    assert "pick_model" in payload["recovery_actions"]

    h = Handler()
    srv.Handler._swap_pilot(h, "stub-oracle", sid)
    assert h.code == 200, h.body
    assert h.body["driver"] == "stub-oracle"

    live = srv._runners.get(sid)
    assert isinstance(live, ConversationalSession)
    assert not is_deferred_placeholder(srv._pilot)
    assert srv._pilot is live
    assert live.config.driver == "stub-oracle"
    assert live.harness_session_id == sid
    assert [m.get("content") for m in live.export_history()] == [
        m["content"] for m in P_HISTORY
    ]
    assert srv._sessions.pilot_preferences(sid)["driver"] == "stub-oracle"
    assert srv._ensure_active_pilot_ready() is live
    status, payload = get_session_queue(sid, srv._session_control_services())
    assert status == 200, payload
    assert payload["state"] == "ready"


def test_abandoned_deferred_build_releases_warm_resources(owned_server, monkeypatch):
    import harness.api.attach as attach

    srv = owned_server
    home = srv._sessions.active
    sid = srv._sessions.create()["id"]
    callbacks = {}
    monkeypatch.setenv("HARNESS_DEFER_COLD_ATTACH", "1")
    monkeypatch.setattr(attach, "schedule_deferred_build",
                        lambda build, **kwargs: callbacks.update(kwargs))
    placeholder = srv._attach_view(sid, defer_cold_build=True)
    srv._attach_view(home, load_transcript_on_create=False)
    srv._runners.drop(sid, notify=False)

    real = srv._build_conversational_pilot(config=srv._runner_config_snapshot())
    released = []
    monkeypatch.setattr(real, "release_warm_acp",
                        lambda **kw: released.append(kw.get("reason")))
    callbacks["on_done"](real)
    assert released == ["session_switch"]
    assert placeholder.is_ready()
    assert srv._runners.get(sid) is None
