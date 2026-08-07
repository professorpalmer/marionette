"""Focused tests for truthful provider-worker provenance."""
from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
from unittest.mock import patch

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.conversation_jobs import (
    EMPTY_MANAGED_IMPLEMENT_EXHAUSTED,
    ConversationJobsMixin,
    _is_empty_diff_implement_failure,
    _worker_provenance_text,
)
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


def test_analysis_empty_diff_provenance_is_not_a_failure_cue():
    text = _worker_provenance_text({
        "managed_worktree_mode": "managed",
        "managed_worktree_path": "/tmp/managed",
        "worktree_diff_empty": True,
        "live_dirty_paths_before": [],
        "live_dirty_paths_after": [],
    }, expects_diff=False)

    assert "expected for read-only" in text
    assert "no changes in disposable managed worktree" not in text


def test_implement_empty_diff_provenance_keeps_no_changes_wording():
    text = _worker_provenance_text({
        "managed_worktree_mode": "managed",
        "worktree_diff_empty": True,
        "live_dirty_paths_before": [],
        "live_dirty_paths_after": [],
    }, expects_diff=True)

    assert "no changes in disposable managed worktree" in text


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


def test_empty_diff_implement_failure_detector():
    empty = WorkerResult(
        ok=False,
        summary="no changes produced",
        worktree_diff_empty=True,
        patch="",
    )
    assert _is_empty_diff_implement_failure(empty, expects_diff=True) is True
    assert _is_empty_diff_implement_failure(empty, expects_diff=False) is False
    ok_patch = WorkerResult(
        ok=True,
        summary="done",
        worktree_diff_empty=False,
        patch="diff --git a/a b/a\n",
    )
    assert _is_empty_diff_implement_failure(ok_patch, expects_diff=True) is False


def test_empty_managed_implement_triggers_one_recovery_when_live_dirty(monkeypatch):
    """Empty managed implement + dirty live tree re-invokes the edit engine once."""
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)
    job_id = "job_empty_recover"
    session._register_local_job(
        job_id, goal="polish mockup", role="implement", engine="native",
    )

    empty = WorkerResult(
        ok=False,
        summary="no changes produced",
        patch="",
        files_changed=[],
        tokens_in=10,
        tokens_out=4,
        tokens_cached=0,
        engine="native",
        model="stub",
        managed_worktree_path="/tmp/managed-wt",
        managed_worktree_mode="managed",
        worktree_diff_empty=True,
    )
    recovered = WorkerResult(
        ok=True,
        summary="patched styles",
        patch=(
            "diff --git a/styles.css b/styles.css\n"
            "--- a/styles.css\n"
            "+++ b/styles.css\n"
            "@@ -1 +1,2 @@\n"
            " body{}\n"
            "+html{}\n"
        ),
        files_changed=["styles.css"],
        tokens_in=20,
        tokens_out=8,
        tokens_cached=0,
        engine="native",
        model="stub",
        managed_worktree_path="/tmp/managed-wt",
        managed_worktree_mode="managed",
        worktree_diff_empty=False,
    )
    calls: list[str] = []

    def fake_worker(objective, *_args, **_kwargs):
        calls.append(objective)
        if len(calls) == 1:
            return empty
        return recovered

    monkeypatch.setattr(
        "harness.worktree_seed._list_git_status_porcelain_paths",
        lambda _repo: ["app.js", "index.html", "styles.css"],
    )
    # Avoid mutating a real checkout when the recovered patch is applied.
    session._apply_worker_patch = lambda *_a, **_k: (True, ["styles.css"], "ok")

    with patch.object(session, "_run_edit_worker_bounded", side_effect=fake_worker):
        session._run_provider_worker_background(
            job_id, "polish the mockup", expects_diff=True,
        )

    assert len(calls) == 2
    assert "[recovery]" in calls[1]
    assert "styles.css" in calls[1]
    item = session._swarm_results.get_nowait()
    assert session._swarm_results.empty()
    result = item["result"]
    assert result["error"] is None
    assert result["applied"] is True
    assert result["files"] == ["styles.css"]
    # Both attempts' tokens roll up onto the same job_id.
    assert result["tokens_in"] == 30
    assert result["tokens_out"] == 12
    assert EMPTY_MANAGED_IMPLEMENT_EXHAUSTED not in (result.get("summary") or "")
    finished = session._local_jobs[job_id]
    assert finished["status"] == "completed"
    assert finished["worker_provenance"]["empty_implement_recovery"] is True
    assert finished["worker_provenance"]["empty_managed_implement_exhausted"] is False


