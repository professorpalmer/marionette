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
    # GET now requires the auth token (centralized do_GET gate). Default it in.
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


def _create_branch(repo: str, name: str) -> None:
    subprocess.run(
        ["git", "-C", repo, "branch", name],
        check=True,
        capture_output=True,
    )


def _branch_list(repo: str) -> set[str]:
    out = subprocess.run(
        ["git", "-C", repo, "branch", "--format=%(refname:short)"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def test_delete_branch_allows_pmedit_and_refuses_unrelated():
    repo = create_temp_git_repo()
    try:
        _create_branch(repo, "pmedit-deadbeef")
        _create_branch(repo, "pmworker-cafef00d")
        _create_branch(repo, "feature-keep")

        _wt.delete_branch(repo, "pmedit-deadbeef")
        _wt.delete_branch(repo, "pmworker-cafef00d")
        _wt.delete_branch(repo, "feature-keep")
        _wt.delete_branch(repo, "main")

        branches = _branch_list(repo)
        assert "pmedit-deadbeef" not in branches
        assert "pmworker-cafef00d" not in branches
        assert "feature-keep" in branches
        assert "main" in branches
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_delete_branch_refuses_current_checkout():
    repo = create_temp_git_repo()
    try:
        _create_branch(repo, "pmedit-active01")
        subprocess.run(
            ["git", "-C", repo, "checkout", "pmedit-active01"],
            check=True,
            capture_output=True,
        )
        _wt.delete_branch(repo, "pmedit-active01")
        assert "pmedit-active01" in _branch_list(repo)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_prune_orphan_edit_branches_skips_active_and_attached_worktree():
    repo = create_temp_git_repo()
    managed_dir = os.path.abspath(os.path.join(repo, "..", ".pmharness-worktrees"))
    try:
        _create_branch(repo, "pmedit-orphan1")
        _create_branch(repo, "pmworker-orphan2")
        _create_branch(repo, "feature-keep")

        attached = _wt.add_worktree(repo, "pmedit-attached")
        assert attached["branch"] == "pmedit-attached"

        _create_branch(repo, "pmedit-current")
        subprocess.run(
            ["git", "-C", repo, "checkout", "pmedit-current"],
            check=True,
            capture_output=True,
        )

        result = _wt.prune_orphan_edit_branches(repo)
        deleted = set(result["deleted"])
        assert result["count"] == len(deleted)
        assert "pmedit-orphan1" in deleted
        assert "pmworker-orphan2" in deleted
        assert "pmedit-attached" not in deleted

        branches = _branch_list(repo)
        assert "pmedit-orphan1" not in branches
        assert "pmworker-orphan2" not in branches
        assert "pmedit-attached" in branches
        assert "pmedit-current" in branches
        assert "feature-keep" in branches
        assert "main" in branches
    finally:
        shutil.rmtree(repo, ignore_errors=True)
        shutil.rmtree(managed_dir, ignore_errors=True)


def test_prune_edit_branches_endpoint():
    repo = create_temp_git_repo()
    httpd, port, srv = _server(repo)
    try:
        _create_branch(repo, "pmedit-stale99")
        post_headers = {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN}
        resp = _post(port, "/api/worktrees/prune-edit-branches", {}, post_headers)
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["ok"] is True
        assert "pmedit-stale99" in data["deleted"]
        assert data["count"] >= 1
        assert "pmedit-stale99" not in _branch_list(repo)
    finally:
        httpd.shutdown()
        shutil.rmtree(repo, ignore_errors=True)
