import os
import json
import shutil
import sys
import tempfile
import subprocess
from unittest.mock import patch, MagicMock

from harness.worker import ProviderWorker, WorkerResult
from harness.conversation import ConversationalSession, ConvEvent
from harness.config import HarnessConfig


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


def test_run_implement_provider_default(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = HarnessConfig()
        cfg.repo = repo_dir
        session = ConversationalSession(cfg)

        # Pin the native engine so this exercises Marionette's own pilot + the
        # apply pipeline deterministically regardless of which provider keys the
        # test host happens to have (agentic is the default only when a key exists).
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: False)

        # Mock ProviderWorker.run to return a canned patch
        canned_patch = (
            "diff --git a/test.txt b/test.txt\n"
            "--- a/test.txt\n"
            "+++ b/test.txt\n"
            "@@ -1,1 +1,2 @@\n"
            " hello\n"
            "+world\n"
        )
        canned_result = WorkerResult(
            ok=True,
            patch=canned_patch,
            files_changed=["test.txt"],
            summary="worker created a nice patch"
        )
        
        run_called = []
        def mock_worker_run(self):
            run_called.append(True)
            # Assert correct config parameters flowed to the worker
            assert self.repo == os.path.abspath(repo_dir)
            assert self.goal == "Add world to test.txt"
            return canned_result

        monkeypatch.setattr(ProviderWorker, "run", mock_worker_run)

        # Mock pilot completing and returning run_implement action with NO adapter
        mock_pilot = MagicMock()
        first_resp = MagicMock()
        first_resp.text = json.dumps({
            "say": "Running provider worker",
            "actions": [{"kind": "run_implement", "goal": "Add world to test.txt"}]
        })
        first_resp.meta = {}
        first_resp.error = None
        mock_pilot.chat.return_value = first_resp
        session.pilot = mock_pilot

        # Send a message to start the action
        events = list(session.send("start implement"))

        # 1. Assert correct start/pending ConvEvents are emitted
        action_starts = [e for e in events if e.kind == "action_start"]
        assert len(action_starts) >= 1
        # The specific action_start should be the last one
        specific_start = action_starts[-1]
        assert specific_start.data["kind"] == "run_implement"
        assert specific_start.data["mode"] == "native"

        swarm_pendings = [e for e in events if e.kind == "swarm_pending"]
        assert len(swarm_pendings) == 1
        job_id = swarm_pendings[0].data["job_ids"][0]
        assert job_id.startswith("local-")

        # 2. Wait for the background worker thread to finish (since it runs in the pool)
        # We can poll session._swarm_futures until all are done
        import time
        start_time = time.time()
        while time.time() - start_time < 5:
            with session._swarm_futures_lock:
                if not session._swarm_futures:
                    break
            time.sleep(0.1)

        # Drain and assert results are correct and patch got applied
        drain_events = list(session.drain_swarm_results())
        swarm_results = [e for e in drain_events if e.kind == "swarm_result"]
        assert len(swarm_results) == 1
        assert swarm_results[0].data["job_id"] == job_id
        assert swarm_results[0].data["result"]["applied"] is True
        assert swarm_results[0].data["result"]["files"] == ["test.txt"]

        # Assert the patch was actually applied to the file
        with open(os.path.join(repo_dir, "test.txt"), "r") as f:
            assert f.read() == "hello\nworld\n"

        assert run_called == [True]

    finally:
        shutil.rmtree(repo_dir)