def test_empty_managed_implement_recovery_exhausted_is_honest(monkeypatch):
    """Still-empty after one recovery must soft-refuse further identical retries."""
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)
    job_id = "job_empty_exhausted"
    session._register_local_job(
        job_id, goal="polish mockup", role="implement", engine="native",
    )

    def empty_result(tokens_in: int, tokens_out: int) -> WorkerResult:
        return WorkerResult(
            ok=False,
            summary="no changes produced",
            patch="",
            files_changed=[],
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_cached=0,
            engine="native",
            model="stub",
            managed_worktree_path="/tmp/managed-wt",
            managed_worktree_mode="managed",
            worktree_diff_empty=True,
        )

    calls: list[str] = []

    def fake_worker(objective, *_args, **_kwargs):
        calls.append(objective)
        return empty_result(11 if len(calls) == 1 else 9, 3)

    monkeypatch.setattr(
        "harness.worktree_seed._list_git_status_porcelain_paths",
        lambda _repo: ["app.js", "index.html", "styles.css"],
    )

    with patch.object(session, "_run_edit_worker_bounded", side_effect=fake_worker):
        session._run_provider_worker_background(
            job_id, "polish the mockup", expects_diff=True,
        )

    assert len(calls) == 2
    item = session._swarm_results.get_nowait()
    assert session._swarm_results.empty()
    result = item["result"]
    assert result["applied"] is False
    assert result["error"]
    summary = result.get("summary") or ""
    assert EMPTY_MANAGED_IMPLEMENT_EXHAUSTED in summary
    assert "Do NOT call run_implement again" in summary
    assert result["tokens_in"] == 20
    assert result["tokens_out"] == 6
    finished = session._local_jobs[job_id]
    assert finished["status"] == "failed"
    assert finished["worker_provenance"]["empty_managed_implement_exhausted"] is True


def test_analysis_empty_diff_skips_recovery_retry(monkeypatch):
    """expects_diff=False must not burn a recovery attempt on empty worktree."""
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)
    job_id = "job_analysis_no_recover"
    session._register_local_job(
        job_id, goal="audit mockup", role="analysis", engine="native",
    )
    calls: list[str] = []

    def fake_worker(objective, *_args, **_kwargs):
        calls.append(objective)
        return WorkerResult(
            ok=True,
            summary=(
                "FINDING: styles.css:12 uses inline styles that fight the "
                "shared theme tokens in theme.py"
            ),
            patch="",
            files_changed=[],
            tokens_in=5,
            tokens_out=5,
            managed_worktree_mode="managed",
            worktree_diff_empty=True,
            findings=[{
                "type": "finding",
                "headline": "styles.css:12 uses inline styles",
                "body": "styles.css:12 uses inline styles that fight theme.py",
            }],
        )

    monkeypatch.setattr(
        "harness.worktree_seed._list_git_status_porcelain_paths",
        lambda _repo: ["app.js", "styles.css"],
    )

    with patch.object(session, "_run_edit_worker_bounded", side_effect=fake_worker):
        session._run_provider_worker_background(
            job_id, "audit the mockup", expects_diff=False,
        )

    assert len(calls) == 1
    assert "[recovery]" not in calls[0]
    item = session._swarm_results.get_nowait()
    result = item["result"]
    assert EMPTY_MANAGED_IMPLEMENT_EXHAUSTED not in (result.get("summary") or "")
    finished = session._local_jobs[job_id]
    assert finished["worker_provenance"].get("empty_implement_recovery") is False
