from __future__ import annotations

"""Marionette git calls never start programs that the repo's own config names.

A repo's .git/config can name programs that git starts by itself:
core.fsmonitor on each index refresh, diff.external and textconv drivers on
a content diff. Checkpoints and diff views run git in the user's repo on
every turn, so these programs would run without the user asking. A
configured external diff also replaces the patch text with its own output.
"""

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX shell programs")


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def trap_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    log = tmp_path / "ran.log"
    log.write_text("")
    programs = {}
    for name in ("fsmonitor", "external", "textconv"):
        script = tmp_path / f"{name}.sh"
        tail = 'cat "$1"\n' if name == "textconv" else "exit 1\n" if name == "fsmonitor" else ""
        script.write_text(f"#!/bin/sh\necho {name} >> {log}\n{tail}")
        script.chmod(0o755)
        programs[name] = str(script)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("a\n")
    (repo / "g.bin").write_text("x\n")
    (repo / ".gitattributes").write_text("g.bin diff=trap\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "config", "core.fsmonitor", programs["fsmonitor"])
    _git(repo, "config", "diff.external", programs["external"])
    _git(repo, "config", "diff.trap.textconv", programs["textconv"])
    return repo, log


def _ran(log: Path):
    return [line for line in log.read_text().splitlines() if line]


def test_checkpoint_snapshot_and_diff(trap_repo):
    from harness.checkpoints import CheckpointStore

    repo, log = trap_repo
    store = CheckpointStore(str(repo), session_id="s1")
    cid = store.snapshot("probe", "turn")
    assert cid
    (repo / "f.txt").write_text("a\nb\n")
    (repo / "g.bin").write_text("y\n")
    result = store.diff(cid)
    assert result["ok"]
    assert "+b" in result["diff"]
    assert _ran(log) == []


def test_hunk_tracker_diff(trap_repo):
    from harness.checkpoints import CheckpointStore

    repo, log = trap_repo
    tracker = CheckpointStore(str(repo), session_id="s1").hunk_tracker()
    assert tracker.ensure_baseline()
    (repo / "f.txt").write_text("a\nb\n")
    (repo / "g.bin").write_text("y\n")
    result = tracker.recompute()
    assert result.get("ok", True)
    assert _ran(log) == []


def test_workspace_diff_view(trap_repo):
    from harness.git_workspace import workspace_diff

    repo, log = trap_repo
    (repo / "f.txt").write_text("a\nb\n")
    (repo / "g.bin").write_text("y\n")
    out = workspace_diff(str(repo), str(repo))
    assert out["ok"] and "+b" in out["out"]
    assert _ran(log) == []


def test_worker_patch(trap_repo):
    from harness.edit_engines import finalize_worktree_patch

    repo, log = trap_repo
    (repo / "f.txt").write_text("a\nb\n")
    patch, files = finalize_worktree_patch(str(repo))
    assert "+b" in patch and "f.txt" in files
    assert "external" not in _ran(log) and "textconv" not in _ran(log)
