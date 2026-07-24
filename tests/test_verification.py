import os
import sys
import shutil
import tempfile
import subprocess
import pytest

# Use the interpreter running the tests, not a bare "python" that may not be on
# PATH (this env has only python3 / .venv/bin/python). Keeps these tests
# PATH-independent.
PY = sys.executable

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, ConvEvent
from harness.worker import ProviderWorker, WorkerResult
from harness.autobudget import AutoBudget

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

def test_run_verification():
    temp_dir = tempfile.mkdtemp()
    try:
        config = HarnessConfig(repo=temp_dir, verify_cmd=f'{PY} -c "print(1)"')
        session = ConversationalSession(config)
        passed, out = session._run_verification()
        assert passed is True
        assert "1" in out

        config2 = HarnessConfig(repo=temp_dir, verify_cmd=f'{PY} -c "import sys; sys.exit(1)"')
        session2 = ConversationalSession(config2)
        passed2, out2 = session2._run_verification()
        assert passed2 is False
    finally:
        shutil.rmtree(temp_dir)

def test_run_auto_verification_passing(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    try:
        config = HarnessConfig(
            repo=temp_dir,
            verify_cmd=f'{PY} -c "print(\'Verification passes\')"'
        )
        session = ConversationalSession(config)
        
        def mock_send(self, message):
            yield ConvEvent("assistant_done", {})
            
        monkeypatch.setattr(ConversationalSession, "send", mock_send)
        
        events = list(session.run_auto("Do something", budget=AutoBudget(max_idle_steps=2)))
        
        verification_events = [e for e in events if e.kind == "verification"]
        assert len(verification_events) == 1
        assert verification_events[0].data["passed"] is True
        assert "Verification passes" in verification_events[0].data["output"]
        
        halt_events = [e for e in events if e.kind == "auto_halt"]
        assert len(halt_events) == 1
        assert "verified" in halt_events[0].data["reason"]
    finally:
        shutil.rmtree(temp_dir)

def test_run_auto_verification_failing_with_retry(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    monkeypatch.setenv("HARNESS_VERIFY_MAX_RETRIES", "1")
    try:
        config = HarnessConfig(
            repo=temp_dir,
            verify_cmd=f'{PY} -c "import sys; sys.exit(1)"'
        )
        session = ConversationalSession(config)
        
        received_messages = []
        def mock_send(self, message):
            received_messages.append(message)
            yield ConvEvent("assistant_done", {})
            
        monkeypatch.setattr(ConversationalSession, "send", mock_send)
        
        events = list(session.run_auto("Do something", budget=AutoBudget(max_idle_steps=2)))
        
        verif_events = [e for e in events if e.kind == "verification"]
        assert len(verif_events) == 1
        assert verif_events[0].data["passed"] is False
        
        halt_events = [e for e in events if e.kind == "auto_halt"]
        assert len(halt_events) == 1
        assert "NOT verified" in halt_events[0].data["reason"]
        
        monkeypatch.setenv("HARNESS_VERIFY_MAX_RETRIES", "2")
        session2 = ConversationalSession(config)
        received_messages_2 = []
        def mock_send_2(self, message):
            received_messages_2.append(message)
            yield ConvEvent("assistant_done", {})
        monkeypatch.setattr(ConversationalSession, "send", mock_send_2)
        
        events2 = list(session2.run_auto("Do something", budget=AutoBudget(max_idle_steps=3)))
        
        verif_events2 = [e for e in events2 if e.kind == "verification"]
        assert len(verif_events2) == 2
        assert verif_events2[0].data["passed"] is False
        assert verif_events2[1].data["passed"] is False
        
        assert len(received_messages_2) == 2
        assert "Verification command failed" in received_messages_2[1]
        
        halt_events2 = [e for e in events2 if e.kind == "auto_halt"]
        assert len(halt_events2) == 1
        assert "NOT verified" in halt_events2[0].data["reason"]
        assert "2 retries" in halt_events2[0].data["reason"]
    finally:
        shutil.rmtree(temp_dir)

def test_worker_run_tests_failing_and_passing(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        def mock_run_auto(self, objective, budget=None, require_codegraph=True, **kwargs):
            filepath = os.path.join(self.config.repo, "added_by_worker.txt")
            with open(filepath, "w") as f:
                f.write("this is a new file created by the worker\n")
            yield ConvEvent("message", {"text": "I have created the added_by_worker.txt file."})
            yield ConvEvent("auto_halt", {"reason": "pilot reports objective met"})

        monkeypatch.setattr(ConversationalSession, "run_auto", mock_run_auto)

        # 1. Failing tests case
        worker_fail = ProviderWorker(
            repo=repo_dir,
            goal="test failing verification",
            run_tests=f'{PY} -c "import sys; sys.exit(1)"',
            keep_worktree_on_failure=True
        )
        res_fail = worker_fail.run()
        
        assert res_fail.ok is False
        assert res_fail.test_passed is False
        assert "worker tests failed" in res_fail.error
        assert res_fail.patch != ""
        assert "added_by_worker.txt" in res_fail.patch

        # 2. Passing tests case
        worker_pass = ProviderWorker(
            repo=repo_dir,
            goal="test passing verification",
            run_tests=f'{PY} -c "print(\'tests passed\')"',
            keep_worktree_on_failure=True
        )
        res_pass = worker_pass.run()
        
        assert res_pass.ok is True
        assert res_pass.test_passed is True
        assert res_pass.patch != ""
        assert "tests passed" in res_pass.test_output
    finally:
        shutil.rmtree(repo_dir)
