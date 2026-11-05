import os
import json
import shutil
import tempfile
import urllib.request
import urllib.error
import threading
import subprocess
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture
def isolated_server_state(monkeypatch, tmp_path, _isolate_provider_state):
    """Keep the HTTP integration tests' store, view and pilot in one lifetime."""
    from dataclasses import replace
    import harness.server as srv
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession
    from harness.session import Session
    from harness.session_runners import SessionRunnerRegistry
    from harness.sessions import SessionStore

    # Workspace opens use the real synchronous attach path; deferred attach
    # races have their own tests and must not outlive these temporary stores.
    monkeypatch.setenv("HARNESS_DEFER_COLD_ATTACH", "0")
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path))
    sessions = SessionStore(str(tmp_path / "harness_sessions.json"))
    monkeypatch.setattr(srv, "_cfg", cfg)
    monkeypatch.setattr(srv, "_sessions", sessions)
    monkeypatch.setattr(srv, "_session", Session(cfg))
    monkeypatch.setattr(srv, "_runners", SessionRunnerRegistry())
    monkeypatch.setattr(srv, "_pilot_swap_lock", threading.Lock())
    monkeypatch.setattr(srv, "_pilot", ConversationalSession(replace(cfg)))
    srv._bind_pilot_services(srv._pilot)
    srv._boot_usage_reset_for_tests()
    yield srv
    sessions.flush()
    srv._usage_cache_clear_for_tests()


pytestmark = pytest.mark.usefixtures("isolated_server_state")


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


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {}, method="GET")
    return urllib.request.urlopen(req, timeout=10)


def test_ergonomics_workspace_files():
    httpd, port, srv = _server()
    try:
        # 1. GET /api/workspace/files without token -> 403
        try:
            _get(port, "/api/workspace/files")
            assert False, "Should have returned 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        headers = {
            "Content-Type": "application/json",
            "X-Harness-Token": srv._TOKEN
        }

        # 2. Open a temporary workspace and write some files.
        # Manual mkdtemp + best-effort rmtree instead of TemporaryDirectory:
        # opening the workspace kicks off async probes (codegraph status) that
        # can briefly hold the dir open on Windows, where an in-use file turns
        # cleanup into a PermissionError. (TemporaryDirectory's
        # ignore_cleanup_errors needs 3.10; CI still runs 3.9.)
        tmpdir = tempfile.mkdtemp()
        try:
            real_tmp = os.path.realpath(tmpdir)
            # init git repo
            subprocess.run(["git", "init", "-b", "main", real_tmp], capture_output=True, check=True)
            
            # create files
            os.makedirs(os.path.join(real_tmp, "src"), exist_ok=True)
            os.makedirs(os.path.join(real_tmp, ".git"), exist_ok=True)
            os.makedirs(os.path.join(real_tmp, "node_modules"), exist_ok=True)
            
            with open(os.path.join(real_tmp, "src/main.py"), "w") as f:
                f.write("print('hello')")
            with open(os.path.join(real_tmp, "node_modules/bad.py"), "w") as f:
                f.write("bad")
            with open(os.path.join(real_tmp, "README.md"), "w") as f:
                f.write("# Sample Readme")

            # Open workspace
            res_open = _post(port, "/api/workspace/open", {"path": real_tmp}, headers)
            assert res_open.status == 200

            # GET /api/workspace/files with auth header
            res_files = _get(port, "/api/workspace/files", headers)
            assert res_files.status == 200
            data = json.loads(res_files.read().decode())
            
            # Should contain workspace files, and exclude node_modules / .git
            assert "files" in data
            files = data["files"]
            assert "README.md" in files
            assert "src/main.py" in files
            # node_modules and .git files must be skipped
            assert not any(f.startswith("node_modules") for f in files)
            assert not any(f.startswith(".git") for f in files)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    finally:
        httpd.shutdown()
        httpd.server_close()


def test_ergonomics_session_compact():
    httpd, port, srv = _server()
    try:
        # 1. POST /api/session/compact without token -> 403
        try:
            _post(port, "/api/session/compact", {}, {})
            assert False, "Should have failed with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        headers = {
            "Content-Type": "application/json",
            "X-Harness-Token": srv._TOKEN
        }

        # 2. POST /api/session/compact with token: too little history to
        # compact, so the endpoint reports a truthful no-op (409) instead of
        # a false success. The fixture provides a fresh, ready pilot.
        assert len(srv._pilot._history) <= 1
        try:
            _post(port, "/api/session/compact", {}, headers)
            assert False, "tiny history should report no-op compaction"
        except urllib.error.HTTPError as e:
            assert e.code == 409
            data = json.loads(e.read().decode())
            assert data["ok"] is False
            assert data["compacted"] is False
            assert "before_tokens" in data
            assert "after_tokens" in data

    finally:
        httpd.shutdown()
        httpd.server_close()
