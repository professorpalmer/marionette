"""Tests for session export GET endpoint."""
import json
import threading
import urllib.request
import urllib.error
import datetime
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
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {}, method="GET")
    return urllib.request.urlopen(req, timeout=10)


def test_export_json_and_md(tmp_path):
    httpd, port, srv = _server()
    # Override server's state_dir with a temporary one to avoid reading/writing real user data
    srv._cfg.state_dir = str(tmp_path)
    # Reinitialize or populate sessions store list
    srv._sessions.path = str(tmp_path / "harness_sessions.json")
    srv._sessions._sessions = []
    
    try:
        # 1. Create a session in our server sessions store
        sess_meta = srv._sessions.create("My Export Test Session!")
        sid = sess_meta["id"]
        
        # 2. Save a transcript for this session id
        messages = [
            {"role": "user", "content": "hello pilot"},
            {"role": "assistant", "content": "hello human"}
        ]
        save_transcript(str(tmp_path), sid, messages)
        
        # 3. Request JSON export
        url_json = f"/api/sessions/export?session={sid}&format=json"
        resp_json = _get(port, url_json, headers={"X-Harness-Token": srv._TOKEN})
        assert resp_json.status == 200
        
        # Check Headers
        content_type_json = resp_json.headers.get("Content-Type")
        assert "application/json" in content_type_json
        
        content_disp_json = resp_json.headers.get("Content-Disposition")
        assert content_disp_json == 'attachment; filename="My_Export_Test_Session.json"'
        
        # Check Body JSON
        data_json = json.loads(resp_json.read().decode("utf-8"))
        assert data_json["session_id"] == sid
        assert data_json["title"] == "My Export Test Session!"
        assert data_json["created"] == sess_meta["created"]
        assert "exported_at" in data_json
        assert data_json["messages"] == messages
        
        # 4. Request MD export
        url_md = f"/api/sessions/export?session={sid}&format=md"
        resp_md = _get(port, url_md, headers={"X-Harness-Token": srv._TOKEN})
        assert resp_md.status == 200
        
        # Check Headers
        content_type_md = resp_md.headers.get("Content-Type")
        assert "text/markdown" in content_type_md
        
        content_disp_md = resp_md.headers.get("Content-Disposition")
        assert content_disp_md == 'attachment; filename="My_Export_Test_Session.md"'
        
        # Check Body MD
        body_md = resp_md.read().decode("utf-8")
        assert "# My Export Test Session!" in body_md
        assert f"**Session ID:** {sid}" in body_md
        assert "## User" in body_md
        assert "hello pilot" in body_md
        assert "## Assistant" in body_md
        assert "hello human" in body_md

    finally:
        httpd.shutdown()


def test_export_unknown_session_does_not_500(tmp_path):
    httpd, port, srv = _server()
    srv._cfg.state_dir = str(tmp_path)
    srv._sessions.path = str(tmp_path / "harness_sessions.json")
    srv._sessions._sessions = []
    
    try:
        url_json = f"/api/sessions/export?session=unknown-id&format=json"
        resp = _get(port, url_json, headers={"X-Harness-Token": srv._TOKEN})
        assert resp.status == 200
        
        content_disp = resp.headers.get("Content-Disposition")
        assert content_disp == 'attachment; filename="unknown-id.json"'
        
        data = json.loads(resp.read().decode("utf-8"))
        assert data["session_id"] == "unknown-id"
        assert data["title"] == "Unknown Session"
        assert data["messages"] == []
        
    finally:
        httpd.shutdown()


def test_export_requires_header_auth_not_query_string(tmp_path):
    """Verify exports authenticate via X-Harness-Token header, not query string.
    
    Query-string tokens are no longer accepted (removed for security hardening).
    Only header-based auth works.
    """
    httpd, port, srv = _server()
    srv._cfg.state_dir = str(tmp_path)
    srv._sessions.path = str(tmp_path / "harness_sessions.json")
    srv._sessions._sessions = []
    
    try:
        # Create a session
        sess_meta = srv._sessions.create("Test Export Auth!")
        sid = sess_meta["id"]
        
        # Save a transcript for this session id
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"}
        ]
        save_transcript(str(tmp_path), sid, messages)
        
        # 1. Query-string token should be rejected (even with token value)
        url = f"/api/sessions/export?session={sid}&format=json&token={srv._TOKEN}"
        req = urllib.request.Request(f"http://127.0.0.1:{port}{url}", method="GET")
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "expected HTTP 403 with query-string token (not supported)"
        except urllib.error.HTTPError as e:
            assert e.code == 403
        
        # 2. Header-based token should work
        url = f"/api/sessions/export?session={sid}&format=json"
        resp = _get(port, url, headers={"X-Harness-Token": srv._TOKEN})
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["session_id"] == sid
        
    finally:
        httpd.shutdown()
