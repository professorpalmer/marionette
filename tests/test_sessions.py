"""Rematerialize from the existing transcript + fork-at-event-id (pointer, not DAG)."""
from __future__ import annotations

from types import SimpleNamespace

from harness.api.sessions import SessionServices, post_session_fork, post_sessions_rename
from harness.sessions import (
    SessionStore,
    load_transcript,
    rematerialize_driver_messages,
    save_transcript,
)


def _five_turn_history():
    return [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
        {"role": "user", "content": "five"},
    ]


def test_rematerialize_five_turn_history_is_identity():
    history = _five_turn_history()
    assert rematerialize_driver_messages(history) == history
    assert rematerialize_driver_messages({"history": history}) == history


def test_rematerialize_drops_system_keeps_tool_pairs():
    history = [
        {"role": "system", "content": "you are a pilot"},
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
    ]
    assert rematerialize_driver_messages({"history": history}) == history[1:]


def test_fork_at_prefix_cutoff_and_parent_unchanged(tmp_path):
    store = SessionStore(str(tmp_path / "harness_sessions.json"))
    parent = store.create(title="Parent", repo=str(tmp_path), workspace_root=str(tmp_path))
    store.pilot_preferences(parent["id"], updates={"driver": "stub-oracle", "reasoning_effort": "high"})
    history = _five_turn_history()
    save_transcript(str(tmp_path), parent["id"], {"history": history})

    child = store.fork_at(parent["id"], 3, str(tmp_path))
    assert child is not None
    assert child["id"] != parent["id"]
    assert store.active == parent["id"]
    assert child.get("active") is False
    assert child["forked_from"] == {"parent_id": parent["id"], "at_event_id": 3}
    assert store.pilot_preferences(child["id"]) == store.pilot_preferences(parent["id"])
    store.pilot_preferences(child["id"], updates={"driver": "stub-oracle-v2"})
    assert store.pilot_preferences(parent["id"])["driver"] == "stub-oracle"

    parent_rows = [r for r in store.rows() if r["id"] == parent["id"]]
    assert parent_rows[0]["forked_to"] == {"child_id": child["id"], "at_event_id": 3}

    parent_msgs = rematerialize_driver_messages(load_transcript(str(tmp_path), parent["id"]))
    child_msgs = rematerialize_driver_messages(load_transcript(str(tmp_path), child["id"]))
    assert parent_msgs == history
    assert child_msgs == history[:3]
    assert child_msgs == parent_msgs[:3]
    assert child_msgs[3:] == []
    assert len(child_msgs) == 3


def test_fork_at_unknown_parent_is_none(tmp_path):
    store = SessionStore(str(tmp_path / "harness_sessions.json"))
    assert store.fork_at("missing", 1, str(tmp_path)) is None


def test_fork_at_bad_event_id_raises(tmp_path):
    store = SessionStore(str(tmp_path / "harness_sessions.json"))
    parent = store.create(title="P", repo=str(tmp_path), workspace_root=str(tmp_path))
    save_transcript(str(tmp_path), parent["id"], {"history": _five_turn_history()})
    try:
        store.fork_at(parent["id"], 0, str(tmp_path))
        assert False, "expected bad event_id"
    except ValueError:
        pass
    try:
        store.fork_at(parent["id"], 9, str(tmp_path))
        assert False, "expected bad event_id"
    except ValueError:
        pass


def _session_svc(store: SessionStore, state_dir: str) -> SessionServices:
    return SessionServices(
        sessions=store,
        runners=SimpleNamespace(get=lambda _sid: None),
        cfg=SimpleNamespace(state_dir=state_dir, repo=state_dir),
        get_pilot=lambda: SimpleNamespace(load_history=lambda _h: None),
        sessions_state_dir=lambda: state_dir,
        save_active_transcript=lambda: None,
        attach_view=lambda *_a, **_k: None,
        sync_pilot_session_id=lambda: None,
        clear_active_pilot=lambda: None,
        diag=lambda *_a, **_k: None,
        is_app_install_root=lambda _p: False,
        ensure_home_workspace=lambda: state_dir,
        prepare_home_workspace=lambda: state_dir,
        home_workspace_path=lambda: state_dir,
        note_boot_repo=lambda _r: None,
        record_recent_workspace=lambda *_a, **_k: None,
        puppetmaster_available=lambda: False,
        index_codegraph_bg=lambda _r: None,
        maybe_refresh_codegraph=lambda _r: None,
        get_codegraph_status=lambda _r: "none",
        lease_exhausted_body=lambda _e: {},
        attach_view_transcript_payload=lambda _p, _s: {},
        parse_bool=lambda v: bool(v),
        set_codegraph_status=lambda *_a, **_k: None,
    )


