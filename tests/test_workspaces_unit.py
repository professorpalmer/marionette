"""Direct unit tests for harness.workspaces (list / switch / create guards).

API peel tests mock this module; these exercise the real git-backed helpers.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from harness.workspaces import (
    create_workspace,
    list_workspaces,
    switch_workspace,
)


def _git(repo: str | Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(tmp_path: Path, *, branch: str = "main") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "t@example.com")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "initial")
    return repo


def test_list_workspaces_empty_without_repo():
    assert list_workspaces("") == []
    assert list_workspaces("/no/such/path") == []


def test_list_workspaces_empty_on_unborn_head(tmp_path):
    repo = tmp_path / "unborn"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    assert list_workspaces(str(repo)) == []


def test_list_workspaces_marks_active_and_dirty(tmp_path):
    repo = _init_repo(tmp_path)
    _git(repo, "branch", "feature")

    rows = list_workspaces(str(repo))
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) >= {"main", "feature"}
    assert by_name["main"]["active"] is True
    assert by_name["main"]["dirty"] is False
    assert by_name["feature"]["active"] is False
    assert by_name["feature"]["dirty"] is False

    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    dirty_rows = list_workspaces(str(repo))
    dirty_by_name = {r["name"]: r for r in dirty_rows}
    assert dirty_by_name["main"]["active"] is True
    assert dirty_by_name["main"]["dirty"] is True
    assert dirty_by_name["feature"]["dirty"] is False


def test_switch_workspace_refuses_dirty(tmp_path):
    repo = _init_repo(tmp_path)
    _git(repo, "branch", "feature")
    (repo / "README.md").write_text("uncommitted\n", encoding="utf-8")

    result = switch_workspace(str(repo), "feature")
    assert result["ok"] is False
    assert result.get("dirty") is True
    assert "uncommitted changes" in result["error"]
    assert "allow_dirty" in result["error"]

    # Still on main
    active = [r for r in list_workspaces(str(repo)) if r["active"]]
    assert active and active[0]["name"] == "main"


def test_switch_workspace_allow_dirty_overrides_guard(tmp_path):
    repo = _init_repo(tmp_path)
    _git(repo, "branch", "feature")
    (repo / "README.md").write_text("uncommitted\n", encoding="utf-8")

    result = switch_workspace(str(repo), "feature", allow_dirty=True)
    # Git may still refuse if checkout would clobber; when content is identical
    # across branches except working tree edits to a shared file, checkout can
    # succeed and carry the dirty state. Either ok or a git error is fine —
    # the module must not return the dirty-refuse payload.
    if result.get("ok"):
        assert result["active"] == "feature"
    else:
        assert result.get("dirty") is not True
        assert "uncommitted changes" not in result.get("error", "")


def test_switch_workspace_clean_success(tmp_path):
    repo = _init_repo(tmp_path)
    _git(repo, "branch", "feature")

    result = switch_workspace(str(repo), "feature")
    assert result == {"ok": True, "active": "feature"}
    active = [r for r in list_workspaces(str(repo)) if r["active"]]
    assert active[0]["name"] == "feature"


def test_switch_workspace_invalid_name_leading_dash(tmp_path):
    repo = _init_repo(tmp_path)
    result = switch_workspace(str(repo), "-evil")
    assert result["ok"] is False
    assert "invalid workspace name" in result["error"]
    assert "cannot start with '-'" in result["error"]


def test_switch_workspace_no_repo(tmp_path):
    result = switch_workspace(str(tmp_path / "missing"), "main")
    assert result == {"ok": False, "error": "no git repo configured"}


def test_create_workspace_success(tmp_path):
    repo = _init_repo(tmp_path)
    result = create_workspace(str(repo), "ws-a")
    assert result == {"ok": True, "active": "ws-a"}
    rows = list_workspaces(str(repo))
    by_name = {r["name"]: r for r in rows}
    assert "ws-a" in by_name
    assert by_name["ws-a"]["active"] is True


def test_create_workspace_from_base(tmp_path):
    repo = _init_repo(tmp_path)
    _git(repo, "branch", "base-line")
    result = create_workspace(str(repo), "from-base", base="base-line")
    assert result == {"ok": True, "active": "from-base"}


def test_create_workspace_invalid_name_leading_dash(tmp_path):
    repo = _init_repo(tmp_path)
    result = create_workspace(str(repo), "-bad")
    assert result["ok"] is False
    assert "invalid workspace name/base" in result["error"]
    assert "cannot start with '-'" in result["error"]


def test_create_workspace_invalid_base_leading_dash(tmp_path):
    repo = _init_repo(tmp_path)
    result = create_workspace(str(repo), "ok-name", base="-badbase")
    assert result["ok"] is False
    assert "invalid workspace name/base" in result["error"]


def test_create_workspace_no_repo(tmp_path):
    result = create_workspace(str(tmp_path / "missing"), "ws")
    assert result == {"ok": False, "error": "no git repo configured"}


def test_create_workspace_duplicate_branch_fails(tmp_path):
    repo = _init_repo(tmp_path)
    assert create_workspace(str(repo), "dup")["ok"] is True
    # Return to main so we can attempt another create with same name
    assert switch_workspace(str(repo), "main")["ok"] is True
    again = create_workspace(str(repo), "dup")
    assert again["ok"] is False
    assert again.get("error")


def test_switch_workspace_soft_refuses_branch_locked_in_worktree(tmp_path):
    """pmedit/pmworker-style branches live in sibling worktrees — don't toast a git fatal."""
    repo = _init_repo(tmp_path)
    _git(repo, "branch", "pmedit-deadbeef")
    wt = tmp_path / "wt-pmedit"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(wt), "pmedit-deadbeef"],
        check=True,
        capture_output=True,
        text=True,
    )

    result = switch_workspace(str(repo), "pmedit-deadbeef")
    assert result["ok"] is False
    assert result.get("worktree_busy") is True
    assert result.get("worktree_path")
    assert "pmedit-deadbeef" in result["error"]
    assert "worktree" in result["error"].lower()
    # Still on main — no partial checkout.
    active = [r for r in list_workspaces(str(repo)) if r["active"]]
    assert active and active[0]["name"] == "main"

    listed = {r["name"]: r for r in list_workspaces(str(repo))}
    assert listed["pmedit-deadbeef"].get("worktree_path")
    assert "worktree_path" not in listed["main"]



