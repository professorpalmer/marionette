"""Tests for session naming and workspace grouping."""
import json
import threading
import urllib.request
import urllib.error
import urllib.parse
import os
from http.server import ThreadingHTTPServer

import pytest
from harness.sessions import derive_title, SessionStore, SessionMeta


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


def _post(port, path, body, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=10)


def test_derive_title():
    # Basic truncation & capitalization
    assert derive_title("fix the resize bug in the tab bar please") == "Fix the resize bug in the tab bar"
    # Trailing punctuation stripped
    assert derive_title("Why does /api/usage return 0?") == "Why does /api/usage return 0"
    # Strips code fences and markdown
    assert derive_title("```python\ndef foo():\n    return True\n```") == "Def foo()"
    # Handles bullet items
    assert derive_title("- item 1\n- item 2") == "Item 1"
    # Handles empty/whitespace
    assert derive_title("   \n   \n") == "New session"
    # Collapses whitespace
    assert derive_title("   hello    world   ") == "Hello world"


def test_set_title_if_default(tmp_path):
    store_path = tmp_path / "sessions.json"
    store = SessionStore(str(store_path))
    
    # Create with default title
    sess = store.create()
    sid = sess["id"]
    assert sess["title"] == "New session"
    
    # Should update if currently default
    store.set_title_if_default(sid, "Sane title")
    listed = store.list()
    assert listed[0]["title"] == "Sane title"
    
    # Should NOT update if already set
    store.set_title_if_default(sid, "Another title")
    listed = store.list()
    assert listed[0]["title"] == "Sane title"


def test_sessions_rename_endpoint(tmp_path):
    httpd, port, srv = _server()
    srv._cfg.state_dir = str(tmp_path)
    srv._sessions.path = str(tmp_path / "harness_sessions.json")
    srv._sessions._sessions = []
    
    try:
        sess = srv._sessions.create("Original Title")
        sid = sess["id"]
        
        # Test 403 without token
        try:
            _post(port, "/api/sessions/rename", {"session": sid, "title": "New Title"},
                  {"Content-Type": "application/json"})
            assert False, "Should fail without token"
        except urllib.error.HTTPError as e:
            assert e.code == 403
            
        # Test successful rename with token
        resp = _post(port, "/api/sessions/rename", {"session": sid, "title": "Renamed Title"},
                     {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["ok"] is True
        
        # Verify rename persisted
        get_resp = _get(port, "/api/sessions")
        sessions = json.loads(get_resp.read().decode())
        assert sessions[0]["title"] == "Renamed Title"
    finally:
        httpd.shutdown()


def test_first_message_auto_titles(tmp_path):
    httpd, port, srv = _server()
    srv._cfg.state_dir = str(tmp_path)
    srv._sessions.path = str(tmp_path / "harness_sessions.json")
    srv._sessions._sessions = []
    
    try:
        # Create default session
        sess = srv._sessions.create()
        sid = sess["id"]
        assert sess["title"] == "New session"
        
        # Mock actual streaming chat call
        # It should trigger set_title_if_default
        try:
            _get(port, f"/api/chat?message={urllib.parse.quote('why does /api/usage return 0?')}&token={srv._TOKEN}")
        except Exception:
            # We don't care if the actual stream fails (e.g. key/preflight issues)
            # because the auto-titling logic runs first
            pass
            
        # Check if the title was updated
        get_resp = _get(port, "/api/sessions")
        sessions = json.loads(get_resp.read().decode())
        assert sessions[0]["title"] == "Why does /api/usage return 0"
    finally:
        httpd.shutdown()


def test_sessions_carry_repo_branch(tmp_path):
    store_path = tmp_path / "sessions.json"
    store = SessionStore(str(store_path))
    
    # Create with repo & branch
    sess = store.create(repo="/path/to/repo", branch="main")
    sid = sess["id"]
    
    assert sess["repo"] == "/path/to/repo"
    assert sess["branch"] == "main"
    
    # Test list() preserves them
    listed = store.list()
    assert listed[0]["repo"] == "/path/to/repo"
    assert listed[0]["branch"] == "main"
