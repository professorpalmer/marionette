"""resolve_effective_repo: Marionette Home parent → single git child checkout."""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from harness.repo_resolve import clear_effective_repo_cache, resolve_effective_repo

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git not available",
)


def _git_init(repo: str) -> None:
    subprocess.run(
        ["git", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_effective_repo_cache()
    yield
    clear_effective_repo_cache()


def test_root_is_git_repo_returns_toplevel(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git_init(str(root))
    got = resolve_effective_repo(str(root))
    assert _norm(got) == _norm(str(root))


def test_exactly_one_git_child_returns_child(tmp_path):
    home = tmp_path / "marionette-home"
    home.mkdir()
    (home / "notes.txt").write_text("not a repo", encoding="utf-8")
    child = home / "marionette"
    child.mkdir()
    _git_init(str(child))
    got = resolve_effective_repo(str(home))
    assert _norm(got) == _norm(str(child))


def test_two_git_children_returns_root_unchanged(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    a = home / "a"
    b = home / "b"
    a.mkdir()
    b.mkdir()
    _git_init(str(a))
    _git_init(str(b))
    got = resolve_effective_repo(str(home))
    assert _norm(got) == _norm(str(home))


def test_non_git_root_no_git_children_unchanged(tmp_path):
    home = tmp_path / "empty-home"
    home.mkdir()
    (home / "subdir").mkdir()
    (home / "readme.md").write_text("hi", encoding="utf-8")
    got = resolve_effective_repo(str(home))
    assert _norm(got) == _norm(str(home))


def test_git_subdirectory_returns_toplevel(tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    _git_init(str(root))
    nested = root / "pkg" / "inner"
    nested.mkdir(parents=True)
    got = resolve_effective_repo(str(nested))
    assert _norm(got) == _norm(str(root))


def test_empty_root_unchanged():
    assert resolve_effective_repo("") == ""
    assert resolve_effective_repo("   ") == "   "


def test_result_is_cached(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    child = home / "only"
    child.mkdir()
    _git_init(str(child))
    first = resolve_effective_repo(str(home))
    assert _norm(first) == _norm(str(child))

    calls = {"n": 0}
    real_listdir = os.listdir

    def counting_listdir(path):
        calls["n"] += 1
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", counting_listdir)
    second = resolve_effective_repo(str(home))
    assert _norm(second) == _norm(first)
    assert calls["n"] == 0
