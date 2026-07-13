"""Tests for the open-workspace runtime flow and its REST endpoints."""
import json
import os
import shutil
import tempfile
import threading
import urllib.request
import urllib.error
import subprocess
from http.server import ThreadingHTTPServer


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


def test_open_workspace_endpoints():
    httpd, port, srv = _server()
    try:
        # 1. POST /api/workspace/open without token -> 403
        try:
            _post(port, "/api/workspace/open", {"path": "/tmp"}, {"Content-Type": "application/json"})
            assert False, "should have failed with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        headers = {
            "Content-Type": "application/json",
            "X-Harness-Token": srv._TOKEN
        }

        # 2. POST /api/workspace/open with a non-existent path -> 400
        try:
            _post(port, "/api/workspace/open", {"path": "/nonexistent/path/here/12345"}, headers)
            assert False, "should have failed with 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            resp_body = json.loads(e.read().decode())
            assert "existing directory" in resp_body["error"]

        # 3. Create a temp directory with a git repository.
        # Manual mkdtemp + best-effort rmtree instead of TemporaryDirectory:
        # opening the workspace kicks off async probes (codegraph status) that
        # can briefly hold the dir on Windows, where an in-use file makes
        # cleanup raise. (TemporaryDirectory's ignore_cleanup_errors needs 3.10;
        # CI still runs 3.9.)
        tmpdir = tempfile.mkdtemp()
        try:
            real_tmp = os.path.realpath(tmpdir)
            # init git repo
            subprocess.run(["git", "init", "-b", "main", real_tmp], capture_output=True, check=True)
            # configure git dummy user
            subprocess.run(["git", "-C", real_tmp, "config", "user.name", "Test User"], check=True)
            subprocess.run(["git", "-C", real_tmp, "config", "user.email", "test@example.com"], check=True)
            # commit something so HEAD exists and rev-parse branch works
            test_file = os.path.join(real_tmp, "README.md")
            with open(test_file, "w") as f:
                f.write("# Temp Repo")
            subprocess.run(["git", "-C", real_tmp, "add", "README.md"], check=True)
            subprocess.run(["git", "-C", real_tmp, "commit", "-m", "initial commit"], check=True)

            # POST /api/workspace/open with this real git repo path
            res = _post(port, "/api/workspace/open", {"path": real_tmp}, headers)
            assert res.status == 200
            data = json.loads(res.read().decode())
            assert data["ok"] is True
            assert data["repo"] == real_tmp
            assert data["is_git"] is True
            assert data["branch"] == "main"

            # 4. GET /api/workspace -> verify it returns the open workspace info
            res_get = _get(port, f"/api/workspace?token={srv._TOKEN}", {"X-Harness-Token": srv._TOKEN})
            assert res_get.status == 200
            data_get = json.loads(res_get.read().decode())
            assert data_get["repo"] == real_tmp
            assert data_get["is_git"] is True
            assert data_get["branch"] == "main"
            assert "codegraph_status" in data_get
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    finally:
        httpd.shutdown()


def test_forget_recent_workspace(monkeypatch, tmp_path):
    import json
    import os
    import tempfile
    import harness.server as srv

    # workspace.json now resolves under HARNESS_STATE_DIR (state-home isolation),
    # so point that at tmp_path instead of patching a module constant.
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    ws_file = tmp_path / "workspace.json"
    
    # Create some dummy directory paths (must be real directories to be persistable)
    dir1 = tmp_path / "dir1"
    dir1.mkdir()
    dir2 = tmp_path / "dir2"
    dir2.mkdir()
    
    # We want these directories to be considered persistable, so let's mock the tempdir check
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/some/other/dummy/path")
    
    # Record them -- append-if-new; re-open must NOT move to front.
    recents = srv._record_recent_workspace(str(dir1))
    assert recents == [str(dir1)]

    recents = srv._record_recent_workspace(str(dir2))
    assert recents == [str(dir1), str(dir2)]

    # Re-open dir1: position stays; active repo key still updates.
    recents = srv._record_recent_workspace(str(dir1))
    assert recents == [str(dir1), str(dir2)]
    with open(ws_file) as f:
        data = json.load(f)
        assert data["repo"] == str(dir1)
        assert data["recents"] == [str(dir1), str(dir2)]

    # Switch active back to dir2 before forget (forget clears repo when it
    # matches the forgotten path).
    srv._record_recent_workspace(str(dir2))

    # Now forget dir1
    recents = srv._forget_recent_workspace(str(dir1))
    assert str(dir1) not in recents
    assert str(dir2) in recents

    # Check that file actually wrote the correct JSON
    with open(ws_file) as f:
        data = json.load(f)
        assert data["repo"] == str(dir2)
        assert str(dir1) not in data["recents"]

    # Forgetting the active workspace clears the boot-restore repo key so the
    # rail cannot re-append it as a phantom via buildProjectsList.
    recents = srv._forget_recent_workspace(str(dir2))
    assert str(dir2) not in recents
    with open(ws_file) as f:
        data = json.load(f)
        assert data["repo"] == ""
        assert data["recents"] == []