def test_post_sessions_rename_missing_is_404(tmp_path):
    store = SessionStore(str(tmp_path / "harness_sessions.json"))
    svc = _session_svc(store, str(tmp_path))
    code, payload = post_sessions_rename(
        {"session": "missing", "title": "New name"}, svc
    )
    assert code == 404
    assert payload.get("ok") is not True


def test_post_sessions_rename_activity_headline_is_400(tmp_path):
    store = SessionStore(str(tmp_path / "harness_sessions.json"))
    row = store.create(title="Keep me", repo=str(tmp_path), workspace_root=str(tmp_path))
    svc = _session_svc(store, str(tmp_path))
    code, payload = post_sessions_rename(
        {"session": row["id"], "title": "Investigating"}, svc
    )
    assert code == 400
    assert "invalid" in str(payload.get("error", "")).lower()
    kept = next(s for s in store.rows() if s["id"] == row["id"])
    assert kept["title"] == "Keep me"


def test_post_session_fork_peel_404_and_400(tmp_path):
    store = SessionStore(str(tmp_path / "harness_sessions.json"))
    parent = store.create(title="P", repo=str(tmp_path), workspace_root=str(tmp_path))
    save_transcript(str(tmp_path), parent["id"], {"history": _five_turn_history()})
    svc = _session_svc(store, str(tmp_path))

    code, payload = post_session_fork({"session_id": "missing", "event_id": 1}, svc)
    assert code == 404
    assert payload["ok"] is False

    code, payload = post_session_fork({"session_id": parent["id"], "event_id": 0}, svc)
    assert code == 400
    assert payload["error"] == "bad event_id"

    code, payload = post_session_fork({"session_id": parent["id"], "event_id": 2}, svc)
    assert code == 200
    assert payload["ok"] is True
    child_id = payload["id"]
    child_msgs = rematerialize_driver_messages(load_transcript(str(tmp_path), child_id))
    assert child_msgs == _five_turn_history()[:2]
    assert child_msgs[2:] == []


