"""Git upstream lag brief for analysis workers."""
from __future__ import annotations

import subprocess

import pytest

from harness.git_upstream import git_upstream_brief, maybe_git_upstream_brief


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_brief_empty_for_nongit(tmp_path):
    assert git_upstream_brief(str(tmp_path)) == ""
    assert maybe_git_upstream_brief(str(tmp_path)) == ""


def test_brief_reports_behind_upstream(tmp_path):
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git unavailable")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "first")
    _git(repo, "branch", "-M", "main")
    # Simulate origin/main ahead without a real remote push.
    first = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "second tip")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "reset", "--hard", first)

    brief = git_upstream_brief(str(repo))
    assert "behind 1" in brief
    assert "IMPORTANT" in brief
    assert "origin/main" in brief
    assert "git show" in brief


def test_analysis_instruction_includes_lag(tmp_path, monkeypatch):
    from pmharness.bridge import _analysis_instruction

    monkeypatch.setattr(
        "harness.git_upstream.maybe_git_upstream_brief",
        lambda _repo: (
            "Git workspace state (authoritative for commit verification):\n"
            "- HEAD: abc123 (old)\n"
            "- upstream: origin/main @ def456 (new tip)\n"
            "- vs upstream: ahead 0, behind 3\n"
            "- IMPORTANT: local HEAD trails origin/main by 3 commit(s)."
        ),
    )
    inst = _analysis_instruction("verify the OOM fix", str(tmp_path), "explore")
    assert "behind 3" in inst
    assert "trails origin/main" in inst
