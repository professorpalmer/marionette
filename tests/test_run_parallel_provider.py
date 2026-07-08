import os
import json
import shutil
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


def test_run_parallel_provider_default(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = HarnessConfig()
        cfg.repo = repo_dir
        session = ConversationalSession(cfg)

        # Pin the native engine so the parallel apply pipeline is deterministic
        # regardless of provider keys on the test host.
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: False)

        goals_seen = []
        def mock_worker_run(self):
            goals_seen.append(self.goal)
            if self.goal == "Goal A":
                patch_content = (
                    "diff --git a/a.txt b/a.txt\n"
                    "new file mode 100644\n"
                    "--- /dev/null\n"
                    "+++ b/a.txt\n"
                    "@@ -0,0 +1 @@\n"
                    "+content-a\n"
                )
                return WorkerResult(
                    ok=True,
                    patch=patch_content,
                    files_changed=["a.txt"],
                    summary="worker A succeeded"
                )
            elif self.goal == "Goal B":
                patch_content = (
                    "diff --git a/b.txt b/b.txt\n"
                    "new file mode 100644\n"
                    "--- /dev/null\n"
                    "+++ b/b.txt\n"
                    "@@ -0,0 +1 @@\n"
                    "+content-b\n"
                )
                return WorkerResult(
                    ok=True,
                    patch=patch_content,
                    files_changed=["b.txt"],
                    summary="worker B succeeded"
                )
            else:
                return WorkerResult(ok=False, error="unknown goal")

        monkeypatch.setattr(ProviderWorker, "run", mock_worker_run)

        # Mock pilot to complete and return run_parallel action with NO adapter
        mock_pilot = MagicMock()
        first_resp = MagicMock()
        first_resp.text = json.dumps({
            "say": "Running parallel provider workers",
            "actions": [{"kind": "run_parallel", "goals": ["Goal A", "Goal B"]}]
        })
        first_resp.meta = {}
        first_resp.error = None
        mock_pilot.chat.return_value = first_resp
        session.pilot = mock_pilot

        # Send a message to start the action
        events = list(session.send("start parallel"))

        # Assert correct action_start is emitted with the engine label
        action_starts = [e for e in events if e.kind == "action_start"]
        assert len(action_starts) >= 1
        specific_start = action_starts[-1]
        assert specific_start.data["kind"] == "run_parallel"
        assert specific_start.data["mode"] == "native"
        assert specific_start.data["goals"] == ["Goal A", "Goal B"]

        # Assert a single swarm_pending with 2 job_ids is emitted
        swarm_pendings = [e for e in events if e.kind == "swarm_pending"]
        assert len(swarm_pendings) == 1
        job_ids = swarm_pendings[0].data["job_ids"]
        assert len(job_ids) == 2
        assert all(jid.startswith("local-") for jid in job_ids)

        # Wait for the background worker threads to finish
        import time
        start_time = time.time()
        while time.time() - start_time < 5:
            with session._swarm_futures_lock:
                if not session._swarm_futures:
                    break
            time.sleep(0.1)

        # Drain results and assert BOTH patches got applied
        drain_events = list(session.drain_swarm_results())
        swarm_results = [e for e in drain_events if e.kind == "swarm_result"]
        assert len(swarm_results) == 2

        # Verify both files are created
        path_a = os.path.join(repo_dir, "a.txt")
        path_b = os.path.join(repo_dir, "b.txt")
        assert os.path.exists(path_a)
        assert os.path.exists(path_b)

        with open(path_a, "r") as f:
            assert f.read() == "content-a\n"
        with open(path_b, "r") as f:
            assert f.read() == "content-b\n"

        assert set(goals_seen) == {"Goal A", "Goal B"}

    finally:
        shutil.rmtree(repo_dir)


