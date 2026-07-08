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
