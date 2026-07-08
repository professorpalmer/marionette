"""Workspace-scoped session listing, legacy inference, delete, and clear."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from harness.sessions import (
    SessionStore,
    infer_legacy_session_root,
    save_transcript,
    session_visible_for_workspace,
)


def _server():
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _get(port, path, headers=None):
    h = dict(headers or {})
    if "X-Harness-Token" not in h:
        import harness.server as _srv
        h["X-Harness-Token"] = _srv._TOKEN
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=h, method="GET")
    return urllib.request.urlopen(req, timeout=10)


def _post(port, path, body, headers):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=10)


def _delete(port, path, headers):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers=headers,
        method="DELETE",
    )
    return urllib.request.urlopen(req, timeout=10)


def _setup_server(tmp_path, srv):
    srv._cfg.state_dir = str(tmp_path)
    srv._sessions.path = str(tmp_path / "harness_sessions.json")
    srv._sessions._sessions = []
    srv._sessions._active = None


def test_sessions_filtered_by_workspace_two_roots(tmp_path):
    httpd, port, srv = _server()
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()
    _setup_server(tmp_path, srv)

    try:
        meta_a = srv._sessions.create("A", repo=str(repo_a), workspace_root=str(repo_a))
        meta_b = srv._sessions.create("B", repo=str(repo_b), workspace_root=str(repo_b))

        srv._cfg.repo = str(repo_a)
        resp_a = _get(port, "/api/sessions")
        sessions_a = json.loads(resp_a.read().decode())
        assert {s["id"] for s in sessions_a} == {meta_a["id"]}

        srv._cfg.repo = str(repo_b)
        resp_b = _get(port, "/api/sessions")
        sessions_b = json.loads(resp_b.read().decode())
        assert {s["id"] for s in sessions_b} == {meta_b["id"]}
    finally:
        httpd.shutdown()


def test_legacy_session_without_stored_root_uses_transcript_cwd(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()

    legacy = {"id": "legacy1", "title": "Legacy", "created": 1.0}
    store._sessions.append(legacy)
    save_transcript(
        str(tmp_path),
        "legacy1",
        {
            "history": [],
            "display": [{"type": "card", "id": "a1", "cwd": str(repo_a)}],
        },
    )

    assert session_visible_for_workspace(legacy, str(repo_a), str(tmp_path))
    assert not session_visible_for_workspace(legacy, str(repo_b), str(tmp_path))
    assert infer_legacy_session_root(legacy, str(tmp_path)) == str(repo_a)


def test_legacy_session_without_root_or_cwd_visible_everywhere(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()

    orphan = {"id": "orphan1", "title": "Orphan", "created": 1.0}
    store._sessions.append(orphan)

    assert session_visible_for_workspace(orphan, str(repo_a), str(tmp_path))
    assert session_visible_for_workspace(orphan, str(repo_b), str(tmp_path))


def test_delete_single_session_via_delete_method(tmp_path):
    httpd, port, srv = _server()
    _setup_server(tmp_path, srv)
    srv._cfg.repo = ""

    try:
        meta1 = srv._sessions.create("One")
        meta2 = srv._sessions.create("Two")
        sid1 = meta1["id"]

        resp = _delete(
            port,
            f"/api/sessions/{sid1}",
            {"X-Harness-Token": srv._TOKEN},
        )
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["ok"] is True
        assert data["active"] == meta2["id"]

        listed = json.loads(_get(port, "/api/sessions").read().decode())
        assert len(listed) == 1
        assert listed[0]["id"] == meta2["id"]
    finally:
        httpd.shutdown()


def test_delete_active_session_stays_in_same_workspace(tmp_path):
    """Deleting the active session must promote a sibling from the SAME
    workspace -- never the newest session globally, which auto-switched the
    whole app to another dir (often a leaked temp worktree)."""
    store = SessionStore(str(tmp_path / "sessions.json"))
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()

    older_a = store.create("A-old", repo=str(repo_a), workspace_root=str(repo_a))
    newest_b = store.create("B-new", repo=str(repo_b), workspace_root=str(repo_b))
    active_a = store.create("A-active", repo=str(repo_a), workspace_root=str(repo_a))

    new_active = store.delete(active_a["id"])
    assert new_active == older_a["id"]
    assert new_active != newest_b["id"]


def test_delete_last_session_in_workspace_leaves_no_active(tmp_path):
    """No same-workspace sibling left: active becomes None instead of yanking
    the workspace to wherever the newest global session lives."""
    store = SessionStore(str(tmp_path / "sessions.json"))
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()

    store.create("B", repo=str(repo_b), workspace_root=str(repo_b))
    only_a = store.create("A", repo=str(repo_a), workspace_root=str(repo_a))

    assert store.delete(only_a["id"]) is None


def test_delete_rootless_active_promotes_rootless_peer(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    peer = store.create("One")
    active = store.create("Two")
    assert store.delete(active["id"]) == peer["id"]


def test_ephemeral_temp_sessions_pruned_on_load(tmp_path, monkeypatch):
    """Session rows rooted in the OS temp dir (worker worktrees, test repos
    that leaked into live state) are dropped when the store loads."""
    import harness.sessions as sessions_mod

    fake_tmp = tmp_path / "faketmp"
    temp_repo = fake_tmp / "tmpabc123"
    temp_repo.mkdir(parents=True)
    real_repo = tmp_path / "repo_a"
    real_repo.mkdir()

    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({
        "sessions": [
            {"id": "real1", "title": "Real", "created": 1.0,
             "workspace_root": str(real_repo)},
            {"id": "temp1", "title": "tmpabc123", "created": 2.0,
             "workspace_root": str(temp_repo)},
        ],
        "active": "real1",
    }))

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sessions_mod.tempfile, "gettempdir", lambda: str(fake_tmp))
    store = SessionStore(str(path))

    ids = {s["id"] for s in store._sessions}
    assert ids == {"real1"}


def test_record_recent_workspace_keeps_prior_repo_on_temp_target(tmp_path, monkeypatch):
    """The persisted "repo" key is the boot-restore workspace; a temp-dir open
    must not overwrite it (that resurrected phantom temp dirs on relaunch)."""
    import harness.server as srv

    fake_tmp = tmp_path / "faketmp"
    temp_repo = fake_tmp / "tmpxyz"
    temp_repo.mkdir(parents=True)
    real_repo = tmp_path / "repo_a"
    real_repo.mkdir()

    monkeypatch.setattr(srv, "_workspace_json_path", lambda: str(tmp_path / "workspace.json"))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    import tempfile as _tempfile
    monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(fake_tmp))

    srv._record_recent_workspace(str(real_repo))
    srv._record_recent_workspace(str(temp_repo))

    data = json.loads((tmp_path / "workspace.json").read_text())
    assert data["repo"] == str(real_repo)
    assert str(temp_repo) not in data["recents"]
    assert str(real_repo) in data["recents"]


def test_clear_sessions_only_current_workspace(tmp_path):
    httpd, port, srv = _server()
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()
    _setup_server(tmp_path, srv)

    try:
        meta_a1 = srv._sessions.create("A1", repo=str(repo_a), workspace_root=str(repo_a))
        meta_a2 = srv._sessions.create("A2", repo=str(repo_a), workspace_root=str(repo_a))
        meta_b = srv._sessions.create("B1", repo=str(repo_b), workspace_root=str(repo_b))

        srv._cfg.repo = str(repo_a)
        resp = _post(
            port,
            "/api/sessions/clear",
            {},
            {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
        )
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["ok"] is True
        assert data["deleted"] == 2

        remaining = srv._sessions._sessions
        assert len(remaining) == 1
        assert remaining[0]["id"] == meta_b["id"]

        srv._cfg.repo = str(repo_b)
        listed_b = json.loads(_get(port, "/api/sessions").read().decode())
        assert len(listed_b) == 1
        assert listed_b[0]["id"] == meta_b["id"]

        srv._cfg.repo = str(repo_a)
        listed_a = json.loads(_get(port, "/api/sessions").read().decode())
        assert listed_a == []
    finally:
        httpd.shutdown()
