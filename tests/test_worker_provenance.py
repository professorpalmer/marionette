"""Focused tests for truthful provider-worker provenance."""
from __future__ import annotations

import json
import queue
import subprocess
import threading

from harness.config import HarnessConfig
from harness.conversation_jobs import ConversationJobsMixin, _worker_provenance_text
from harness.edit_engines import run_native_edit
from harness.local_jobs import LocalJobsMixin
from harness.worker import ProviderWorker, WorkerResult
from harness.worktree_seed import (
    _list_git_status_porcelain_paths,
    _list_live_dirty_paths,
)
from pmharness.bridge import _analysis_instruction


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=str(repo), check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(repo), check=True,
                   capture_output=True)
    return repo


def test_live_dirty_paths_include_modified_and_untracked_files_only(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "tracked.txt").write_text("after\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    assert _list_live_dirty_paths(str(repo)) == ["new.txt", "tracked.txt"]


def test_git_status_porcelain_paths_include_deleted_and_renamed(tmp_path):
    repo = _git_repo(tmp_path)
    other = repo / "other.txt"
    other.write_text("other\n", encoding="utf-8")
    subprocess.run(["git", "add", "other.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "add other"], cwd=str(repo), check=True,
                   capture_output=True)

    subprocess.run(["git", "rm", "tracked.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "mv", "other.txt", "renamed.txt"], cwd=str(repo), check=True)
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    porcelain = _list_git_status_porcelain_paths(str(repo))
    assert porcelain == ["new.txt", "other.txt", "renamed.txt", "tracked.txt"]

    assert _list_live_dirty_paths(str(repo)) == ["new.txt", "renamed.txt"]


def test_provenance_text_distinguishes_disposable_worktree_and_live_tree():
    text = _worker_provenance_text({
        "managed_worktree_mode": "managed",
        "managed_worktree_path": "/tmp/managed",
        "worktree_diff_empty": True,
        "live_dirty_paths_before": ["tracked.txt", "new.txt"],
        "live_dirty_paths_after": ["tracked.txt", "new.txt"],
    })

    assert "no changes in disposable managed worktree" in text
    assert "2 pre-existing dirty paths before" in text
    assert "tracked.txt" in text


def test_analysis_instruction_labels_git_status_as_disposable():
    instruction = _analysis_instruction("audit the code", "/tmp/repo", "explore")

    assert "disposable managed worker worktree" in instruction
    assert "not the user's live checkout" in instruction


def test_run_native_edit_honors_cwd(monkeypatch, tmp_path):
    repo = _git_repo(tmp_path)
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    config = HarnessConfig()
    config.repo = str(repo)
    sentinel = WorkerResult(ok=True, summary="done")
    seen = []

    def fake_run(worker):
        seen.append(worker.repo)
        return sentinel

    monkeypatch.setattr(ProviderWorker, "run", fake_run)
    assert run_native_edit(config, "inspect", cwd=str(other_repo)) is sentinel
    assert seen == [str(other_repo)]


class _LocalJobHost(LocalJobsMixin):
    def __init__(self, state_dir, repo):
        self._local_jobs = {}
        self._local_jobs_lock = threading.RLock()
        self._local_job_cancels = {}
        self._local_jobs_path = str(state_dir / "jobs.json")
        self.harness_session_id = "session"
        self.config = type("Config", (), {"repo": str(repo), "driver": "driver"})()


class _CancelledWorkerHost(ConversationJobsMixin, _LocalJobHost):
    def __init__(self, state_dir, repo):
        _LocalJobHost.__init__(self, state_dir, repo)
        self._swarm_results = queue.Queue()
        self._apply_lock = threading.RLock()
        self._release_objective = lambda _objective: None


def test_worker_provenance_is_persisted_on_job_and_terminal_artifact(tmp_path):
    repo = _git_repo(tmp_path)
    host = _LocalJobHost(tmp_path, repo)
    provenance = {
        "managed_worktree_mode": "managed",
        "managed_worktree_path": "/tmp/managed",
        "worktree_diff_empty": True,
        "live_dirty_paths_before": ["new.txt"],
        "live_dirty_paths_after": ["new.txt"],
    }
    host._register_local_job("local-provenance", "audit", role="analysis",
                             cwd=str(repo), engine="native")
    host._finish_local_job("local-provenance", ok=True, summary="finding",
                           worker_provenance=provenance)

    saved = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
    job = saved["jobs"][0]
    terminal = next(item for item in job["artifacts"] if item["type"] == "analysis")
    assert job["worker_provenance"] == provenance
    assert terminal["worker_provenance"] == provenance


def test_cancelled_worker_adds_late_provenance_without_enqueuing_or_applying(
    monkeypatch, tmp_path
):
    repo = _git_repo(tmp_path)
    host = _CancelledWorkerHost(tmp_path, repo)
    host._register_local_job(
        "cancelled-worker", "audit", role="analysis", cwd=str(repo), engine="native"
    )
    apply_calls = []
    host._apply_worker_patch = lambda *args: apply_calls.append(args)

    def run_worker(*_args, **_kwargs):
        assert host.cancel_local_job("cancelled-worker") is True
        return WorkerResult(
            ok=True,
            summary="late result",
            patch="must not apply",
            tokens_in=11,
            tokens_out=7,
            managed_worktree_path="/tmp/managed",
            managed_worktree_mode="managed",
            worktree_diff_empty=False,
        )

    host._run_edit_worker_bounded = run_worker
    monkeypatch.setattr(
        "harness.worktree_seed._list_git_status_porcelain_paths",
        lambda _repo: ["before.txt"],
    )

    host._run_provider_worker_background("cancelled-worker", "audit")

    assert host._local_jobs["cancelled-worker"]["status"] == "cancelled"
    assert host._swarm_results.empty()
    assert apply_calls == []
    saved = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
    job = saved["jobs"][0]
    assert job["status"] == "cancelled"
    assert job["tokens"] == 18
    assert job["worker_provenance"]["live_dirty_paths_before"] == ["before.txt"]
    assert job["worker_provenance"]["live_dirty_paths_after"] == ["before.txt"]
    assert job["worker_provenance"]["managed_worktree_path"] == "/tmp/managed"
    assert job["worker_provenance"]["managed_worktree_mode"] == "managed"
    assert job["worker_provenance"]["worktree_diff_empty"] is False
    terminal = next(item for item in job["artifacts"] if item["id"].endswith("-result"))
    assert terminal["worker_provenance"] == job["worker_provenance"]


def test_cancelled_worker_provenance_collection_failure_is_best_effort(
    monkeypatch, tmp_path
):
    repo = _git_repo(tmp_path)
    host = _CancelledWorkerHost(tmp_path, repo)
    host._register_local_job(
        "cancelled-failure", "audit", role="analysis", cwd=str(repo), engine="native"
    )

    def run_worker(*_args, **_kwargs):
        assert host.cancel_local_job("cancelled-failure") is True
        raise RuntimeError("worker result unavailable")

    host._run_edit_worker_bounded = run_worker

    def fail_status(_repo):
        raise OSError("git unavailable")

    monkeypatch.setattr(
        "harness.worktree_seed._list_git_status_porcelain_paths", fail_status
    )

    host._run_provider_worker_background("cancelled-failure", "audit")

    assert host._local_jobs["cancelled-failure"]["status"] == "cancelled"
    assert host._swarm_results.empty()
    saved = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
    job = saved["jobs"][0]
    assert job["status"] == "cancelled"
    assert job["worker_provenance"]["managed_worktree_mode"] == "unknown"
