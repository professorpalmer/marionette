"""Tests for session delete and archive endpoints."""
import json
import threading
import urllib.request
import urllib.error
import os
from http.server import ThreadingHTTPServer

import pytest
from harness.sessions import save_transcript, load_transcript


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
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=10)


def test_sessions_manage_post_rejected_without_token(tmp_path):
    httpd, port, srv = _server()
    # Override server's state_dir with a temporary one to avoid reading/writing real user data
    srv._cfg.state_dir = str(tmp_path)
    srv._cfg.repo = ""
    srv._sessions.path = str(tmp_path / "harness_sessions.json")
    srv._sessions._sessions = []
    
    try:
        # Delete POST rejected
        try:
            _post(port, "/api/sessions/delete", {"session": "dummy-id"},
                  {"Content-Type": "application/json"})
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # Archive POST rejected
        try:
            _post(port, "/api/sessions/archive", {"session": "dummy-id", "archived": True},
                  {"Content-Type": "application/json"})
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # Settle POST rejected
        try:
            _post(port, "/api/sessions/settle", {"session": "dummy-id", "settled": True},
                  {"Content-Type": "application/json"})
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()


def test_sessions_archive_works_fine(tmp_path):
    httpd, port, srv = _server()
    srv._cfg.state_dir = str(tmp_path)
    srv._cfg.repo = ""
    srv._sessions.path = str(tmp_path / "harness_sessions.json")
    srv._sessions._sessions = []
    
    try:
        # Create a session
        meta = srv._sessions.create("Test Session to Archive")
        sid = meta["id"]
        
        # Verify it lists as not archived by default
        resp = _get(port, "/api/sessions")
        sessions = json.loads(resp.read().decode())
        assert len(sessions) == 1
        assert sessions[0]["id"] == sid
        assert sessions[0]["archived"] is False
        
        # Archive it
        post_resp = _post(port, "/api/sessions/archive", {"session": sid, "archived": True},
                          {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert post_resp.status == 200
        post_data = json.loads(post_resp.read().decode())
        assert post_data["ok"] is True
        
        # Verify lists as archived
        resp = _get(port, "/api/sessions")
        sessions = json.loads(resp.read().decode())
        assert sessions[0]["archived"] is True
        
        # Unarchive it
        post_resp2 = _post(port, "/api/sessions/archive", {"session": sid, "archived": False},
                           {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert post_resp2.status == 200
        
        # Verify lists as unarchived
        resp = _get(port, "/api/sessions")
        sessions = json.loads(resp.read().decode())
        assert sessions[0]["archived"] is False
    finally:
        httpd.shutdown()


def test_sessions_settle_round_trip_and_defaults(tmp_path):
    httpd, port, srv = _server()
    srv._cfg.state_dir = str(tmp_path)
    srv._cfg.repo = ""
    srv._sessions.path = str(tmp_path / "harness_sessions.json")
    srv._sessions._sessions = []

    try:
        meta = srv._sessions.create("Settle me")
        sid = meta["id"]
        assert meta.get("settled") is False

        resp = _get(port, "/api/sessions")
        sessions = json.loads(resp.read().decode())
        assert sessions[0]["settled"] is False
        assert sessions[0]["archived"] is False

        post_resp = _post(
            port, "/api/sessions/settle", {"session": sid, "settled": True},
            {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
        )
        assert post_resp.status == 200
        assert json.loads(post_resp.read().decode())["ok"] is True

        resp = _get(port, "/api/sessions")
        row = json.loads(resp.read().decode())[0]
        assert row["settled"] is True
        assert row["archived"] is False

        post_resp2 = _post(
            port, "/api/sessions/settle", {"id": sid, "settled": False},
            {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
        )
        assert post_resp2.status == 200
        resp = _get(port, "/api/sessions")
        assert json.loads(resp.read().decode())[0]["settled"] is False
    finally:
        httpd.shutdown()


def test_sessions_settle_defaults_false_on_legacy_rows(tmp_path):
    """Legacy JSON without a settled key must list as settled=False (no rewrite)."""
    from harness.sessions import SessionStore

    path = tmp_path / "harness_sessions.json"
    path.write_text(json.dumps({
        "sessions": [{
            "id": "legacy1",
            "title": "Old",
            "created": 1.0,
            "archived": True,
            "repo": str(tmp_path),
            "workspace_root": str(tmp_path),
        }],
        "active": "legacy1",
    }), encoding="utf-8")
    store = SessionStore(str(path))
    rows = store.list()
    assert len(rows) == 1
    assert rows[0]["archived"] is True
    assert rows[0]["settled"] is False
    # Defaults-only: do not persist a migrated settled key until settle() runs.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert "settled" not in on_disk["sessions"][0]


def test_sessions_settle_independent_of_archive(tmp_path):
    httpd, port, srv = _server()
    srv._cfg.state_dir = str(tmp_path)
    srv._cfg.repo = ""
    srv._sessions.path = str(tmp_path / "harness_sessions.json")
    srv._sessions._sessions = []

    try:
        sid = srv._sessions.create("Both flags")["id"]
        headers = {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN}

        _post(port, "/api/sessions/archive", {"session": sid, "archived": True}, headers).read()
        row = json.loads(_get(port, "/api/sessions").read().decode())[0]
        assert row["archived"] is True
        assert row["settled"] is False

        _post(port, "/api/sessions/settle", {"session": sid, "settled": True}, headers).read()
        row = json.loads(_get(port, "/api/sessions").read().decode())[0]
        assert row["archived"] is True
        assert row["settled"] is True

        _post(port, "/api/sessions/archive", {"session": sid, "archived": False}, headers).read()
        row = json.loads(_get(port, "/api/sessions").read().decode())[0]
        assert row["archived"] is False
        assert row["settled"] is True
    finally:
        httpd.shutdown()


def test_sessions_settle_rejects_unknown_and_foreign_workspace(tmp_path):
    httpd, port, srv = _server()
    home = tmp_path / "home"
    other = tmp_path / "other"
    home.mkdir()
    other.mkdir()
    srv._cfg.state_dir = str(tmp_path)
    srv._cfg.repo = str(home)
    srv._sessions.path = str(tmp_path / "harness_sessions.json")
    srv._sessions._sessions = []

    try:
        headers = {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN}
        try:
            _post(port, "/api/sessions/settle", {"session": "missing", "settled": True}, headers)
            assert False, "unknown session should 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404

        foreign = srv._sessions.create("Foreign", repo=str(other), workspace_root=str(other))
        try:
            _post(
                port, "/api/sessions/settle",
                {"session": foreign["id"], "settled": True}, headers,
            )
            assert False, "foreign workspace session should 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # Archive still silently accepts unknown ids (unchanged contract).
        ok = _post(
            port, "/api/sessions/archive",
            {"session": "missing", "archived": True}, headers,
        )
        assert ok.status == 200
    finally:
        httpd.shutdown()


def test_sessions_delete_and_transcript_removal(tmp_path):
    httpd, port, srv = _server()
    srv._cfg.state_dir = str(tmp_path)
    srv._cfg.repo = ""
    srv._sessions.path = str(tmp_path / "harness_sessions.json")
    srv._sessions._sessions = []
    
    try:
        # Create two sessions
        meta1 = srv._sessions.create("Session One")
        meta2 = srv._sessions.create("Session Two")
        
        sid1 = meta1["id"]
        sid2 = meta2["id"]
        
        # Save transcript for first session
        messages = [{"role": "user", "content": "hi session one"}]
        save_transcript(str(tmp_path), sid1, messages)
        
        # Verify transcript file exists
        trans_file = tmp_path / "transcripts" / f"{sid1}.json"
        assert trans_file.exists()
        
        # Delete first session (DELETE endpoint)
        del_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/sessions/{sid1}",
            headers={"X-Harness-Token": srv._TOKEN},
            method="DELETE",
        )
        post_resp = urllib.request.urlopen(del_req, timeout=10)
        assert post_resp.status == 200
        post_data = json.loads(post_resp.read().decode())
        assert post_data["ok"] is True
        # Since we deleted sid1 (active was sid2 because we created meta2 last), active should remain sid2
        assert post_data["active"] == sid2
        
        # Verify transcript file was removed
        assert not trans_file.exists()
        
        # Verify list only contains session 2
        resp = _get(port, "/api/sessions")
        sessions = json.loads(resp.read().decode())
        assert len(sessions) == 1
        assert sessions[0]["id"] == sid2
        
        # Now delete session 2 (which is active)
        del_req2 = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/sessions/{sid2}",
            headers={"X-Harness-Token": srv._TOKEN},
            method="DELETE",
        )
        post_resp2 = urllib.request.urlopen(del_req2, timeout=10)
        assert post_resp2.status == 200
        post_data2 = json.loads(post_resp2.read().decode())
        assert post_data2["ok"] is True
        assert post_data2["active"] is None
        
        # Verify list is empty
        resp = _get(port, "/api/sessions")
        sessions = json.loads(resp.read().decode())
        assert len(sessions) == 0
    finally:
        httpd.shutdown()
