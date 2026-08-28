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
    _empty_implement_recovery_eligible,
    _is_empty_diff_implement_failure,
    _worker_provenance_text,
    _worker_stopped_by_guard_or_budget,
)
from harness.edit_engines import run_native_edit
from harness.local_jobs import LocalJobsMixin
from harness.worker import ProviderWorker, WorkerResult, scope_goal_paths_to_worktree
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
    assert job["worker_provenance"]["managed_worktree_mode"] == "none"


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


def test_scope_goal_paths_to_worktree_rewrites_live_absolute(tmp_path):
    repo = tmp_path / "freeze"
    repo.mkdir()
    (repo / "app.js").write_text("x\n", encoding="utf-8")
    live = str(repo)
    abs_app = str(repo / "app.js")
    goal = f"Polish the mockup in {abs_app} and keep styles.css"
    scoped = scope_goal_paths_to_worktree(goal, live, "/tmp/managed-wt")
    assert abs_app not in scoped
    assert "app.js" in scoped
    assert "styles.css" in scoped


def test_empty_implement_recovery_skips_guard_exhausted_first_attempt(monkeypatch):
    """Guard/budget exhaustion must not launch a second full recovery attempt,
    but must still annotate empty_managed_implement_exhausted for soft-refuse."""
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)
    job_id = "job_guard_no_recover"
    session._register_local_job(
        job_id, goal="polish mockup", role="implement", engine="native",
    )

    empty = WorkerResult(
        ok=False,
        summary="no changes produced",
        patch="",
        files_changed=[],
        tokens_in=40,
        tokens_out=12,
        tokens_cached=0,
        engine="native",
        model="stub",
        managed_worktree_path="/tmp/managed-wt",
        managed_worktree_mode="managed",
        worktree_diff_empty=True,
        stopped_by_guard_or_budget=True,
    )
    calls: list[str] = []

    def fake_worker(objective, *_args, **_kwargs):
        calls.append(objective)
        return empty

    monkeypatch.setattr(
        "harness.worktree_seed._list_git_status_porcelain_paths",
        lambda _repo: ["app.js"],
    )

    with patch.object(session, "_run_edit_worker_bounded", side_effect=fake_worker):
        session._run_provider_worker_background(
            job_id, "polish the mockup", expects_diff=True,
        )

    assert len(calls) == 1
    assert "[recovery]" not in calls[0]
    item = session._swarm_results.get_nowait()
    result = item["result"]
    summary = result.get("summary") or ""
    assert EMPTY_MANAGED_IMPLEMENT_EXHAUSTED in summary
    assert "Do NOT call run_implement again" in summary
    finished = session._local_jobs[job_id]
    assert finished["worker_provenance"]["empty_implement_recovery"] is False
    assert finished["worker_provenance"]["empty_managed_implement_exhausted"] is True
    assert _worker_stopped_by_guard_or_budget(empty) is True
    assert _empty_implement_recovery_eligible(
        empty,
        expects_diff=True,
        live_dirty_before=["app.js"],
        cancelled=False,
    ) is False


def test_empty_implement_recovery_skips_agentic_engine_error():
    crashed = WorkerResult(
        ok=False,
        error="agentic_error",
        summary="Agentic engine error: swarm exited with incomplete tasks",
        worktree_diff_empty=True,
        patch="",
        managed_worktree_mode="managed",
    )
    assert _empty_implement_recovery_eligible(
        crashed,
        expects_diff=True,
        live_dirty_before=["report.ts"],
        cancelled=False,
    ) is False


def test_agentic_engine_error_keeps_unapplied_files_without_apply(monkeypatch):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)
    job_id = "job_agentic_crash"
    session._register_local_job(
        job_id, goal="harden scoring", role="implement", engine="agentic",
    )
    crashed = WorkerResult(
        ok=False,
        error="agentic_error",
        summary=(
            "Agentic engine error: swarm exited with incomplete tasks\n"
            "require_diff: no PATCH artifact\n"
            "unapplied worktree files: src/lib/scoring/report.ts"
        ),
        patch="diff --git a/src/lib/scoring/report.ts b/src/lib/scoring/report.ts\n",
        files_changed=["src/lib/scoring/report.ts"],
        tokens_in=40,
        tokens_out=12,
        engine="agentic",
        model="agentic/openai-codex/gpt-5.6-luna",
        managed_worktree_path="/tmp/managed-wt",
        managed_worktree_mode="managed",
        worktree_diff_empty=False,
    )
    applied = []

    def fake_apply(*_a, **_k):
        applied.append("ran")
        return True, ["src/lib/scoring/report.ts"], "ok"

    session._apply_worker_patch = fake_apply
    monkeypatch.setattr(
        "harness.worktree_seed._list_git_status_porcelain_paths",
        lambda _repo: [],
    )
    with patch.object(session, "_run_edit_worker_bounded", return_value=crashed):
        session._run_provider_worker_background(
            job_id, "harden scoring", expects_diff=True,
        )
    item = session._swarm_results.get_nowait()
    result = item["result"]
    assert result["error"] == "agentic_error"
    assert result["applied"] is False
    assert result["has_patch_art"] is False
    assert result["files"] == ["src/lib/scoring/report.ts"]
    assert "require_diff" in (result["summary"] or "")
    assert "managed" in (result["summary"] or "")
    assert applied == []
    finished = session._local_jobs[job_id]
    assert finished["status"] == "failed"
    assert finished["worker_provenance"]["managed_worktree_mode"] == "managed"
    assert finished["worker_provenance"]["worktree_diff_empty"] is False