def test_run_parallel_analysis_empty_diff_applied(monkeypatch):
    """mode=analysis with empty-diff worker -> swarm_result applied=True and
    local job completed (not the red 'swarm failed' badge)."""
    import time

    repo_dir = create_temp_git_repo()
    try:
        cfg = HarnessConfig()
        cfg.repo = repo_dir
        session = ConversationalSession(cfg)
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: False)

        expects_seen = []

        def mock_worker_run(self):
            expects_seen.append(getattr(self, "expects_diff", True))
            return WorkerResult(
                ok=True,
                patch="",
                files_changed=[],
                summary="Last assistant message: Audit findings: none.",
            )

        monkeypatch.setattr(ProviderWorker, "run", mock_worker_run)

        mock_pilot = MagicMock()
        first_resp = MagicMock()
        first_resp.text = json.dumps({
            "say": "Running analysis",
            "actions": [{
                "kind": "run_parallel",
                "goals": ["Audit auth"],
                "mode": "analysis",
            }],
        })
        first_resp.meta = {}
        first_resp.error = None
        mock_pilot.chat.return_value = first_resp
        session.pilot = mock_pilot

        events = list(session.send("audit please"))
        swarm_pendings = [e for e in events if e.kind == "swarm_pending"]
        assert len(swarm_pendings) == 1
        job_ids = swarm_pendings[0].data["job_ids"]
        assert len(job_ids) == 1
        job_id = job_ids[0]

        start_time = time.time()
        while time.time() - start_time < 5:
            with session._swarm_futures_lock:
                if not session._swarm_futures:
                    break
            time.sleep(0.1)

        drain_events = list(session.drain_swarm_results())
        swarm_results = [e for e in drain_events if e.kind == "swarm_result"]
        assert len(swarm_results) == 1
        res = swarm_results[0].data["result"]
        assert res["applied"] is True
        assert res.get("error") in (None, "")
        assert res.get("has_patch_art") is False
        assert "Audit findings" in (res.get("summary") or "")

        with session._local_jobs_lock:
            job = session._local_jobs.get(job_id)
            assert job is not None
            assert job["status"] == "completed"
            assert job["role"] == "analysis"

        assert expects_seen == [False]
    finally:
        shutil.rmtree(repo_dir)


def test_run_parallel_implement_empty_diff_not_applied(monkeypatch):
    """Implement mode empty diff still surfaces applied=False (swarm failed badge)."""
    import time

    repo_dir = create_temp_git_repo()
    try:
        cfg = HarnessConfig()
        cfg.repo = repo_dir
        session = ConversationalSession(cfg)
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: False)

        def mock_worker_run(self):
            assert getattr(self, "expects_diff", True) is True
            return WorkerResult(
                ok=False,
                patch="",
                summary="no changes captured in the worktree diff (worktree=/tmp/x)",
            )

        monkeypatch.setattr(ProviderWorker, "run", mock_worker_run)

        mock_pilot = MagicMock()
        first_resp = MagicMock()
        first_resp.text = json.dumps({
            "say": "Implementing",
            "actions": [{
                "kind": "run_parallel",
                "goals": ["Add a feature"],
                "mode": "implement",
            }],
        })
        first_resp.meta = {}
        first_resp.error = None
        mock_pilot.chat.return_value = first_resp
        session.pilot = mock_pilot

        events = list(session.send("implement please"))
        job_ids = [e for e in events if e.kind == "swarm_pending"][0].data["job_ids"]
        job_id = job_ids[0]

        start_time = time.time()
        while time.time() - start_time < 5:
            with session._swarm_futures_lock:
                if not session._swarm_futures:
                    break
            time.sleep(0.1)

        drain_events = list(session.drain_swarm_results())
        swarm_results = [e for e in drain_events if e.kind == "swarm_result"]
        assert len(swarm_results) == 1
        res = swarm_results[0].data["result"]
        assert res["applied"] is False

        with session._local_jobs_lock:
            job = session._local_jobs.get(job_id)
            assert job["status"] == "completed" or job["status"] == "failed"
            # _finish_local_job uses ok=not error; empty-diff failure has error
            # from apply_msg path -- either failed or completed depending on
            # whether error key is set. applied=False is the badge contract.
    finally:
        shutil.rmtree(repo_dir)
