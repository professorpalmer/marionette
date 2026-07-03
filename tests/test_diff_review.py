import pytest
import tempfile
import shutil
import os
import subprocess
import json
import urllib.request
import urllib.error
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch, MagicMock

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.diffreview import parse_unified_diff, reconstruct_diff


@pytest.fixture
def temp_git_repo():
    dirpath = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "init"], cwd=dirpath, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=dirpath, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=dirpath, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        # Write base files
        file1 = os.path.join(dirpath, "file1.txt")
        with open(file1, "w") as f:
            f.write("Line A\nLine B\nLine C\n")
            
        file2 = os.path.join(dirpath, "file2.txt")
        with open(file2, "w") as f:
            f.write("Alpha\nBeta\nGamma\n")
            
        subprocess.run(["git", "add", "file1.txt", "file2.txt"], cwd=dirpath, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=dirpath, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        yield dirpath
    finally:
        shutil.rmtree(dirpath, ignore_errors=True)


def test_diff_parser_and_reconstruct():
    diff_text = (
        "diff --git a/file1.txt b/file1.txt\n"
        "--- a/file1.txt\n"
        "+++ b/file1.txt\n"
        "@@ -1,3 +1,4 @@\n"
        " Line A\n"
        "+Line A.5\n"
        " Line B\n"
        " Line C\n"
        "diff --git a/file2.txt b/file2.txt\n"
        "--- a/file2.txt\n"
        "+++ b/file2.txt\n"
        "@@ -1,3 +1,4 @@\n"
        " Alpha\n"
        "+Delta\n"
        " Beta\n"
        " Gamma\n"
    )
    
    parsed = parse_unified_diff(diff_text)
    assert len(parsed) == 2
    assert parsed[0]["path"] == "file1.txt"
    assert parsed[1]["path"] == "file2.txt"
    
    assert len(parsed[0]["hunks"]) == 1
    assert len(parsed[1]["hunks"]) == 1
    
    hunk1_id = parsed[0]["hunks"][0]["id"]
    hunk2_id = parsed[1]["hunks"][0]["id"]
    
    # Reconstruct with only the first hunk accepted
    decisions = {hunk1_id: "accept", hunk2_id: "reject"}
    new_diff = reconstruct_diff(parsed, decisions)
    
    assert "file1.txt" in new_diff
    assert "Line A.5" in new_diff
    assert "file2.txt" not in new_diff
    assert "Delta" not in new_diff


def test_reconstruct_diff_preserves_index_headers():
    """Partial-hunk accept must keep the `index <blob>..<blob>` ancestor SHAs
    (and the diff --git / --- / +++ headers). Those blob identities are exactly
    what lets `git apply --3way` reconstruct the ancestor and do a REAL 3-way
    merge onto a moved tree. If reconstruct_diff ever drops them, the apply
    silently degrades to context-only matching -- the corruption class we
    removed the lenient tier to prevent. This locks the invariant."""
    diff_text = (
        "diff --git a/file1.txt b/file1.txt\n"
        "index 1111111..2222222 100644\n"
        "--- a/file1.txt\n"
        "+++ b/file1.txt\n"
        "@@ -1,3 +1,4 @@\n"
        " Line A\n"
        "+Line A.5\n"
        " Line B\n"
        " Line C\n"
        "@@ -10,2 +11,3 @@\n"
        " Line J\n"
        "+Line J.5\n"
        " Line K\n"
    )

    parsed = parse_unified_diff(diff_text)
    assert len(parsed) == 1
    assert len(parsed[0]["hunks"]) == 2

    # Accept only the first hunk, reject the second.
    hunk_a = parsed[0]["hunks"][0]["id"]
    hunk_b = parsed[0]["hunks"][1]["id"]
    rebuilt = reconstruct_diff(parsed, {hunk_a: "accept", hunk_b: "reject"})

    # The ancestor-blob line and every file header must survive verbatim.
    assert "index 1111111..2222222 100644" in rebuilt
    assert "diff --git a/file1.txt b/file1.txt" in rebuilt
    assert "--- a/file1.txt" in rebuilt
    assert "+++ b/file1.txt" in rebuilt
    # Accepted hunk present, rejected hunk gone.
    assert "Line A.5" in rebuilt
    assert "Line J.5" not in rebuilt


def test_apply_review(temp_git_repo):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = temp_git_repo
    session = ConversationalSession(cfg)
    session._review_edits_before_apply = True
    
    # Mocking some artifacts containing a patch
    artifacts = [
        {
            "type": "patch",
            "payload": {
                "files": ["file1.txt", "file2.txt"],
                "unified_diff": (
                    "diff --git a/file1.txt b/file1.txt\n"
                    "--- a/file1.txt\n"
                    "+++ b/file1.txt\n"
                    "@@ -1,3 +1,4 @@\n"
                    " Line A\n"
                    "+Line A.5\n"
                    " Line B\n"
                    " Line C\n"
                    "diff --git a/file2.txt b/file2.txt\n"
                    "--- a/file2.txt\n"
                    "+++ b/file2.txt\n"
                    "@@ -1,3 +1,4 @@\n"
                    " Alpha\n"
                    "+Delta\n"
                    " Beta\n"
                    " Gamma\n"
                )
            }
        }
    ]
    
    original_run = subprocess.run
    def mock_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "git":
            return original_run(cmd, *args, **kwargs)
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        if "artifacts" in cmd_str:
            mock_p = MagicMock()
            mock_p.returncode = 0
            mock_p.stdout = json.dumps(artifacts)
            return mock_p
        mock_p = MagicMock()
        mock_p.returncode = 0
        mock_p.stdout = ""
        return mock_p

    with patch("subprocess.run", side_effect=mock_run):
        # Process job which triggers review hold since review_edits_before_apply is True
        res = session._await_and_apply_job("job-123", state_dir=None, objective="Test edits")
        assert res["held_for_review"] is True
        assert res["applied"] is False
        
        pending = res["pending_review"]
        assert pending is not None
        review_id = pending["id"]
        
        # Verify the pending review exists in session storage
        assert review_id in session._pending_reviews
        review_item = session._pending_reviews[review_id]
        assert review_item["objective"] == "Test edits"
        
        # Now let's apply the review with decisions: accept first hunk, reject second
        hunk1_id = review_item["files"][0]["hunks"][0]["id"]
        hunk2_id = review_item["files"][1]["hunks"][0]["id"]
        
        decisions = {hunk1_id: "accept", hunk2_id: "reject"}
        apply_res = session.apply_review(review_id, decisions)
        
        assert apply_res["ok"] is True
        assert "file1.txt" in apply_res["applied_files"]
        assert hunk2_id in apply_res["rejected_hunks"]
        
        # Check that it took a checkpoint
        assert apply_res["checkpoint_id"] is not None
        
        # Confirm file1 was modified but file2 was not
        with open(os.path.join(temp_git_repo, "file1.txt")) as f:
            content1 = f.read()
        assert "Line A.5" in content1
        
        with open(os.path.join(temp_git_repo, "file2.txt")) as f:
            content2 = f.read()
        assert "Delta" not in content2
        
        # Verify the pending review was cleared
        assert review_id not in session._pending_reviews


def test_review_edits_before_apply_off_by_default(temp_git_repo):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = temp_git_repo
    session = ConversationalSession(cfg)
    
    # Off by default
    assert session._review_edits_before_apply is False
    
    artifacts = [
        {
            "type": "patch",
            "payload": {
                "files": ["file1.txt"],
                "unified_diff": (
                    "diff --git a/file1.txt b/file1.txt\n"
                    "--- a/file1.txt\n"
                    "+++ b/file1.txt\n"
                    "@@ -1,3 +1,4 @@\n"
                    " Line A\n"
                    "+Line A.5\n"
                    " Line B\n"
                    " Line C\n"
                )
            }
        }
    ]
    
    original_run = subprocess.run
    def mock_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "git":
            return original_run(cmd, *args, **kwargs)
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        if "artifacts" in cmd_str:
            mock_p = MagicMock()
            mock_p.returncode = 0
            mock_p.stdout = json.dumps(artifacts)
            return mock_p
        mock_p = MagicMock()
        mock_p.returncode = 0
        mock_p.stdout = ""
        return mock_p

    with patch("subprocess.run", side_effect=mock_run):
        # This should auto-apply
        res = session._await_and_apply_job("job-456", state_dir=None, objective="Test edits")
        assert res["applied"] is True
        assert res.get("held_for_review") is not True


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


def test_reviews_endpoints_403_without_token():
    httpd, port, srv = _server()
    try:
        # GET reviews without token -> 403
        try:
            _get(port, "/api/reviews")
            assert False, "GET should have returned 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
            
        # POST apply without token -> 403
        try:
            _post(port, "/api/reviews/apply", {"id": "rev-123", "decisions": {}}, {})
            assert False, "POST apply should have returned 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # POST dismiss without token -> 403
        try:
            _post(port, "/api/reviews/dismiss", {"id": "rev-123"}, {})
            assert False, "POST dismiss should have returned 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()