def test_fork_preview_revision_retry_and_isolation(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    parent = store.create("Parent", repo=str(tmp_path))
    raw = {"history": _five_turn_history(), "job_ids": ["owned"],
           "goal": {"text": "parent goal"}, "display": [{"kind": "review"}],
           "permissions": {"allow_all": True}, "tokens_used": 500}
    save_transcript(str(tmp_path), parent["id"], raw)
    svc = _session_svc(store, str(tmp_path))
    code, preview = post_session_fork({"session_id": parent["id"], "preview": True}, svc)
    assert code == 200
    assert preview["boundaries"]
    request = {"session_id": parent["id"], "event_id": 4,
               "revision": preview["revision"], "request_id": "retry-one"}
    code, child = post_session_fork(request, svc)
    assert code == 200
    assert post_session_fork(request, svc)[1]["id"] == child["id"]
    assert len(store.rows()) == 2
    assert load_transcript(str(tmp_path), child["id"]) == {"history": raw["history"][:4]}
    assert load_transcript(str(tmp_path), parent["id"]) == raw
    assert store.active == parent["id"]
    assert child["forked_from"]["revision"] == preview["revision"]
    save_transcript(str(tmp_path), parent["id"], {"history": raw["history"][:2]})
    request["request_id"] = "another"
    assert post_session_fork(request, svc)[0] == 409
    assert len(store.rows()) == 2


def test_fork_rejects_incomplete_tool_batch_and_noninteger(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    parent = store.create("P")
    history = [{"role": "user", "content": "go"},
               {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
               {"role": "tool", "tool_call_id": "a", "content": "a"},
               {"role": "tool", "tool_call_id": "b", "content": "b"},
               {"role": "assistant", "content": "done"}]
    save_transcript(str(tmp_path), parent["id"], {"history": history})
    svc = _session_svc(store, str(tmp_path))
    for boundary in [2, 3, True, 1.5]:
        assert post_session_fork({"session_id": parent["id"], "event_id": boundary}, svc)[0] == 400
    assert post_session_fork({"session_id": parent["id"], "event_id": 5}, svc)[0] == 200


def test_fork_runner_loads_real_history_without_parent_runtime(tmp_path):
    import json
    from harness.api.attach import _fork_runner_config, attach_view
    from harness.session_runners import SessionRunnerRegistry
    import threading
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    store = SessionStore(str(tmp_path / "sessions.json"))
    parent = store.create("P")
    save_transcript(str(tmp_path), parent["id"], {"history": _five_turn_history(),
        "display": [{"type": "command_approval", "session_id": parent["id"]}], "job_ids": ["parent-job"]})
    (tmp_path / "prompt_queue.json").write_text(json.dumps({"queue": [{"id": "parent-prompt", "text": "do parent work"}]}))
    child = store.fork_at(parent["id"], 4, str(tmp_path))
    svc = SimpleNamespace(sessions=store, sessions_state_dir=lambda: str(tmp_path))
    cfg = _fork_runner_config(child["id"], svc, HarnessConfig(state_dir=str(tmp_path)))
    current = [None]
    def sync():
        current[0].harness_session_id = store.active
        current[0].reload_session_goal()
    svc.cfg = HarnessConfig(state_dir=str(tmp_path))
    svc.runners = SessionRunnerRegistry()
    svc.pilot_swap_lock = threading.RLock()
    svc.get_pilot = lambda: current[0]
    svc.set_pilot = lambda pilot: current.__setitem__(0, pilot)
    svc.get_session = lambda: SimpleNamespace()
    svc.bind_pilot_services = lambda pilot: None
    svc.sync_pilot_session_id = sync
    svc.runner_config_snapshot = lambda: HarnessConfig(state_dir=str(tmp_path))
    svc.build_conversational_pilot = lambda config: ConversationalSession(config)
    svc.diag = lambda *args: None
    store.switch(child["id"])
    runner = attach_view(child["id"], svc, defer_cold_build=False)
    assert runner.export_history() == _five_turn_history()[:4]
    assert runner.list_prompts() == []
    assert runner._session_job_ids == []
    assert runner._display_transcript == [
        {"type": "message", "role": m["role"], "text": m["content"]}
        for m in _five_turn_history()[:4]
    ]
    assert not runner._session_goal.text
    assert runner._local_jobs == {}
    assert cfg.state_dir != str(tmp_path)
    assert json.loads((tmp_path / "prompt_queue.json").read_text())["queue"][0]["id"] == "parent-prompt"
    queued = runner.enqueue_prompt("child work")
    svc.runners = SessionRunnerRegistry()
    restarted = attach_view(child["id"], svc, defer_cold_build=False)
    assert restarted.list_prompts() == []
    assert restarted.held_prompts()[0]["id"] == queued["id"]
    assert restarted.input_receipts()[0]["original_text"] == "child work"
    assert restarted.input_receipts()[0]["held"] is True
    sibling = store.fork_at(parent["id"], 2, str(tmp_path))
    store.switch(sibling["id"])
    other = attach_view(sibling["id"], svc, defer_cold_build=False)
    assert other.list_prompts() == []


def test_fork_retry_survives_restart_and_keeps_siblings(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    parent = store.create("P")
    save_transcript(str(tmp_path), parent["id"], {"history": _five_turn_history()})
    revision = store.fork_preview(parent["id"], str(tmp_path))["revision"]
    first = store.fork_at(parent["id"], 2, str(tmp_path), revision=revision, request_id="one")
    second = store.fork_at(parent["id"], 4, str(tmp_path), revision=revision, request_id="two")
    reloaded = SessionStore(str(tmp_path / "sessions.json"))
    assert reloaded.fork_at(parent["id"], 2, str(tmp_path), revision=revision, request_id="one")["id"] == first["id"]
    assert {r["id"] for r in reloaded.rows() if r.get("forked_from")} == {first["id"], second["id"]}


def test_fork_persistence_failure_does_not_publish_child(tmp_path, monkeypatch):
    import harness.sessions as module
    store = SessionStore(str(tmp_path / "sessions.json"))
    parent = store.create("P")
    save_transcript(str(tmp_path), parent["id"], {"history": _five_turn_history()})
    monkeypatch.setattr(module, "save_transcript", lambda *args: None)
    code, _ = post_session_fork({"session_id": parent["id"], "event_id": 2}, _session_svc(store, str(tmp_path)))
    assert code == 500
    assert len(store.rows()) == 1
    assert "forked_to" not in store.rows()[0]
