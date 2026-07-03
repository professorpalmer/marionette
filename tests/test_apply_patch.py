import pytest
import tempfile
import shutil
import os
import subprocess
import json
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession

@pytest.fixture
def temp_git_repo():
    dirpath = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "init"], cwd=dirpath, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=dirpath, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=dirpath, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        # Write base file
        base_file = os.path.join(dirpath, "base.txt")
        with open(base_file, "w") as f:
            f.write("Line 1\nLine 2\nLine 3\n")
            
        subprocess.run(["git", "add", "base.txt"], cwd=dirpath, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=dirpath, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        yield dirpath
    finally:
        shutil.rmtree(dirpath, ignore_errors=True)

def test_apply_worker_patch_create_file(temp_git_repo):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = temp_git_repo
    session = ConversationalSession(cfg)
    
    artifacts = [
        {
            "type": "patch",
            "payload": {
                "files": ["new_file.txt"],
                "unified_diff": "diff --git a/new_file.txt b/new_file.txt\nnew file mode 100644\n--- /dev/null\n+++ b/new_file.txt\n@@ -0,0 +1 @@\n+hello world from worker\n"
            }
        }
    ]
    
    applied, files, msg = session._apply_worker_patch(artifacts)
    assert applied is True
    assert files == ["new_file.txt"]
    assert "applied cleanly" in msg or "applied with 3way merge" in msg
    
    # Assert file exists with exact contents
    new_filepath = os.path.join(temp_git_repo, "new_file.txt")
    assert os.path.exists(new_filepath)
    with open(new_filepath, "r") as f:
        assert f.read() == "hello world from worker\n"

def test_apply_worker_patch_idempotency(temp_git_repo):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = temp_git_repo
    session = ConversationalSession(cfg)
    
    artifacts = [
        {
            "type": "patch",
            "payload": {
                "files": ["new_file.txt"],
                "unified_diff": "diff --git a/new_file.txt b/new_file.txt\nnew file mode 100644\n--- /dev/null\n+++ b/new_file.txt\n@@ -0,0 +1 @@\n+hello world from worker\n"
            }
        }
    ]
    
    # 1st apply
    applied, files, msg = session._apply_worker_patch(artifacts)
    assert applied is True
    
    # 2nd apply (idempotency check)
    applied_2, files_2, msg_2 = session._apply_worker_patch(artifacts)
    assert applied_2 is True
    assert files_2 == ["new_file.txt"]
    assert "already applied" in msg_2
    
    # Assert file is still correct
    new_filepath = os.path.join(temp_git_repo, "new_file.txt")
    assert os.path.exists(new_filepath)
    with open(new_filepath, "r") as f:
        assert f.read() == "hello world from worker\n"

def test_apply_worker_patch_modify_file(temp_git_repo):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = temp_git_repo
    session = ConversationalSession(cfg)
    
    artifacts = [
        {
            "type": "patch",
            "payload": {
                "files": ["base.txt"],
                "unified_diff": "diff --git a/base.txt b/base.txt\n--- a/base.txt\n+++ b/base.txt\n@@ -1,3 +1,4 @@\n Line 1\n+Line 1.5\n Line 2\n Line 3\n"
            }
        }
    ]
    
    applied, files, msg = session._apply_worker_patch(artifacts)
    assert applied is True
    assert files == ["base.txt"]
    assert "applied cleanly" in msg or "applied with 3way merge" in msg
    
    base_filepath = os.path.join(temp_git_repo, "base.txt")
    with open(base_filepath, "r") as f:
        assert f.read() == "Line 1\nLine 1.5\nLine 2\nLine 3\n"

def test_apply_worker_patch_not_cleanly(temp_git_repo):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = temp_git_repo
    session = ConversationalSession(cfg)
    
    # Context mismatch on base.txt
    artifacts = [
        {
            "type": "patch",
            "payload": {
                "files": ["base.txt"],
                "unified_diff": "diff --git a/base.txt b/base.txt\n--- a/base.txt\n+++ b/base.txt\n@@ -1,3 +1,3 @@\n Nonexistent Line\n-Line 2\n+Line 2 Modified\n Line 3\n"
            }
        }
    ]
    
    applied, files, msg = session._apply_worker_patch(artifacts)
    assert applied is False
    assert "patch did not apply cleanly" in msg
    
    # Assert base.txt remains unchanged
    base_filepath = os.path.join(temp_git_repo, "base.txt")
    with open(base_filepath, "r") as f:
        assert f.read() == "Line 1\nLine 2\nLine 3\n"

def _capture_git_patch(repo, rel_path, new_content):
    """Produce a real `git diff` patch (carrying the `index <blob>..<blob>`
    ancestor SHAs) for editing rel_path to new_content, then restore the file.
    Mirrors what finalize_worktree_patch emits from a worker worktree."""
    abs_path = os.path.join(repo, rel_path)
    with open(abs_path, "r") as f:
        original = f.read()
    try:
        with open(abs_path, "w") as f:
            f.write(new_content)
        p = subprocess.run(
            ["git", "diff", "--no-color", "--", rel_path],
            cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
        )
        return p.stdout
    finally:
        with open(abs_path, "w") as f:
            f.write(original)


def test_apply_worker_patch_conflict_restores_tree(temp_git_repo):
    """A genuinely conflicting patch must FAIL loudly (no lenient force-land)
    AND leave the working tree byte-identical -- git apply --3way writes
    conflict markers and returns non-zero on a real conflict, so the harness
    must restore the pre-apply bytes. This guards audit finding #8: a stale
    diff never silently corrupts a file that a concurrent edit already moved."""
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = temp_git_repo
    session = ConversationalSession(cfg)

    # Worker patch (real git diff, has an index line) that rewrites Line 2.
    worker_patch = _capture_git_patch(temp_git_repo, "base.txt", "Line 1\nLine 2 WORKER\nLine 3\n")
    assert "index " in worker_patch  # ancestor blob SHAs present

    # A conflicting edit landed on the SAME line since the worker branched.
    base_filepath = os.path.join(temp_git_repo, "base.txt")
    local_content = "Line 1\nLine 2 LOCAL\nLine 3\n"
    with open(base_filepath, "w") as f:
        f.write(local_content)

    artifacts = [{"type": "patch", "payload": {"files": ["base.txt"], "unified_diff": worker_patch}}]
    applied, files, msg = session._apply_worker_patch(artifacts)

    assert applied is False
    assert "patch did not apply cleanly" in msg
    # Tree restored exactly -- no conflict markers, no half-applied worker hunk.
    with open(base_filepath, "r") as f:
        restored = f.read()
    assert restored == local_content
    assert "<<<<<<<" not in restored
    assert "WORKER" not in restored


def test_apply_worker_patch_no_patch_artifact(temp_git_repo):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = temp_git_repo
    session = ConversationalSession(cfg)
    
    artifacts = [
        {
            "type": "finding",
            "payload": {
                "report": "Some other finding"
            }
        }
    ]
    
    applied, files, msg = session._apply_worker_patch(artifacts)
    assert applied is False
    assert files == []
    assert msg == "no patch to apply"