def test_forget_recent_workspace_normalizes_path_spelling(monkeypatch, tmp_path):
    """Slash/case variants of the same root must all drop on forget."""
    import json
    import os
    import tempfile
    import harness.server as srv

    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/some/other/dummy/path")
    ws_file = tmp_path / "workspace.json"

    dir1 = tmp_path / "Ashita"
    dir1.mkdir()
    stored = str(dir1)
    # Record with native spelling.
    srv._record_recent_workspace(stored)

    # Forget with a deliberately different slash/case form when on Windows.
    if os.name == "nt":
        alt = stored.replace("\\", "/")
        if alt[0].isupper():
            alt = alt[0].lower() + alt[1:]
        elif alt[0].islower():
            alt = alt[0].upper() + alt[1:]
    else:
        alt = stored

    recents = srv._forget_recent_workspace(alt)
    assert stored not in recents
    with open(ws_file) as f:
        data = json.load(f)
        assert data["recents"] == []
        assert data["repo"] == ""


def test_record_recent_dedupes_path_spellings(monkeypatch, tmp_path):
    import os
    import tempfile
    import harness.server as srv

    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/some/other/dummy/path")

    dir1 = tmp_path / "proj"
    dir1.mkdir()
    a = str(dir1)
    srv._record_recent_workspace(a)
    if os.name == "nt":
        b = a.replace("\\", "/")
        recents = srv._record_recent_workspace(b)
        assert len(recents) == 1
    else:
        recents = srv._record_recent_workspace(a)
        assert len(recents) == 1


class _OpenProjectPilot:
    def __init__(self, path):
        self.path = path
        self.calls = 0
    def complete(self, prompt, *, system=None):
        from pmharness.drivers.openai_compat import DriverResponse
        self.calls += 1
        if self.calls == 1:
            # json.dumps so Windows backslash paths are escaped as valid JSON
            # (an f-string would inject raw '\U...' sequences the parser rejects).
            txt = json.dumps({
                "say": "Opening...",
                "actions": [{"kind": "open_project", "path": self.path}],
            })
        else:
            txt = '{"say":"Done","actions":[]}'
        return DriverResponse(text=txt, tokens_out=10, latency_ms=1.0)


def test_open_project_action_validates_and_succeeds(monkeypatch, tmp_path):
    import os
    import pytest
    from harness.conversation import ConversationalSession, ConvEvent
    from harness.config import HarnessConfig
    
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path))
    session = ConversationalSession(cfg)
    
    # 1. Test non-existent path
    nonexistent = tmp_path / "nonexistent"
    session.pilot = _OpenProjectPilot(str(nonexistent))
    
    events = list(session.send("open the project please"))
    # The action should start and then return result with error
    action_results = [e for e in events if e.kind == "action_result"]
    assert len(action_results) == 1
    assert "error" in action_results[0].data
    assert "not an existing directory" in action_results[0].data["error"]
    
    # 2. Test successful path
    existing = tmp_path / "existing"
    existing.mkdir()
    
    session = ConversationalSession(cfg)
    session.pilot = _OpenProjectPilot(str(existing))
    
    events = list(session.send("open the project please"))
    action_results = [e for e in events if e.kind == "action_result"]
    assert len(action_results) == 1
    assert "error" not in action_results[0].data
    assert "workspace" in action_results[0].data.get("types", [])
    assert os.path.normcase(os.path.abspath(action_results[0].data["workspace_root"])) == \
        os.path.normcase(os.path.abspath(str(existing)))
    
    # Check that environment and config are updated
    assert session.config.repo == str(existing)
    assert os.environ["HARNESS_REPO"] == str(existing)
