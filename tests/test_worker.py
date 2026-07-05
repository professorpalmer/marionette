import os
import shutil
import tempfile
import subprocess
import pytest

from harness.worker import ProviderWorker, WorkerResult, is_obviously_destructive
from harness.conversation import ConversationalSession, ConvEvent
from harness.autobudget import AutoBudget
from harness.worktrees import _is_repo


def create_temp_git_repo():
    repo_dir = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, capture_output=True)
    
    with open(os.path.join(repo_dir, "test.txt"), "w") as f:
        f.write("hello\n")
    subprocess.run(["git", "add", "test.txt"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, capture_output=True)
    return repo_dir


def test_is_obviously_destructive():
    # Destructive patterns
    assert is_obviously_destructive("rm -rf /") is True
    assert is_obviously_destructive("rm -rf ~") is True
    assert is_obviously_destructive(":(){:|:&};:") is True
    assert is_obviously_destructive("mkfs.ext4 /dev/sdb1") is True
    assert is_obviously_destructive("dd if=/dev/zero of=/dev/sd") is True
    assert is_obviously_destructive("git push origin --force") is True
    assert is_obviously_destructive("git push --force") is True
    assert is_obviously_destructive("RM -RF /") is True  # Case insensitive
    assert is_obviously_destructive("  rm   -rf   ~  ") is True  # Whitespace robust
    
    # Safe patterns
    assert is_obviously_destructive("pytest -q") is False
    assert is_obviously_destructive("git diff") is False
    assert is_obviously_destructive("rm -rf temp_folder_name") is False
    assert is_obviously_destructive("echo hello") is False

    # Catastrophic roots stay blocked, including flag-order/verbosity variants
    # and bare top-level system directories.
    assert is_obviously_destructive("rm -fr /") is True
    assert is_obviously_destructive("rm -rfv /") is True
    assert is_obviously_destructive("rm -rf /*") is True
    assert is_obviously_destructive("rm -rf /etc") is True
    assert is_obviously_destructive("rm -rf /home") is True
    assert is_obviously_destructive("rm -rf /Users") is True
    assert is_obviously_destructive("rm -rf ~/*") is True
    assert is_obviously_destructive("rm -rf $HOME") is True

    # Legitimate absolute project/temp cleanups must NOT be flagged -- this is
    # the over-broad `rm -rf /` regex fix (previously any absolute path matched).
    assert is_obviously_destructive("rm -rf /home/user/project/build") is False
    assert is_obviously_destructive("rm -rf /Users/cary/pm-harness/dist") is False
    assert is_obviously_destructive("rm -rf /var/folders/tmp/xyz") is False
    assert is_obviously_destructive("rm -rf ~/project/node_modules") is False

    # --force-with-lease is the SAFE variant and must not be denied, even though
    # its substring is `--force`. (The plain force-push above stays blocked.)
    assert is_obviously_destructive("git push --force-with-lease origin main") is False
    assert is_obviously_destructive("git push --force-with-lease") is False


def test_patch_subprocess_run_nested_guard_stays_armed():
    """Concurrent workers share one reference-counted guard: an inner/earlier
    holder exiting must not disarm the destructive-command guard while an outer
    holder is still active (the run_parallel race)."""
    from harness.worker import patch_subprocess_run

    real_run = subprocess.run
    with patch_subprocess_run("/tmp"):
        assert subprocess.run is not real_run  # guard armed
        with patch_subprocess_run("/tmp"):
            assert subprocess.run is not real_run
        # Inner holder exited; guard MUST remain armed for the outer holder.
        assert subprocess.run is not real_run, "guard disarmed while an outer worker is still active"
        blocked = subprocess.run("rm -rf /", shell=True)
        assert blocked.returncode == 1
        assert "rejected by safety guardrails" in (blocked.stdout or "")
    # Last holder exited -> fully restored.
    assert subprocess.run is real_run


def test_worker_not_git_repo():
    temp_dir = tempfile.mkdtemp()
    try:
        worker = ProviderWorker(repo=temp_dir, goal="add something")
        res = worker.run()
        assert res.ok is False
        assert "not a git repo" in res.error
    finally:
        shutil.rmtree(temp_dir)


def test_worker_success(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        # We monkeypatch ConversationalSession.run_auto to simulate writing a file and yielding some events.
        def mock_run_auto(self, objective, budget=None, require_codegraph=True):
            # self is the ConversationalSession instance
            assert self.config.repo != repo_dir  # Must be a separate worktree path
            assert os.path.exists(self.config.repo)
            
            # Write a real file in the worktree
            filepath = os.path.join(self.config.repo, "added_by_worker.txt")
            with open(filepath, "w") as f:
                f.write("this is a new file created by the worker\n")
                
            yield ConvEvent("message", {"text": "I have created the added_by_worker.txt file."})
            yield ConvEvent("auto_halt", {"reason": "pilot reports objective met"})

        monkeypatch.setattr(ConversationalSession, "run_auto", mock_run_auto)

        # Let's run the worker
        worker = ProviderWorker(
            repo=repo_dir,
            goal="Create a file added_by_worker.txt with custom text",
            run_tests="echo 'tests passed'",
            keep_worktree_on_failure=True
        )
        
        # Verify the setup
        assert worker.repo == os.path.abspath(repo_dir)
        assert worker.goal == "Create a file added_by_worker.txt with custom text"
        
        res = worker.run()
        
        # Verify result
        assert res.ok is True
        assert res.patch != ""
        assert "added_by_worker.txt" in res.files_changed
        assert "added_by_worker.txt" in res.patch
        assert "this is a new file created by the worker" in res.patch
        assert "tests passed" in res.test_output
        assert "pilot reports objective met" in res.summary
        assert "I have created the added_by_worker.txt file." in res.summary
        
        # Verify worktree is cleaned up on success
        assert not os.path.exists(res.worktree)
        
        # Verify the patch applies cleanly to the original repo
        patch_file = os.path.join(repo_dir, "change.patch")
        with open(patch_file, "w") as f:
            f.write(res.patch)
            
        p_apply = subprocess.run(
            ["git", "apply", "change.patch"],
            cwd=repo_dir,
            capture_output=True,
            text=True
        )
        assert p_apply.returncode == 0
        
        # Verify original repo now has the file
        created_file_path = os.path.join(repo_dir, "added_by_worker.txt")
        assert os.path.exists(created_file_path)
        with open(created_file_path, "r") as f:
            assert f.read() == "this is a new file created by the worker\n"
            
    finally:
        shutil.rmtree(repo_dir)


def test_worker_empty_change(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        def mock_run_auto_empty(self, objective, budget=None, require_codegraph=True):
            yield ConvEvent("message", {"text": "I looked around but made no changes."})
            yield ConvEvent("auto_halt", {"reason": "pilot reports objective met"})

        monkeypatch.setattr(ConversationalSession, "run_auto", mock_run_auto_empty)

        worker = ProviderWorker(
            repo=repo_dir,
            goal="Inspect the repository",
            keep_worktree_on_failure=True
        )
        
        res = worker.run()
        
        assert res.ok is False
        assert res.patch == ""
        assert res.files_changed == []
        # Summary was made truthful in the empty-diff branch: it now names
        # the worktree it inspected instead of lying with "no changes produced".
        # See harness.worker._detect_escaped_writes and the finalize block.
        assert res.summary.startswith('no changes captured in the worktree diff')
        assert res.worktree in res.summary
        
        # Worktree should still be cleaned up because success of the run itself is False but keep_worktree_on_failure only retains on execution failure (exceptions), not on empty diff.
        # Wait, if patch is empty, is it a success or a failure of the execution?
        # In our ProviderWorker, success = True when it finishes without exception. So it is cleaned up successfully!
        assert not os.path.exists(res.worktree)
        
    finally:
        shutil.rmtree(repo_dir)


def test_worker_destructive_guards(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        commands_run = []
        
        # We monkeypatch run_auto to run a destructive command, and a safe command
        def mock_run_auto_destructive(self, objective, budget=None, require_codegraph=True):
            # Try running a destructive command
            p_dest = subprocess.run("rm -rf /", shell=True)
            commands_run.append(("rm -rf /", p_dest.returncode, p_dest.stdout))
            
            # Try running a safe command
            p_safe = subprocess.run("echo hello_safe", shell=True, capture_output=True, text=True)
            commands_run.append(("echo hello_safe", p_safe.returncode, p_safe.stdout.strip()))
            
            yield ConvEvent("auto_halt", {"reason": "done"})

        monkeypatch.setattr(ConversationalSession, "run_auto", mock_run_auto_destructive)

        worker = ProviderWorker(repo=repo_dir, goal="test guards")
        res = worker.run()
        
        # Check that rm -rf / was intercepted and mocked
        assert len(commands_run) == 2
        
        cmd1, code1, out1 = commands_run[0]
        assert cmd1 == "rm -rf /"
        assert code1 == 1
        assert "rejected by safety guardrails" in out1
        
        cmd2, code2, out2 = commands_run[1]
        assert cmd2 == "echo hello_safe"
        assert code2 == 0
        assert out2 == "hello_safe"
        
    finally:
        shutil.rmtree(repo_dir)


def test_worker_leaf_mode_schemas_and_defense(monkeypatch):
    from unittest.mock import MagicMock, patch
    import json
    from harness.pilot import build_tools_schema
    from harness.config import HarnessConfig

    # 1. build_tools_schema with no_delegation=True
    schema_worker = build_tools_schema(no_delegation=True)
    tool_names_worker = {t["function"]["name"] for t in schema_worker}

    # Excludes delegation actions
    assert "run_implement" not in tool_names_worker
    assert "run_parallel" not in tool_names_worker
    assert "run_swarm" not in tool_names_worker

    # Includes direct-edit actions
    assert "write_file" in tool_names_worker
    assert "read_file" in tool_names_worker
    assert "run_command" in tool_names_worker
    assert "list_dir" in tool_names_worker
    assert "search_codegraph" not in tool_names_worker
    assert "query_wiki" not in tool_names_worker

    # 2. build_tools_schema by default (regression guard)
    schema_default = build_tools_schema()
    tool_names_default = {t["function"]["name"] for t in schema_default}
    assert "run_implement" in tool_names_default
    assert "run_parallel" in tool_names_default
    assert "run_swarm" in tool_names_default
    assert "search_codegraph" in tool_names_default
    assert "query_wiki" in tool_names_default

    # 3. Verify ProviderWorker constructs session with no_delegation=True
    repo_dir = create_temp_git_repo()
    try:
        captured_no_delegation = None
        original_init = ConversationalSession.__init__

        def mock_init(self, config):
            nonlocal captured_no_delegation
            captured_no_delegation = getattr(config, "no_delegation", False)
            original_init(self, config)

        monkeypatch.setattr(ConversationalSession, "__init__", mock_init)

        def mock_run_auto(self, objective, budget=None, require_codegraph=True):
            yield ConvEvent("auto_halt", {"reason": "pilot reports objective met"})

        monkeypatch.setattr(ConversationalSession, "run_auto", mock_run_auto)

        worker = ProviderWorker(
            repo=repo_dir,
            goal="test no_delegation"
        )
        worker.run()

        assert captured_no_delegation is True
    finally:
        shutil.rmtree(repo_dir)

    # 4. Verify defense-in-depth on action loop
    config = HarnessConfig(no_delegation=True, repo=os.path.abspath("."))
    session = ConversationalSession(config)

    # Mock pilot chat() to return a run_implement attempt
    mock_pilot = MagicMock()
    first_resp = MagicMock()
    first_resp.text = json.dumps({
        "say": "Trying to delegate",
        "actions": [{"kind": "run_implement", "goal": "nested task"}]
    })
    first_resp.meta = {}
    first_resp.error = None
    mock_pilot.chat.return_value = first_resp
    session.pilot = mock_pilot

    # Trigger action loop
    events = list(session.send("trigger delegation"))

    # Assert defense-in-depth warning was produced
    action_results = [e for e in events if e.kind == "action_result"]
    assert len(action_results) >= 1
    assert "delegation is disabled for workers" in action_results[0].data.get("error", "")


def test_provider_worker_leak_and_failure_cleanup(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        # Mock run_auto to write a file
        def mock_run_auto(self, objective, budget=None, require_codegraph=True):
            filepath = os.path.join(self.config.repo, "added_by_worker.txt")
            with open(filepath, "w") as f:
                f.write("this is a new file created by the worker\n")
            yield ConvEvent("message", {"text": "Wrote the file."})
            yield ConvEvent("auto_halt", {"reason": "pilot reports objective met"})

        monkeypatch.setattr(ConversationalSession, "run_auto", mock_run_auto)

        # 1. SUCCESS PATH
        worker = ProviderWorker(
            repo=repo_dir,
            goal="Add a file",
            keep_worktree_on_failure=False  # default is False now
        )
        res = worker.run()
        assert res.ok is True

        # Assert worktree dir is gone
        assert not os.path.exists(res.worktree)

        # Assert no pmworker-* branch remains
        p_branches = subprocess.run(
            ["git", "-C", repo_dir, "branch", "--list", "pmworker-*"],
            capture_output=True,
            text=True
        )
        assert p_branches.stdout.strip() == ""

        # 2. FAILURE PATH (must clean up by default too)
        def mock_run_auto_fail(self, objective, budget=None, require_codegraph=True):
            # Write a file but raise an error to trigger failure
            filepath = os.path.join(self.config.repo, "added_by_worker_fail.txt")
            with open(filepath, "w") as f:
                f.write("failed\n")
            raise RuntimeError("something went wrong")

        monkeypatch.setattr(ConversationalSession, "run_auto", mock_run_auto_fail)

        worker_fail = ProviderWorker(
            repo=repo_dir,
            goal="This will fail",
            keep_worktree_on_failure=False  # must clean up by default
        )
        res_fail = worker_fail.run()
        assert res_fail.ok is False
        assert "something went wrong" in res_fail.error

        # Assert worktree dir is gone
        assert not os.path.exists(res_fail.worktree)

        # Assert no pmworker-* branch remains
        p_branches = subprocess.run(
            ["git", "-C", repo_dir, "branch", "--list", "pmworker-*"],
            capture_output=True,
            text=True
        )
        assert p_branches.stdout.strip() == ""

    finally:
        shutil.rmtree(repo_dir)

