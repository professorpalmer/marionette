"""Rematerialize from the existing transcript + fork-at-event-id (pointer, not DAG)."""
from __future__ import annotations

from types import SimpleNamespace

from harness.api.sessions import SessionServices, post_session_fork
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
    history = _five_turn_history()
    save_transcript(str(tmp_path), parent["id"], {"history": history})

    child = store.fork_at(parent["id"], 3, str(tmp_path))
    assert child is not None
    assert child["id"] != parent["id"]
    assert store.active == parent["id"]
    assert child.get("active") is False
    assert child["forked_from"] == {"parent_id": parent["id"], "at_event_id": 3}

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
