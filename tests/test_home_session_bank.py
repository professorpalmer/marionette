"""Home workspace, session relocate, and cross-workspace session bank."""
import json
import os

import harness.server as srv
from harness.sessions import SessionStore, save_transcript


def test_ensure_home_workspace_creates_and_seeds(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    # Re-bind helpers that close over env at call time.
    home = srv._ensure_home_workspace()
    assert os.path.isdir(home)
    assert home == os.path.join(str(tmp_path), "home")
    assert os.path.isfile(os.path.join(home, "AGENTS.md"))
    # as_active=False must not clobber a prior boot repo.
    ws = os.path.join(str(tmp_path), "workspace.json")
    if os.path.isfile(ws):
        data = json.loads(open(ws, encoding="utf-8").read())
        # Home may be in recents but need not be the active repo key when
        # nothing else was set -- either empty or home is acceptable on first
        # ensure; subsequent ensure with prior must keep prior.
        assert "recents" in data


def test_home_record_does_not_steal_active_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    project = tmp_path / "myproj"
    project.mkdir()
    ws_path = srv._workspace_json_path()
    os.makedirs(os.path.dirname(ws_path), exist_ok=True)
    with open(ws_path, "w", encoding="utf-8") as f:
        json.dump({"repo": str(project), "recents": [str(project)]}, f)

    home = srv._ensure_home_workspace()
    data = json.loads(open(ws_path, encoding="utf-8").read())
    assert os.path.normcase(os.path.realpath(data["repo"])) == os.path.normcase(
        os.path.realpath(str(project))
    )
    assert any(
        os.path.normcase(os.path.realpath(r)) == os.path.normcase(os.path.realpath(home))
        for r in data.get("recents") or []
    )


def test_migrate_empty_roots_to_home(tmp_path):
    store = SessionStore(str(tmp_path / "harness_sessions.json"))
    orphan = store.create("Orphan", repo="", workspace_root="")
    rooted = store.create("Rooted", repo=str(tmp_path / "p"), workspace_root=str(tmp_path / "p"))
    home = str(tmp_path / "home")
    os.makedirs(home, exist_ok=True)
    moved = store.migrate_empty_roots(home)
    assert orphan["id"] in moved
    assert rooted["id"] not in moved
    rows = {r["id"]: r for r in store.rows()}
    assert rows[orphan["id"]]["workspace_root"] == home
    assert rows[rooted["id"]]["workspace_root"] == str(tmp_path / "p")


def test_relocate_preserves_session_id_and_transcript(tmp_path):
    store = SessionStore(str(tmp_path / "harness_sessions.json"))
    state = str(tmp_path / "state")
    os.makedirs(state, exist_ok=True)
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    meta = store.create("Chat", repo=str(home), workspace_root=str(home))
    sid = meta["id"]
    save_transcript(state, sid, {"history": [{"role": "user", "content": "hi"}], "display": []})

    relocated = store.relocate(sid, str(proj), repo=str(proj), title="Moved", make_active=True)
    assert relocated is not None
    assert relocated["id"] == sid
    assert relocated["workspace_root"] == str(proj)
    assert relocated["title"] == "Moved"
    assert store.active == sid
    # Transcript file unchanged
    from harness.sessions import load_transcript
    data = load_transcript(state, sid)
    assert data["history"][0]["content"] == "hi"


def test_list_bank_chrono_and_query(tmp_path):
    store = SessionStore(str(tmp_path / "harness_sessions.json"))
    a = store.create("Alpha", repo="/a", workspace_root="/a")
    b = store.create("Beta searchme", repo="/b", workspace_root="/b")
    # Force created ordering on the live store rows (rows() returns copies).
    with store._lock:
        for s in store._sessions:
            if s["id"] == a["id"]:
                s["created"] = 100.0
            if s["id"] == b["id"]:
                s["created"] = 200.0
        store._save()

    bank = store.list_bank(limit=10)
    assert bank[0]["id"] == b["id"]
    filtered = store.list_bank(query="searchme")
    assert len(filtered) == 1
    assert filtered[0]["id"] == b["id"]


def test_handle_session_relocate_api(monkeypatch, tmp_path):
    proj = tmp_path / "target"
    proj.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    # Isolate session store
    store = SessionStore(str(tmp_path / "harness_sessions.json"))
    meta = store.create("Stay", repo=str(home), workspace_root=str(home))
    monkeypatch.setattr(srv, "_sessions", store)
    monkeypatch.setattr(srv, "_save_active_transcript", lambda: None)
    monkeypatch.setattr(srv, "_attach_view", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_record_recent_workspace", lambda *a, **k: [str(proj)])
    monkeypatch.setattr(srv, "_note_boot_repo", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_index_codegraph_bg", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_maybe_refresh_codegraph", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: True)
    monkeypatch.setattr(srv, "_get_codegraph_status", lambda *a, **k: "indexing")
    monkeypatch.setattr(srv, "_is_app_install_root", lambda *a, **k: False)
    srv._cfg.repo = str(home)

    status, payload = srv._handle_session_relocate({
        "session_id": meta["id"],
        "workspace_root": str(proj),
        "title": "In project",
    })
    assert status == 200
    assert payload["ok"] is True
    assert payload["active"] == meta["id"]
    assert payload["repo"] == str(proj)
    row = next(r for r in store.rows() if r["id"] == meta["id"])
    assert row["workspace_root"] == str(proj)
    assert row["title"] == "In project"
    assert srv._cfg.repo == str(proj)
