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
