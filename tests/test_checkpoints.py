import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import urllib.error

import pytest

from harness.checkpoints import CheckpointStore


@pytest.fixture
def temp_git_repo():
    temp_dir = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=temp_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=temp_dir, check=True)
        
        # Create an initial commit so we have a valid HEAD
        file1 = os.path.join(temp_dir, "file1.txt")
        with open(file1, "w") as f:
            f.write("initial content")
        subprocess.run(["git", "add", "file1.txt"], cwd=temp_dir, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=temp_dir, check=True)
        
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir)


def test_checkpoint_lifecycle(temp_git_repo):
    repo = temp_git_repo
    store = CheckpointStore(repo)
    assert store._enabled is True

    # 1. Take snapshot of base state
    c1 = store.snapshot(label="Base state", trigger="test")
    assert c1 is not None

    # Verify commit exists and list() returns it
    lst = store.list()
    assert len(lst) == 1
    assert lst[0]["id"] == c1
    assert lst[0]["label"] == "Base state"
    assert lst[0]["trigger"] == "test"

    # Save HEAD before modifications
    head_proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    orig_head = head_proc.stdout.strip()

    # 2. Modify a file, create an untracked file
    file1 = os.path.join(repo, "file1.txt")
    with open(file1, "w") as f:
        f.write("modified content")

    file2 = os.path.join(repo, "file2.txt")
    with open(file2, "w") as f:
        f.write("untracked file content")

    # 3. Restore base checkpoint
    res = store.restore(c1)
    assert res["ok"] is True
    assert "auto_snapshot_id" in res

    # Verify HEAD didn't move
    head_proc_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    assert head_proc_after.stdout.strip() == orig_head

    # Verify file1 was restored to "initial content"
    with open(file1, "r") as f:
        assert f.read() == "initial content"

    # Verify file2 (created after checkpoint) was removed
    assert not os.path.exists(file2)

    # 4. Verify auto-snapshot works: restoring the auto-snapshot should bring back modifications!
    auto_id = res["auto_snapshot_id"]
    res_undo = store.restore(auto_id)
    assert res_undo["ok"] is True

    with open(file1, "r") as f:
        assert f.read() == "modified content"
    with open(file2, "r") as f:
        assert f.read() == "untracked file content"


def test_checkpoint_non_git():
    temp_dir = tempfile.mkdtemp()
    try:
        store = CheckpointStore(temp_dir)
        assert store._enabled is False
        assert store.snapshot("Test", "test") is None
        assert store.list() == []
        res = store.restore("some_id")
        assert res["ok"] is False
        assert "disabled" in res["error"]
    finally:
        shutil.rmtree(temp_dir)


def _server():
    import harness.server as srv
    from http.server import ThreadingHTTPServer
    import threading
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


def test_api_endpoints_protection(temp_git_repo):
    repo = temp_git_repo
    httpd, port, srv = _server()
    
    # Configure the active server repository to point to our test repo
    srv._cfg.repo = repo
    
    try:
        # 1. GET /api/checkpoints without token -> 403
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(port, "/api/checkpoints")
        assert exc.value.code == 403

        # 2. GET /api/checkpoints with token -> 200
        resp = _get(port, f"/api/checkpoints?token={srv._TOKEN}")
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert isinstance(data, list)

        # 3. POST /api/checkpoints/snapshot without token -> 403
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(port, "/api/checkpoints/snapshot", {"label": "Manual"}, {"Content-Type": "application/json"})
        assert exc.value.code == 403

        # 4. POST /api/checkpoints/snapshot with token -> 200
        resp = _post(port, "/api/checkpoints/snapshot", {"label": "Manual"}, 
                    {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert resp.status == 200
        snap_res = json.loads(resp.read().decode())
        assert snap_res["ok"] is True
        assert "id" in snap_res
        checkpoint_id = snap_res["id"]

        # 5. POST /api/checkpoints/restore without token -> 403
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(port, "/api/checkpoints/restore", {"id": checkpoint_id}, {"Content-Type": "application/json"})
        assert exc.value.code == 403

        # 6. POST /api/checkpoints/restore with token -> 200
        resp = _post(port, "/api/checkpoints/restore", {"id": checkpoint_id}, 
                    {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert resp.status == 200
        rest_res = json.loads(resp.read().decode())
        assert rest_res["ok"] is True
        assert "auto_snapshot_id" in rest_res

    finally:
        httpd.shutdown()


def test_checkpoint_diff(temp_git_repo):
    repo = temp_git_repo
    store = CheckpointStore(repo)
    
    # Take initial snapshot
    c1 = store.snapshot(label="Initial state", trigger="test")
    assert c1 is not None

    # 1. Unchanged tree diff is empty
    res = store.diff(c1)
    assert res["ok"] is True
    assert res["diff"].strip() == ""
    assert len(res["files"]) == 0
    assert res["truncated"] is False

    # 2. Modify a file, add a file
    file1 = os.path.join(repo, "file1.txt")
    with open(file1, "w") as f:
        f.write("modified content here")
    
    file2 = os.path.join(repo, "file2.txt")
    with open(file2, "w") as f:
        f.write("new untracked file")

    res2 = store.diff(c1)
    assert res2["ok"] is True
    # file1 is modified, file2 is removed on restore (since it's not in the checkpoint c1)
    files = {f["path"]: f["status"] for f in res2["files"]}
    assert files["file1.txt"] == "modified"
    assert files["file2.txt"] == "removed"

    # 3. Bad ID is handled gracefully
    res_bad = store.diff("invalidid1234567890")
    assert res_bad["ok"] is False

def test_api_checkpoints_diff_protection(temp_git_repo):
    repo = temp_git_repo
    httpd, port, srv = _server()
    srv._cfg.repo = repo
    
    try:
        # Create a checkpoint
        store = CheckpointStore(repo)
        c1 = store.snapshot("Test", "test")
        
        # 1. GET /api/checkpoints/diff without token -> 403
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(port, f"/api/checkpoints/diff?id={c1}")
        assert exc.value.code == 403

        # 2. GET /api/checkpoints/diff with token -> 200
        resp = _get(port, f"/api/checkpoints/diff?id={c1}&token={srv._TOKEN}")
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["ok"] is True
        assert "diff" in data
        assert "files" in data
    finally:
        httpd.shutdown()