def test_list_workspaces_hides_stale_local_release_keeps_live(tmp_path):
    """BRANCHES hides leftover local release/v0.9.* once origin deleted them.

    Keep main, dev, the current checkout, and origin-backed release heads.
    Hide leftover release worktrees (318); do not delete the directory.
    """
    repo = _init_repo(tmp_path, branch="main")
    _git(repo, "branch", "dev")
    _git(repo, "branch", "release/v0.9.286")
    _git(repo, "branch", "release/v0.9.318")
    _git(repo, "branch", "release/v0.9.348")
    _git(repo, "branch", "feature")

    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main", "dev", "release/v0.9.348")

    wt = tmp_path / "wt-318"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(wt), "release/v0.9.318"],
        check=True,
        capture_output=True,
        text=True,
    )

    names = {r["name"] for r in list_workspaces(str(repo))}
    assert "main" in names
    assert "dev" in names
    assert "feature" in names
    assert "release/v0.9.348" in names  # still on origin
    assert "release/v0.9.318" not in names  # leftover worktree hidden, dir stays
    assert "release/v0.9.286" not in names  # stale local-only leftover
    assert Path(wt).is_dir()  # do not delete the worktree directory


def test_list_workspaces_keeps_current_release_checkout_even_if_local_only(tmp_path):
    repo = _init_repo(tmp_path, branch="main")
    _git(repo, "branch", "release/v0.9.331")
    _git(repo, "checkout", "-q", "release/v0.9.331")
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main")

    names = {r["name"] for r in list_workspaces(str(repo))}
    assert "release/v0.9.331" in names
    active = [r for r in list_workspaces(str(repo)) if r["active"]]
    assert active and active[0]["name"] == "release/v0.9.331"


def test_switch_workspace_ignores_untracked_noise(tmp_path):
    """Untracked / ignored files must not trip the dirty stash block."""
    repo = _init_repo(tmp_path)
    _git(repo, "branch", "feature")
    (repo / "scratch.local").write_text("noise\n", encoding="utf-8")
    (repo / ".gitignore").write_text("scratch.local\nignored.tmp\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore scratch")
    (repo / "ignored.tmp").write_text("also noise\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    result = switch_workspace(str(repo), "feature")
    assert result == {"ok": True, "active": "feature"}


def test_switch_workspace_reports_tracked_dirty_paths(tmp_path):
    repo = _init_repo(tmp_path)
    _git(repo, "branch", "feature")
    (repo / "README.md").write_text("tracked dirty\n", encoding="utf-8")

    result = switch_workspace(str(repo), "feature")
    assert result["ok"] is False
    assert result.get("dirty") is True
    assert "README.md" in (result.get("dirty_paths") or [])
    assert "README.md" in result["error"]


def test_list_workspaces_hides_stale_release_even_without_origin_picture(tmp_path):
    """Empty remote picture must not resurrect leftover release/v0.9.* on BRANCHES."""
    repo = _init_repo(tmp_path, branch="main")
    _git(repo, "branch", "dev")
    _git(repo, "branch", "release/v0.9.308")
    _git(repo, "branch", "feature")

    names = {r["name"] for r in list_workspaces(str(repo))}
    assert "main" in names
    assert "dev" in names
    assert "feature" in names
    assert "release/v0.9.308" not in names


def test_list_workspaces_hides_gone_upstream_absorb_and_dest(tmp_path):
    """BRANCHES hides locals whose origin copy is gone; keep unpushed features."""
    repo = _init_repo(tmp_path, branch="main")
    _git(repo, "branch", "dev")
    _git(repo, "branch", "dest")
    _git(repo, "branch", "absorb/marionette-223-scope-labels")
    _git(repo, "branch", "feat/pm-pin-1.22.37")
    _git(repo, "branch", "feat/keep-unpushed")

    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "main", "dev")
    _git(repo, "push", "-q", "-u", "origin", "absorb/marionette-223-scope-labels")
    _git(repo, "push", "-q", "-u", "origin", "feat/pm-pin-1.22.37")
    _git(repo, "push", "origin", "--delete", "absorb/marionette-223-scope-labels")
    _git(repo, "push", "origin", "--delete", "feat/pm-pin-1.22.37")
    _git(repo, "fetch", "--prune", "origin")

    names = {r["name"] for r in list_workspaces(str(repo))}
    assert "main" in names
    assert "dev" in names
    assert "feat/keep-unpushed" in names
    assert "dest" not in names
    assert "absorb/marionette-223-scope-labels" not in names
    assert "feat/pm-pin-1.22.37" not in names