def test_empty_implement_recovery_shares_lifecycle_budget(monkeypatch):
    """Primary + recovery share one ambient lifecycle budget (no double ceiling)."""
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)
    job_id = "job_shared_budget"
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
        summary="patched app.js",
        patch=(
            "diff --git a/app.js b/app.js\n"
            "--- a/app.js\n"
            "+++ b/app.js\n"
            "@@ -1 +1,2 @@\n"
            " console.log(1);\n"
            "+console.log(2);\n"
        ),
        files_changed=["app.js"],
        tokens_in=20,
        tokens_out=8,
        tokens_cached=0,
        engine="native",
        model="stub",
        managed_worktree_path="/tmp/managed-wt",
        managed_worktree_mode="managed",
        worktree_diff_empty=False,
    )
    budgets_seen = []
    calls: list[str] = []

    def fake_worker(objective, *_args, **kwargs):
        calls.append(objective)
        budgets_seen.append(kwargs.get("lifecycle_budget"))
        if len(calls) == 1:
            # Simulate first-attempt spend on the shared lifecycle budget.
            budget = kwargs.get("lifecycle_budget")
            if budget is not None:
                budget.add_tokens(14)
            return empty
        return recovered

    monkeypatch.setattr(
        "harness.worktree_seed._list_git_status_porcelain_paths",
        lambda _repo: ["app.js"],
    )
    session._apply_worker_patch = lambda *_a, **_k: (True, ["app.js"], "ok")

    with patch.object(session, "_run_edit_worker_bounded", side_effect=fake_worker):
        session._run_provider_worker_background(
            job_id, "polish the mockup", expects_diff=True,
        )

    # One visible implement job, two worker attempts, shared lifecycle object.
    assert len(calls) == 2
    assert "[recovery]" in calls[1]
    assert budgets_seen[0] is not None
    assert budgets_seen[0] is budgets_seen[1]
    assert budgets_seen[0].tokens_used == 14
    item = session._swarm_results.get_nowait()
    result = item["result"]
    assert result["applied"] is True
    # Merged one-visible-step / two-attempt token accounting.
    assert result["tokens_in"] == 30
    assert result["tokens_out"] == 12
    finished = session._local_jobs[job_id]
    assert finished["worker_provenance"]["empty_implement_recovery"] is True
    assert finished["worker_provenance"]["empty_managed_implement_exhausted"] is False


def test_shared_ambient_budget_stamps_per_attempt_token_delta():
    """Two workers under one ambient budget each report only their own spend."""
    from harness.autobudget import AutoBudget
    from harness.worker import ambient_budget

    parent = AutoBudget(max_tokens=10_000).start()

    def run_attempt(goal: str, tokens_in: int, total_spend: int) -> WorkerResult:
        worker = ProviderWorker("/tmp/repo", goal)

        def _run_impl() -> WorkerResult:
            worker.budget.add_tokens(total_spend)
            worker._session_tokens_in = tokens_in
            return WorkerResult(ok=True, summary="done")

        worker._run_impl = _run_impl
        return worker.run()

    with ambient_budget(parent):
        first = run_attempt("first", tokens_in=10, total_spend=30)
        second = run_attempt("second", tokens_in=10, total_spend=30)

    assert first.tokens_in == 10
    assert first.tokens_out == 20
    assert second.tokens_in == 10
    assert second.tokens_out == 20
    assert parent.tokens_used == 60


def test_child_budget_start_preserves_shared_ceiling():
    from harness.autobudget import AutoBudget

    parent = AutoBudget(max_tokens=100).start()
    parent.add_tokens(40)
    child = parent.child()
    assert child.tokens_used == 40
    child.start()  # must not reset shared position
    assert child.tokens_used == 40
    assert parent.tokens_used == 40
    child.add_tokens(70)
    assert parent.tokens_used == 110
    assert child.check() is not None
    assert parent.check() is not None


