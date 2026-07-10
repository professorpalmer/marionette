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
        # ignore_errors: git background maintenance can race the teardown walk
        # (observed on macOS CI: FileNotFoundError on maintenance.lock).
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_checkpoint_lifecycle(temp_git_repo):
    repo = temp_git_repo
    store = CheckpointStore(repo, session_id="sess-lifecycle")
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
    assert lst[0]["session_id"] == "sess-lifecycle"
    assert lst[0].get("repo_hash")

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


def test_checkpoint_session_list_isolation(temp_git_repo):
    """Two sessions in the same repo must not see each other's checkpoints."""
    repo = temp_git_repo
    store_a = CheckpointStore(repo, session_id="session-a")
    store_b = CheckpointStore(repo, session_id="session-b")

    id_a = store_a.snapshot(label="A only", trigger="test")
    id_b = store_b.snapshot(label="B only", trigger="test")
    assert id_a and id_b and id_a != id_b

    list_a = store_a.list()
    list_b = store_b.list()
    assert {c["id"] for c in list_a} == {id_a}
    assert {c["id"] for c in list_b} == {id_b}
    assert all(c["session_id"] == "session-a" for c in list_a)
    assert all(c["session_id"] == "session-b" for c in list_b)

    # No active session: stamped rows stay hidden (legacy-only list).
    open_store = CheckpointStore(repo, session_id=None)
    open_ids = {c["id"] for c in open_store.list()}
    assert id_a not in open_ids
    assert id_b not in open_ids


def test_checkpoint_restore_wrong_session_refused(temp_git_repo):
    repo = temp_git_repo
    store_a = CheckpointStore(repo, session_id="session-a")
    id_a = store_a.snapshot(label="owned by A", trigger="test")
    assert id_a

    store_b = CheckpointStore(repo, session_id="session-b")
    res = store_b.restore(id_a)
    assert res["ok"] is False
    assert "session" in res["error"].lower()

    diff = store_b.diff(id_a)
    assert diff["ok"] is False
    assert "session" in diff["error"].lower()

    # Same-session restore still works
    ok = store_a.restore(id_a)
    assert ok["ok"] is True


def test_checkpoint_different_repos_do_not_share_entries():
    """Distinct repo stores use distinct metadata files (repo_hash binding)."""
    dir_a = tempfile.mkdtemp()
    dir_b = tempfile.mkdtemp()
    try:
        for d in (dir_a, dir_b):
            subprocess.run(["git", "init"], cwd=d, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=d, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=d, check=True)
            path = os.path.join(d, "file1.txt")
            with open(path, "w") as f:
                f.write("initial")
            subprocess.run(["git", "add", "file1.txt"], cwd=d, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=d, check=True)

        store_a = CheckpointStore(dir_a, session_id="shared-session-name")
        store_b = CheckpointStore(dir_b, session_id="shared-session-name")
        assert store_a._meta_file != store_b._meta_file
        assert store_a._repo_hash != store_b._repo_hash

        id_a = store_a.snapshot(label="in A", trigger="test")
        assert id_a
        assert store_a.list()[0]["id"] == id_a
        assert store_b.list() == []

        # Wrong expected_repo refuses restore/diff
        bad = store_a.restore(id_a, expected_repo=dir_b)
        assert bad["ok"] is False
        assert "repo" in bad["error"].lower() or "workspace" in bad["error"].lower()
    finally:
        shutil.rmtree(dir_a, ignore_errors=True)
        shutil.rmtree(dir_b, ignore_errors=True)


def test_checkpoint_legacy_entries_repo_only(temp_git_repo):
    """Legacy metadata without session_id stays repo-bound and is listable."""
    repo = temp_git_repo
    store = CheckpointStore(repo, session_id="session-a")
    # Simulate a legacy entry by writing metadata without session_id
    legacy_id = store.snapshot(label="will strip", trigger="test", session_id=None)
    assert legacy_id
    # Force-remove session_id from the saved entry
    with open(store._meta_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    for entry in data:
        entry.pop("session_id", None)
    with open(store._meta_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    # Same-repo session can still see legacy
    listed = store.list(session_id="session-a")
    assert any(c["id"] == legacy_id for c in listed)

    # restore of legacy (no stamped session) is allowed for matching store
    res = store.restore(legacy_id, session_id="session-b")
    assert res["ok"] is True



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

def test_checkpoint_git_timeout_handled_gracefully(temp_git_repo, monkeypatch):
    repo = temp_git_repo
    store = CheckpointStore(repo)
    assert store._enabled is True

    def run_raises_timeout(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    monkeypatch.setattr(subprocess, "run", run_raises_timeout)

    assert store.snapshot("label", "trigger") is None
    assert store.list() == []

    restore_res = store.restore("abc123")
    assert restore_res["ok"] is False
    assert "Failed to verify checkpoint" in restore_res["error"]

    diff_res = store.diff("abc123")
    assert diff_res["ok"] is False
    assert "Failed to verify checkpoint" in diff_res["error"]

    store.prune()


def test_checkpoint_init_git_timeout_disables_store(temp_git_repo, monkeypatch):
    def run_raises_timeout(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    monkeypatch.setattr(subprocess, "run", run_raises_timeout)
    store = CheckpointStore(temp_git_repo)
    assert store._enabled is False
    assert store.snapshot("label", "trigger") is None
    assert store.list() == []


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
