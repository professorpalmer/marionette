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


def test_decision_id_assigned_and_stable_under_reorder():
    from harness.diffreview import (
        assign_decision_ids,
        decision_for_hunk,
        hunk_content_fingerprint,
        parse_unified_diff,
    )

    diff_text = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "+one\n"
        "@@ -2 +2 @@\n"
        "+two\n"
        "@@ -1 +1 @@\n"
        "+one\n"
    )
    parsed = parse_unified_diff(diff_text)
    hunks = parsed[0]["hunks"]
    assert all(h.get("decision_id") for h in hunks)
    # Exact duplicate content gets distinct decision ids.
    assert hunks[0]["decision_id"] != hunks[2]["decision_id"]
    assert hunks[0]["decision_id"].rsplit("#", 1)[0] == hunks[2]["decision_id"].rsplit("#", 1)[0]
    assert hunks[0]["decision_id"].endswith("#0")
    assert hunks[2]["decision_id"].endswith("#1")

    # Opposite decisions on duplicate-content hunks.
    decisions = {
        hunks[0]["decision_id"]: "accept",
        hunks[1]["decision_id"]: "reject",
        hunks[2]["decision_id"]: "reject",
    }
    assert decision_for_hunk(decisions, hunks[0], "a.txt") == "accept"
    assert decision_for_hunk(decisions, hunks[2], "a.txt") == "reject"

    # Reordering unrelated middle hunk must not change keys for A and the dup.
    reordered = [
        {
            "path": "a.txt",
            "headers": parsed[0]["headers"],
            "hunks": [dict(hunks[0]), dict(hunks[2]), dict(hunks[1])],
        }
    ]
    # Strip decision_ids to force reassignment from content.
    for h in reordered[0]["hunks"]:
        h.pop("decision_id", None)
    assign_decision_ids(reordered)
    ids = [h["decision_id"] for h in reordered[0]["hunks"]]
    # First duplicate-content occurrence stays #0; second stays #1 regardless of
    # where the unrelated hunk sits.
    assert ids[0] == hunks[0]["decision_id"]
    assert ids[1] == hunks[2]["decision_id"]
    assert ids[2] == hunks[1]["decision_id"]

    fp = hunk_content_fingerprint("a.txt", "@@ -1 +1 @@\n", ["+one\n"])
    assert hunks[0]["decision_id"].startswith(fp)


def test_legacy_plain_id_decisions_still_resolve():
    from harness.diffreview import decision_for_hunk

    hunk = {"id": "0:0", "header": "@@", "lines": ["+x"]}
    assert decision_for_hunk({"0:0": "accept"}, hunk, "f.py") == "accept"
    assert decision_for_hunk({}, hunk, "f.py") == "reject"


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


def test_apply_review_keeps_pending_on_failure(temp_git_repo):
    """Failed apply must leave the review queued so the user can retry/reject."""
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = temp_git_repo
    session = ConversationalSession(cfg)

    diff_text = (
        "diff --git a/file1.txt b/file1.txt\n"
        "--- a/file1.txt\n"
        "+++ b/file1.txt\n"
        "@@ -1,3 +1,4 @@\n"
        " Line A\n"
        "+Line A.5\n"
        " Line B\n"
        " Line C\n"
    )
    parsed = parse_unified_diff(diff_text)
    review_id = "rev-fail-keep"
    session._pending_reviews[review_id] = {
        "id": review_id,
        "job_id": "job-fail",
        "objective": "keep on fail",
        "files": parsed,
        "created_at": 0,
    }

    hunk_id = parsed[0]["hunks"][0]["id"]
    with patch.object(
        session,
        "_apply_worker_patch",
        return_value=(False, [], "patch did not apply cleanly"),
    ):
        res = session.apply_review(review_id, {hunk_id: "accept"})

    assert res["ok"] is False
    assert "Failed to apply" in res["message"]
    assert review_id in session._pending_reviews
    assert "Failed to apply" in session._pending_reviews[review_id].get("error", "")

    # A later successful apply still clears the review.
    with patch.object(
        session,
        "_apply_worker_patch",
        return_value=(True, ["file1.txt"], "ok"),
    ):
        session._last_checkpoint_id = "cp-retry"
        ok = session.apply_review(review_id, {hunk_id: "accept"})
    assert ok["ok"] is True
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
