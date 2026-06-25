import os
import json
import shutil
import tempfile
import threading
import urllib.request
import urllib.error
import subprocess
from http.server import ThreadingHTTPServer

import pytest
from harness import worktrees as _wt


def create_temp_git_repo():
    repo_dir = tempfile.mkdtemp()
    # Use config that doesn't rely on global user
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, capture_output=True)
    
    with open(os.path.join(repo_dir, "test.txt"), "w") as f:
        f.write("hello")
    subprocess.run(["git", "add", "test.txt"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, capture_output=True)
    return repo_dir


def _server(repo_path):
    import harness.server as srv
    # override repo in config
    srv._cfg.repo = repo_path
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


def test_worktrees_module_and_endpoints():
    repo_path = create_temp_git_repo()
    httpd, port, srv = _server(repo_path)
    
    try:
        # 1. Test direct python list_worktrees
        wt_list = _wt.list_worktrees(repo_path)
        assert len(wt_list) == 1
        assert wt_list[0]["is_main"] is True
        assert wt_list[0]["branch"] == "main"
        
        # 2. Test direct python add_worktree
        # Create a new branch 'feature-1'
        new_wt = _wt.add_worktree(repo_path, "feature-1")
        assert new_wt["branch"] == "feature-1"
        assert os.path.exists(new_wt["path"])
        
        # Verify it lists
        wt_list = _wt.list_worktrees(repo_path)
        assert len(wt_list) == 2
        
        # 3. Test direct python path traversal check
        with pytest.raises(ValueError):
            # Attempt path traversal: path outside the managed dir
            _wt.add_worktree(repo_path, "feature-2", path="/tmp/dangerous")
            
        with pytest.raises(ValueError):
            _wt.remove_worktree(repo_path, "/tmp/dangerous")
            
        # 4. Test endpoints GET /api/worktrees
        resp = _get(port, "/api/worktrees")
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert "worktrees" in data
        assert "max" in data
        assert len(data["worktrees"]) == 2
        
        # 5. Test endpoint POST /api/worktrees/add
        # Add another branch 'feature-2'
        post_headers = {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN}
        resp = _post(port, "/api/worktrees/add", {"branch": "feature-2"}, post_headers)
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["branch"] == "feature-2"
        
        # Verify lists again
        resp = _get(port, "/api/worktrees")
        data = json.loads(resp.read().decode())
        assert len(data["worktrees"]) == 3
        
        # 6. Test endpoint POST /api/worktrees/remove
        # Find the path of feature-1
        feature_1_path = None
        for wt in data["worktrees"]:
            if wt["branch"] == "feature-1":
                feature_1_path = wt["path"]
                break
        assert feature_1_path is not None
        
        resp = _post(port, "/api/worktrees/remove", {"path": feature_1_path}, post_headers)
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["ok"] is True
        
        # Verify lists again
        resp = _get(port, "/api/worktrees")
        data = json.loads(resp.read().decode())
        assert len(data["worktrees"]) == 2
        
        # 7. Test endpoint POST /api/worktrees/max
        resp = _post(port, "/api/worktrees/max", {"max": 5}, post_headers)
        assert resp.status == 200
        
        resp = _get(port, "/api/worktrees")
        data = json.loads(resp.read().decode())
        assert data["max"] == 5
        
        # 8. Test security / API token protection on POST endpoints (should be 403)
        no_token_headers = {"Content-Type": "application/json"}
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(port, "/api/worktrees/add", {"branch": "feature-3"}, no_token_headers)
        assert excinfo.value.code == 403
        
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(port, "/api/worktrees/remove", {"path": feature_1_path}, no_token_headers)
        assert excinfo.value.code == 403

    finally:
        httpd.shutdown()
        # Clean up the managed worktrees directory and the main repository
        shutil.rmtree(repo_path, ignore_errors=True)
        # Check if there are other worktree paths to clean
        managed_dir = os.path.abspath(os.path.join(repo_path, "..", ".pmharness-worktrees"))
        shutil.rmtree(managed_dir, ignore_errors=True)
