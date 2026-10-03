"""Gitignored scratch named in a goal must not block worker worktrees.

Before: a goal mentioning a gitignored directory (e.g. repos cloned under
tmp/) seeded every file beneath it, then commit_seed_baseline ran
``git add -- <path>`` per file with check=True. git refuses ignored paths,
so one ignored file failed the seed baseline, the worktree, and every job in
the dispatch.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from harness.edit_engines import finalize_worktree_patch
from harness.worktree_seed import commit_seed_baseline, seed_worktree_from_goal
from harness.worktrees import add_worktree, remove_worktree


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_ignored_scratch_is_readable_but_not_baselined(tmp_path):
    repo = tmp_path / "kit"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text("tmp/\n", encoding="utf-8")
    (repo / "tracked.py").write_text("print(0)\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "tracked.py")
    _git(repo, "commit", "-m", "init")
    clone = repo / "tmp" / "links-haul" / "repos" / "Zurp"
    clone.mkdir(parents=True)
    _git(clone, "init", "-b", "main")  # embedded repo, like a real clone
    (clone / "CODE_OF_CONDUCT.md").write_text("be kind\n", encoding="utf-8")
    (clone / "README.md").write_text("zurp\n", encoding="utf-8")
    (repo / "notes.md").write_text("untracked live edit\n", encoding="utf-8")

    wt_path = add_worktree(str(repo), branch="seed-ignored", base="HEAD")["path"]
    try:
        seeded = seed_worktree_from_goal(
            str(repo), wt_path, "summarize the repos in tmp/links-haul/repos and notes.md")
        assert "tmp/links-haul/repos/Zurp/CODE_OF_CONDUCT.md" in seeded.paths
        committed = commit_seed_baseline(wt_path, seeded.paths)
        assert committed == 1  # notes.md; ignored scratch is not baselined
        wt = Path(wt_path)
        assert (wt / "tmp/links-haul/repos/Zurp/CODE_OF_CONDUCT.md").read_text() == "be kind\n"
        patch, files = finalize_worktree_patch(wt_path)
        assert patch.strip() == "" and files == []
        (wt / "tracked.py").write_text("print(1)\n", encoding="utf-8")
        _, files = finalize_worktree_patch(wt_path)
        assert files == ["tracked.py"]
    finally:
        remove_worktree(str(repo), wt_path, force=True)


def test_baseline_with_only_ignored_paths_commits_nothing(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "init")
    wt_path = add_worktree(str(repo), branch="seed-only-ignored", base="HEAD")["path"]
    try:
        (Path(wt_path) / "scratch").mkdir()
        (Path(wt_path) / "scratch" / "a.txt").write_text("x\n", encoding="utf-8")
        assert commit_seed_baseline(wt_path, ["scratch/a.txt"]) == 0
    finally:
        remove_worktree(str(repo), wt_path, force=True)


def test_glob_characters_in_seeded_names_stage_only_that_file(tmp_path):
    repo = tmp_path / "g"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "keep.txt").write_text("k\n", encoding="utf-8")
    _git(repo, "add", "keep.txt")
    _git(repo, "commit", "-m", "init")
    wt_path = add_worktree(str(repo), branch="seed-glob", base="HEAD")["path"]
    try:
        wt = Path(wt_path)
        (wt / "a[1].md").write_text("one\n", encoding="utf-8")
        (wt / "a1.md").write_text("glob would match me\n", encoding="utf-8")
        assert commit_seed_baseline(wt_path, ["a[1].md"]) == 1
    finally:
        remove_worktree(str(repo), wt_path, force=True)
