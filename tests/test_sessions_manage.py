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
    finally:
        httpd.shutdown()


def test_sessions_archive_works_fine(tmp_path):
    httpd, port, srv = _server()
    srv._cfg.state_dir = str(tmp_path)
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


def test_sessions_delete_and_transcript_removal(tmp_path):
    httpd, port, srv = _server()
    srv._cfg.state_dir = str(tmp_path)
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
        
        # Delete first session
        post_resp = _post(port, "/api/sessions/delete", {"session": sid1},
                          {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
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
        post_resp2 = _post(port, "/api/sessions/delete", {"session": sid2},
                           {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
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