def test_run_implement_external_fallback(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = HarnessConfig()
        cfg.repo = repo_dir
        session = ConversationalSession(cfg)

        # Mock puppetmaster available
        monkeypatch.setattr("harness.conversation._puppetmaster_available", lambda: True)
        # Mock the external adapter CLI as available so this test exercises the
        # external dispatch path (cursor is not installed in CI; without this it
        # would correctly fall back to the provider-native worker).
        monkeypatch.setattr(ConversationalSession, "_external_adapter_available", lambda self, adapter: True)

        # Mock puppetmaster cmd
        pm_cmd_called = []
        def mock_pm_cmd(*args, **kwargs):
            pm_cmd_called.append(args)
            # `echo` is a shell builtin, not an executable, so spawning it as
            # an argv list fails on Windows; a python -c print works everywhere.
            return [sys.executable, "-c", "print('job_123456789012')"]
        monkeypatch.setattr("harness.conversation._puppetmaster_cmd", mock_pm_cmd)

        # Mock pilot completing and returning run_implement action with an external adapter
        mock_pilot = MagicMock()
        first_resp = MagicMock()
        first_resp.text = json.dumps({
            "say": "Running external cursor-agent",
            "actions": [{"kind": "run_implement", "goal": "Add world to test.txt", "adapter": "cursor"}]
        })
        first_resp.meta = {}
        first_resp.error = None
        mock_pilot.chat.return_value = first_resp
        session.pilot = mock_pilot

        # Send a message to start the action
        events = list(session.send("start implement"))

        # Assert action_start is emitted and DOES NOT have mode="provider" (meaning it took the external path)
        action_starts = [e for e in events if e.kind == "action_start"]
        assert len(action_starts) >= 1
        specific_start = action_starts[-1]
        assert specific_start.data["kind"] == "run_implement"
        assert "mode" not in specific_start.data

        # Assert correct swarm_pending is emitted with the mocked job_id from puppetmaster CLI output
        swarm_pendings = [e for e in events if e.kind == "swarm_pending"]
        assert len(swarm_pendings) == 1
        assert swarm_pendings[0].data["job_ids"] == ["job_123456789012"]

        assert len(pm_cmd_called) > 0
        assert "cursor" in pm_cmd_called[0]

    finally:
        shutil.rmtree(repo_dir)


def test_run_implement_falls_back_to_provider_when_cli_absent(monkeypatch):
    """When an external adapter (cursor/codex/claude-code) is requested but its
    CLI is not installed, the implement must fall back to the provider-native
    worker (which runs off the user's own keys) instead of hard-failing. The
    platform must never be unusable just because an optional worker CLI is gone.
    """
    repo_dir = create_temp_git_repo()
    try:
        cfg = HarnessConfig()
        cfg.repo = repo_dir
        session = ConversationalSession(cfg)

        monkeypatch.setattr("harness.conversation._puppetmaster_available", lambda: True)
        # The external adapter CLI is NOT available.
        monkeypatch.setattr(ConversationalSession, "_external_adapter_available", lambda self, adapter: False)
        # Pin the native engine so the in-process fallback is deterministic here.
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: False)

        # If the external path were taken it would call the pm CLI; assert it does NOT.
        pm_cmd_called = []
        def mock_pm_cmd(*args, **kwargs):
            pm_cmd_called.append(args)
            return ["echo", "job_should_not_be_used"]
        monkeypatch.setattr("harness.conversation._puppetmaster_cmd", mock_pm_cmd)

        mock_pilot = MagicMock()
        first_resp = MagicMock()
        first_resp.text = json.dumps({
            "say": "Running cursor implement",
            "actions": [{"kind": "run_implement", "goal": "Add world to test.txt", "adapter": "cursor"}]
        })
        first_resp.meta = {}
        first_resp.error = None
        mock_pilot.chat.return_value = first_resp
        session.pilot = mock_pilot

        events = list(session.send("start implement"))

        # The in-process fallback path emits action_start with the engine label.
        action_starts = [e for e in events if e.kind == "action_start" and e.data.get("kind") == "run_implement"]
        assert len(action_starts) >= 1
        assert action_starts[-1].data.get("mode") == "native"

        # The external CLI must NOT have been invoked for the implement dispatch.
        assert not any("cursor" in c for c in pm_cmd_called)

        # A swarm_pending should still be emitted (the in-process worker is dispatched).
        swarm_pendings = [e for e in events if e.kind == "swarm_pending"]
        assert len(swarm_pendings) == 1
    finally:
        shutil.rmtree(repo_dir)


def test_run_implement_agentic_engine_default(monkeypatch):
    """With a provider key present, run_implement routes through the first-class
    agentic engine (keys-only) and the returned patch flows through the same
    apply pipeline as the native engine."""
    repo_dir = create_temp_git_repo()
    try:
        cfg = HarnessConfig()
        cfg.repo = repo_dir
        session = ConversationalSession(cfg)

        canned_patch = (
            "diff --git a/test.txt b/test.txt\n"
            "--- a/test.txt\n"
            "+++ b/test.txt\n"
            "@@ -1,1 +1,2 @@\n"
            " hello\n"
            "+world\n"
        )
        # Agentic is available; stub the actual engine so the test stays hermetic
        # (no provider network / worktree), exercising dispatch + apply.
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)

        def fake_agentic(config, goal, **kwargs):
            assert goal == "Add world to test.txt"
            return WorkerResult(ok=True, patch=canned_patch,
                                files_changed=["test.txt"], summary="agentic patch",
                                tokens_out=1234)
        monkeypatch.setattr("harness.edit_engines.run_agentic_edit", fake_agentic)

        mock_pilot = MagicMock()
        first_resp = MagicMock()
        first_resp.text = json.dumps({
            "say": "Running agentic worker",
            "actions": [{"kind": "run_implement", "goal": "Add world to test.txt"}]
        })
        first_resp.meta = {}
        first_resp.error = None
        mock_pilot.chat.return_value = first_resp
        session.pilot = mock_pilot

        events = list(session.send("start implement"))

        action_starts = [e for e in events if e.kind == "action_start" and e.data.get("kind") == "run_implement"]
        assert len(action_starts) >= 1
        assert action_starts[-1].data["mode"] == "agentic"

        swarm_pendings = [e for e in events if e.kind == "swarm_pending"]
        assert len(swarm_pendings) == 1
        job_id = swarm_pendings[0].data["job_ids"][0]

        import time
        start_time = time.time()
        while time.time() - start_time < 5:
            with session._swarm_futures_lock:
                if not session._swarm_futures:
                    break
            time.sleep(0.1)

        drain_events = list(session.drain_swarm_results())
        swarm_results = [e for e in drain_events if e.kind == "swarm_result"]
        assert len(swarm_results) == 1
        assert swarm_results[0].data["job_id"] == job_id
        assert swarm_results[0].data["result"]["applied"] is True
        assert swarm_results[0].data["result"]["files"] == ["test.txt"]

        with open(os.path.join(repo_dir, "test.txt"), "r") as f:
            assert f.read() == "hello\nworld\n"
    finally:
        shutil.rmtree(repo_dir)
