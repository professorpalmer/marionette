import concurrent.futures
import os
import json
import sys
import subprocess
from unittest.mock import MagicMock
from types import SimpleNamespace

import pytest

from harness.worker import WorkerResult
from harness.conversation import ConversationalSession
from harness.config import HarnessConfig


@pytest.fixture(autouse=True)
def _workers_ready(monkeypatch):
    # workers_ready() only falls through to agentic_available() when the
    # key/platform probe raises. Isolated CI has no keys, so the send
    # loop refuses run_implement before per-test agentic_available mocks.
    monkeypatch.setattr("harness.edit_engines.workers_ready", lambda: True)

# Bind the real runner at import so a leaked xdist subprocess.run mock
# cannot silently skip git init (macOS then emits run_implement without mode).
_REAL_RUN = subprocess.run


@pytest.fixture
def git_repo(tmp_path):
    repo_dir = os.path.realpath(tmp_path / "repo")
    os.makedirs(repo_dir)
    _REAL_RUN(["git", "init", "-b", "main"], cwd=repo_dir, capture_output=True, check=True)
    _REAL_RUN(["git", "config", "user.name", "Test User"], cwd=repo_dir, capture_output=True, check=True)
    _REAL_RUN(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, capture_output=True, check=True)
    with open(os.path.join(repo_dir, "test.txt"), "w") as f:
        f.write("hello\n")
    _REAL_RUN(["git", "add", "test.txt"], cwd=repo_dir, capture_output=True, check=True)
    _REAL_RUN(["git", "commit", "-m", "initial commit"], cwd=repo_dir, capture_output=True, check=True)
    git_marker = os.path.join(repo_dir, ".git")
    if not (os.path.isdir(git_marker) or os.path.isfile(git_marker)):
        raise RuntimeError("temp git repo is missing .git: " + repo_dir)
    return repo_dir


def _session(repo_dir):
    cfg = HarnessConfig()
    cfg.repo = repo_dir
    return ConversationalSession(cfg)


def _pilot_with_actions(say, actions):
    mock_pilot = MagicMock()
    first_resp = MagicMock()
    first_resp.text = json.dumps({"say": say, "actions": actions})
    first_resp.meta = {}
    first_resp.error = None
    mock_pilot.chat.return_value = first_resp
    return mock_pilot


def _wait_swarm_futures(session, timeout=5):
    with session._swarm_futures_lock:
        pending = set(session._swarm_futures)
    concurrent.futures.wait(pending, timeout=timeout)


def _implement_starts(events):
    return [e for e in events if e.kind == "action_start" and e.data.get("kind") == "run_implement"]


def _action_errors(events):
    return [e.data.get("error") or e.data.get("message") or "" for e in events if e.kind == "action_result"]


def test_run_implement_provider_default(monkeypatch, git_repo):
    repo_dir = git_repo
    session = _session(repo_dir)

    # Supply an available agentic worker independently of host credentials.
    monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)
    monkeypatch.setattr("harness.edit_engines.agentic_platform_enabled", lambda: True)

    # Return a patch at the agentic worker boundary.
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
        # resolve_effective_repo canonicalizes via git toplevel, which can
        # differ textually (Windows 8.3 short paths, macOS /private/var).
        assert os.path.normcase(os.path.realpath(self.repo)) == os.path.normcase(
            os.path.realpath(repo_dir))
        assert self.goal == "Add world to test.txt"
        return canned_result

    monkeypatch.setattr("harness.edit_engines.run_agentic_edit", lambda config, goal, **kwargs: mock_worker_run(SimpleNamespace(goal=goal, repo=config.repo)))

    # Pilot returns a run_implement action with NO adapter.
    session.pilot = _pilot_with_actions(
        "Running provider worker",
        [{"kind": "run_implement", "goal": "Add world to test.txt"}],
    )

    events = list(session.send("start implement"))

    # 1. Assert correct start/pending ConvEvents are emitted
    action_starts = _implement_starts(events)
    assert action_starts, _action_errors(events)
    specific_start = action_starts[-1]
    assert specific_start.data.get("mode") == "agentic", (
        specific_start.data, _action_errors(events)
    )

    swarm_pendings = [e for e in events if e.kind == "swarm_pending"]
    assert len(swarm_pendings) == 1
    job_id = swarm_pendings[0].data["job_ids"][0]
    assert job_id.startswith("local-")

    # 2. Wait for the background worker in the pool to finish.
    _wait_swarm_futures(session)

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


@pytest.mark.parametrize(
    "cli_available",
    [pytest.param(True, id="cli_present"), pytest.param(False, id="cli_absent")],
)
def test_run_implement_explicit_external_adapter_is_rejected(
    monkeypatch, git_repo, cli_available,
):
    """An explicit non-agentic adapter fails closed whether its CLI is installed or not."""
    session = _session(git_repo)

    # Mock puppetmaster available (dispatch lives in send_loop after the peel)
    monkeypatch.setattr("harness.send_loop_dispatch._puppetmaster_available", lambda: True)
    # CLI availability must not authorize an unsupported product adapter.
    monkeypatch.setattr(
        ConversationalSession, "_external_adapter_available", lambda self, adapter: cli_available,
    )
    # An available agentic engine must not silently replace an explicit pin.
    monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)
    monkeypatch.setattr("harness.edit_engines.agentic_platform_enabled", lambda: True)

    pm_cmd_called = []
    def mock_pm_cmd(*args, **kwargs):
        pm_cmd_called.append(args)
        # `echo` is a shell builtin, not an executable, so spawning it as
        # an argv list fails on Windows; a python -c print works everywhere.
        return [sys.executable, "-c", "print('job_123456789012')"]
    monkeypatch.setattr("harness.send_loop_dispatch._puppetmaster_cmd", mock_pm_cmd)

    session.pilot = _pilot_with_actions(
        "Running external cursor-agent",
        [{"kind": "run_implement", "goal": "Add world to test.txt", "adapter": "cursor"}],
    )

    events = list(session.send("start implement"))

    # Explicit non-agentic adapters fail closed; no CLI fallback is allowed.
    action_starts = _implement_starts(events)
    assert action_starts, _action_errors(events)
    assert "mode" not in action_starts[-1].data

    results = [e for e in events if e.kind == "action_result"]
    assert any("unsupported" in (e.data.get("error") or "").lower() for e in results), _action_errors(events)
    assert not any(e.kind == "swarm_pending" for e in events)
    assert not pm_cmd_called
    assert not any("cursor" in c for c in pm_cmd_called)
