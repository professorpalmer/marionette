from __future__ import annotations

import os

import pytest

from harness.api import codegraph_index as ci


class _SyncThread:
    def __init__(self, target=None, daemon=None, **_kw):
        self._target = target

    def start(self):
        self._target()


class _FakeThreading:
    Thread = _SyncThread


def _repo(tmp_path, name):
    repo = tmp_path / "repo"
    cg = repo / ".codegraph"
    cg.mkdir(parents=True)
    src = repo / name
    src.write_text("x = 1\n", encoding="utf-8")
    # Index built at t=1000, directory untouched since t=500, file edited at t=2000.
    os.utime(src, (2000, 2000))
    os.utime(cg, (1000, 1000))
    os.utime(repo, (500, 500))
    return str(repo)


@pytest.fixture
def refresh_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(ci, "threading", _FakeThreading)
    monkeypatch.setattr(ci, "reindex_codegraph_bg", lambda repo: calls.append(repo))
    monkeypatch.setattr(ci, "prewarm_codegraph_context", lambda repo: None)
    monkeypatch.setattr(ci, "auto_index_refused", lambda repo: False)
    monkeypatch.setattr(ci, "codegraph_status", "ready")
    monkeypatch.setattr(ci, "codegraph_stale_check_at", {})
    return calls


@pytest.mark.parametrize("name", ["game.lua", "mod.luau", "main.c", "main.cpp", "app.rb"])
def test_in_place_edit_of_indexed_language_is_stale(tmp_path, refresh_calls, name):
    repo = _repo(tmp_path, name)
    assert ci.codegraph_is_stale(repo) is True
    ci.maybe_refresh_codegraph(repo, force=True)
    assert refresh_calls == [repo]


def test_python_edit_still_stale(tmp_path, refresh_calls):
    repo = _repo(tmp_path, "main.py")
    ci.maybe_refresh_codegraph(repo, force=True)
    assert refresh_calls == [repo]


@pytest.mark.parametrize("name", ["README.md", "notes.txt", "data.json"])
def test_edit_of_non_source_file_is_not_stale(tmp_path, refresh_calls, name):
    repo = _repo(tmp_path, name)
    assert ci.codegraph_is_stale(repo) is False
    ci.maybe_refresh_codegraph(repo, force=True)
    assert refresh_calls == []
