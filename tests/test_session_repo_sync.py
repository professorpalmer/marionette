"""Tests for session switch repo sync and codegraph staleness check (no network)."""
import json
import threading
import urllib.request
import urllib.error
import os
import time
from http.server import ThreadingHTTPServer

import pytest
from harness.sessions import save_transcript, load_transcript
from harness.server import _codegraph_is_stale, _maybe_refresh_codegraph


def _server():
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _post(port, path, body, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=10)


def test_session_switch_repo_sync(tmp_path, monkeypatch):
    # Mock puppetmaster availability and bg indexers to record calls
    bg_index_calls = []
    bg_reindex_calls = []

    import harness.server as srv
    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: True)
    monkeypatch.setattr(srv, "_index_codegraph_bg", lambda repo: bg_index_calls.append(repo))
    monkeypatch.setattr(srv, "_reindex_codegraph_bg", lambda repo: bg_reindex_calls.append(repo))

    httpd, port, srv = _server()
    # Override server's state_dir with a temporary one to avoid reading/writing real user data
    srv._cfg.state_dir = str(tmp_path)
    srv._sessions.path = str(tmp_path / "harness_sessions.json")
    srv._sessions._sessions = []
    srv._sessions._active = None

    try:
        # Create temporary project directories A and B
        repo_a = tmp_path / "repo_a"
        repo_a.mkdir()
        repo_b = tmp_path / "repo_b"
        repo_b.mkdir()

        # Step 1: Open project A, which will create session_a bound to repo_a
        resp1 = _post(port, "/api/workspace/open", {"path": str(repo_a)},
                      {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert resp1.status == 200
        data1 = json.loads(resp1.read().decode())
        assert data1["ok"] is True
        
        session_a_id = srv._sessions.active
        assert session_a_id is not None
        assert srv._cfg.repo == str(repo_a)

        # Create session B bound to repo B by opening workspace B
        resp2 = _post(port, "/api/workspace/open", {"path": str(repo_b)},
                      {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert resp2.status == 200
        data2 = json.loads(resp2.read().decode())
        assert data2["ok"] is True

        session_b_id = srv._sessions.active
        assert session_b_id is not None
        assert srv._cfg.repo == str(repo_b)

        # Clear bg_index_calls from the initial setup
        bg_index_calls.clear()

        # Step 2: Switch back to session_a via /api/sessions/switch
        # This should switch the current repo to repo_a, rebuild pilot/session,
        # trigger codegraph index if missing, and return repo + codegraph.
        resp3 = _post(port, "/api/sessions/switch", {"id": session_a_id},
                      {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert resp3.status == 200
        data3 = json.loads(resp3.read().decode())
        assert data3["ok"] is True
        assert data3["repo"] == str(repo_a)
        assert srv._cfg.repo == str(repo_a)
        assert os.environ["HARNESS_REPO"] == str(repo_a)
        
        # Verify that indexing was triggered for repo_a because .codegraph is missing
        assert str(repo_a) in bg_index_calls

        # Step 3: Switch to session_a AGAIN (same repo)
        # It should not needless trigger codegraph index or rebuild if already in same repo
        bg_index_calls.clear()
        resp4 = _post(port, "/api/sessions/switch", {"id": session_a_id},
                      {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert resp4.status == 200
        data4 = json.loads(resp4.read().decode())
        assert data4["ok"] is True
        assert data4["repo"] == str(repo_a)
        assert len(bg_index_calls) == 0

    finally:
        httpd.shutdown()


def test_codegraph_staleness(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    
    # Check stale when .codegraph is missing -> returns False
    assert _codegraph_is_stale(str(repo)) is False

    # Create .codegraph directory and set its mtime to 1000
    cg_dir = repo / ".codegraph"
    cg_dir.mkdir()
    os.utime(cg_dir, (1000, 1000))

    # Create a source file in repo with mtime 900 -> not stale (False)
    src_file = repo / "main.py"
    src_file.write_text("print('hello')")
    os.utime(src_file, (900, 900))
    assert _codegraph_is_stale(str(repo)) is False

    # Create a source file in repo with mtime 1100 -> stale (True)
    os.utime(src_file, (1100, 1100))
    assert _codegraph_is_stale(str(repo)) is True

    # Check that skipped directories (.git, node_modules, etc.) are ignored even if new
    ignored_dir = repo / "node_modules"
    ignored_dir.mkdir()
    ignored_src = ignored_dir / "lib.js"
    ignored_src.write_text("console.log()")
    os.utime(ignored_src, (1200, 1200))
    # It should still be stale because of main.py at 1100, but if we set main.py back to 900:
    os.utime(src_file, (900, 900))
    # now it should be False because ignored_src is skipped!
    assert _codegraph_is_stale(str(repo)) is False
