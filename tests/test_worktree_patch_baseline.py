"""Real Git regressions for patches spanning worker commits and live edits."""
import subprocess
from pathlib import Path

import pytest

from harness.edit_engines import finalize_worktree_patch, managed_worktree_for_goal


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "commit.gpgsign", "false")
    (path / "test.txt").write_text("hello\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    return path


def test_committed_rename_is_captured(repo):
    with managed_worktree_for_goal(str(repo), "Change test.txt") as wt:
        baseline = git(wt, "rev-parse", "HEAD")
        git(wt, "mv", "test.txt", "renamed.txt")
        git(wt, "commit", "-m", "rename")
        patch, files = finalize_worktree_patch(wt, baseline)
        assert "renamed.txt" in files
        assert "rename from test.txt" in patch
        assert "rename to renamed.txt" in patch


def test_mixed_worker_edits_exclude_seed_and_committed_artifacts(repo):
    (repo / "test.txt").write_text("parent dirty\n")
    (repo / "seed.txt").write_text("parent untracked\n")
    with managed_worktree_for_goal(str(repo), "Change test.txt and seed.txt") as wt:
        baseline = git(wt, "rev-parse", "HEAD")
        path = Path(wt)
        (path / "test.txt").write_text("parent dirty\ncommitted\n")
        (path / "pkg" / "__pycache__").mkdir(parents=True)
        (path / "pkg" / "__pycache__" / "junk.pyc").write_text("cache")
        (path / ".pytest_cache").mkdir()
        (path / ".pytest_cache" / "README.md").write_text("cache")
        git(wt, "add", ".")
        git(wt, "commit", "-m", "worker")
        (path / "test.txt").write_text("parent dirty\ncommitted\nuncommitted\n")
        (path / "new.txt").write_text("new\n")
        patch, files = finalize_worktree_patch(wt, baseline)
        assert set(files) == {"test.txt", "new.txt"}
        assert "+committed" in patch and "+uncommitted" in patch
        assert "+parent dirty" not in patch
        assert "seed.txt" not in patch and "cache" not in patch


def test_seeded_noop_is_empty(repo):
    (repo / "test.txt").write_text("parent dirty\n")
    (repo / "seed.txt").write_text("parent untracked\n")
    with managed_worktree_for_goal(str(repo), "Change test.txt and seed.txt") as wt:
        baseline = git(wt, "rev-parse", "HEAD")
        assert finalize_worktree_patch(wt, baseline) == ("", [])


def test_seed_commit_failure_prevents_worker_entry(repo):
    (repo / "test.txt").write_text("parent dirty\n")
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", str(repo / "missing-gpg"))
    entered = False
    with pytest.raises(RuntimeError, match="seed"):
        with managed_worktree_for_goal(str(repo), "Change test.txt"):
            entered = True
    assert not entered


@pytest.mark.parametrize("engine", ["agentic", "cursor", "native"])
@pytest.mark.parametrize("seed_fails", [False, True])
def test_engine_captures_commits_or_stops_before_worker(repo, monkeypatch, engine, seed_fails):
    from unittest.mock import MagicMock

    from harness import edit_engines
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession, ConvEvent
    from harness.worker import ProviderWorker

    (repo / "test.txt").write_text("parent dirty\n")
    if seed_fails:
        git(repo, "config", "commit.gpgsign", "true")
        git(repo, "config", "gpg.program", str(repo / "missing-gpg"))
    entered = []

    def execute(wt):
        entered.append(wt)
        assert Path(wt, "test.txt").read_text() == "parent dirty\n"
        git(wt, "mv", "test.txt", "renamed.txt")
        git(wt, "commit", "-m", "worker rename")
        Path(wt, "new.txt").write_text("uncommitted\n")

    def run_orchestrator(self, goal, specs, **kwargs):
        execute(specs[0].payload["cwd"])
        result = MagicMock()
        result.artifacts = []
        return result

    def run_native(self, objective, **kwargs):
        execute(self.config.repo)
        yield ConvEvent("auto_halt", {"reason": "pilot reports objective met"})

    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator.run", run_orchestrator)
    monkeypatch.setattr(ConversationalSession, "run_auto", run_native)
    monkeypatch.setattr(edit_engines, "agentic_available", lambda: True)
    monkeypatch.setattr(edit_engines, "cursor_platform_available", lambda: True)
    if engine == "native":
        result = ProviderWorker(repo=str(repo), goal="Change test.txt").run()
    else:
        cfg = HarnessConfig()
        cfg.repo = str(repo)
        runner = edit_engines.run_agentic_edit if engine == "agentic" else edit_engines.run_cursor_edit
        result = runner(cfg, "Change test.txt")
    if seed_fails:
        assert not entered
        assert not result.ok
        assert "seed" in result.error + result.summary
        assert not result.patch
    else:
        assert len(entered) == 1
        assert result.ok, result.error + result.summary
        assert set(result.files_changed) == {"renamed.txt", "new.txt"}
        assert "rename from test.txt" in result.patch
        assert "+uncommitted" in result.patch
        assert "+parent dirty" not in result.patch
    assert (repo / "test.txt").read_text() == "parent dirty\n"