def test_lifecycle_halted_skips_recovery_but_marks_exhausted(monkeypatch):
    """Recovery-eligible empty implement with a halted lifecycle must not relaunch,
    but must still annotate empty_managed_implement_exhausted for soft-refuse."""
    from harness.autobudget import AutoBudget

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)
    job_id = "job_lifecycle_halted_exhausted"
    session._register_local_job(
        job_id, goal="polish mockup", role="implement", engine="native",
    )
    # Token ceiling already spent: recovery would be eligible on dirty overlap,
    # but check() is halted so we must not burn a second worker call.
    lifecycle = AutoBudget(max_tokens=10, max_seconds=900).start()
    lifecycle.add_tokens(10)
    assert lifecycle.check() is not None
    session._auto_budget = lifecycle

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
    calls: list[str] = []

    def fake_worker(objective, *_args, **_kwargs):
        calls.append(objective)
        return empty

    monkeypatch.setattr(
        "harness.worktree_seed._list_git_status_porcelain_paths",
        lambda _repo: ["app.js", "styles.css"],
    )

    with patch.object(session, "_run_edit_worker_bounded", side_effect=fake_worker):
        session._run_provider_worker_background(
            job_id, "polish the mockup", expects_diff=True,
        )

    assert len(calls) == 1
    assert "[recovery]" not in calls[0]
    item = session._swarm_results.get_nowait()
    result = item["result"]
    summary = result.get("summary") or ""
    assert EMPTY_MANAGED_IMPLEMENT_EXHAUSTED in summary
    assert "Do NOT call run_implement again" in summary
    finished = session._local_jobs[job_id]
    assert finished["worker_provenance"]["empty_implement_recovery"] is False
    assert finished["worker_provenance"]["empty_managed_implement_exhausted"] is True


def test_implement_provenance_never_renders_mode_unknown():
    text = _worker_provenance_text({
        "requested_mode": "implement",
        "worktree_diff_empty": None,
        "error": "patch_capture_failed",
        "live_dirty_paths_before": [],
        "live_dirty_paths_after": [],
    })
    assert "mode=unknown" not in text
    assert "unavailable" not in text.lower()
    assert "patch_capture_failed" in text
    assert "could not be determined" in text


def test_cleanup_failed_provenance_is_secondary():
    text = _worker_provenance_text({
        "requested_mode": "implement",
        "managed_worktree_mode": "managed",
        "worktree_diff_empty": False,
        "error": "agentic_orchestrator_failed",
        "cleanup_status": "failed",
        "cleanup_stage": "store",
        "cleanup_error": "store: rmtree denied",
        "live_dirty_paths_before": [],
        "live_dirty_paths_after": [],
    })
    assert text.startswith("[provenance] agentic_orchestrator_failed:")
    assert "Cleanup failed (store)" in text
    assert "rmtree denied" in text


def test_undetermined_diff_not_empty_recovery_or_unavailable_unknown():
    res = WorkerResult(
        ok=False,
        error="patch_capture_failed",
        worktree_diff_empty=None,
        patch="",
        requested_mode="implement",
        managed_worktree_mode="managed",
    )
    assert _is_empty_diff_implement_failure(res, expects_diff=True) is False
    assert _empty_implement_recovery_eligible(
        res,
        expects_diff=True,
        live_dirty_before=["report.ts"],
        cancelled=False,
    ) is False
    text = _worker_provenance_text({
        "requested_mode": "implement",
        "managed_worktree_mode": "managed",
        "worktree_diff_empty": None,
        "error": "patch_capture_failed",
        "live_dirty_paths_before": [],
        "live_dirty_paths_after": [],
    })
    assert "unavailable" not in text.lower()
    assert "mode=unknown" not in text


def test_usage_known_false_does_not_claim_measured_zero():
    text = _worker_provenance_text({
        "requested_mode": "implement",
        "managed_worktree_mode": "managed",
        "worktree_diff_empty": None,
        "error": "agentic_orchestrator_failed",
        "usage_known": False,
        "live_dirty_paths_before": [],
        "live_dirty_paths_after": [],
    })
    assert "$0" not in text
    assert "0.00" not in text
    assert "measured $0" not in text.lower()


def test_agentic_orchestrator_failed_skips_empty_implement_recovery():
    crashed = WorkerResult(
        ok=False,
        error="agentic_orchestrator_failed",
        summary="Agentic engine error: swarm exited with incomplete tasks",
        worktree_diff_empty=True,
        patch="",
        managed_worktree_mode="managed",
    )
    assert _empty_implement_recovery_eligible(
        crashed,
        expects_diff=True,
        live_dirty_before=["report.ts"],
        cancelled=False,
    ) is False


def test_stage_codes_skip_empty_implement_recovery():
    for err in (
        "worktree_create_failed",
        "patch_capture_failed",
        "worker_cleanup_failed",
    ):
        res = WorkerResult(
            ok=False,
            error=err,
            worktree_diff_empty=True,
            patch="",
            managed_worktree_mode="none" if err == "worktree_create_failed" else "managed",
        )
        assert _empty_implement_recovery_eligible(
            res,
            expects_diff=True,
            live_dirty_before=["report.ts"],
            cancelled=False,
        ) is False


def test_failure_stamp_fields_survive_in_provenance_text():
    text = _worker_provenance_text({
        "requested_mode": "implement",
        "managed_worktree_mode": "managed",
        "worktree_diff_empty": True,
        "error": "agentic_orchestrator_failed",
        "engine": "agentic",
        "adapter": "agentic",
        "model": "agentic/openrouter/foo",
        "live_dirty_paths_before": [],
        "live_dirty_paths_after": [],
    })
    assert text.startswith("[provenance] agentic_orchestrator_failed:")
    assert "mode=managed" in text
