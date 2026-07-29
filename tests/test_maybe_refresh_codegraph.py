"""Focused coverage for maybe_refresh_codegraph debounce / force / fail-until."""
from __future__ import annotations

import os
import threading
import time

import harness.api.codegraph_index as cgi


# Fixed epochs — far enough apart for 2-second filesystem mtime rounding.
_SOURCE_T = 1_700_000_000.0
_INDEX_T = 1_700_000_100.0
_NEWER_T = 1_700_000_200.0


def _set_mtime(path, when):
    os.utime(os.fspath(path), (when, when))


def _mk_stale_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("print('a')\n")
    (repo / ".codegraph").mkdir()
    (repo / ".codegraph" / "db").write_text("index")
    for path in (repo, repo / "src", repo / "src" / "a.py"):
        _set_mtime(path, _SOURCE_T)
    _set_mtime(repo / ".codegraph", _INDEX_T)
    _set_mtime(repo / ".codegraph" / "db", _INDEX_T)
    (repo / "src" / "a.py").write_text("print('edited')\n")
    _set_mtime(repo / "src" / "a.py", _NEWER_T)
    return str(repo)


class _ImmediateThread:
    """Run thread targets inline so refresh workers are deterministic in tests."""

    def __init__(self, target=None, daemon=None, args=(), kwargs=None, **_kw):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)


def _reset_refresh_state():
    cgi.codegraph_stale_check_at.clear()
    cgi.codegraph_fail_until.clear()
    cgi.codegraph_status = "ready"
    cgi.codegraph_status_reason = None


def test_debounce_skips_second_check_within_window(tmp_path, monkeypatch):
    repo = _mk_stale_repo(tmp_path)
    _reset_refresh_state()
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    reindex_calls = []
    monkeypatch.setattr(cgi, "reindex_codegraph_bg", lambda path: reindex_calls.append(path))

    cgi.maybe_refresh_codegraph(repo)
    cgi.maybe_refresh_codegraph(repo)

    assert reindex_calls == [repo]


def test_force_bypasses_debounce(tmp_path, monkeypatch):
    repo = _mk_stale_repo(tmp_path)
    _reset_refresh_state()
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    reindex_calls = []
    monkeypatch.setattr(cgi, "reindex_codegraph_bg", lambda path: reindex_calls.append(path))

    cgi.maybe_refresh_codegraph(repo)
    cgi.maybe_refresh_codegraph(repo, force=True)

    assert reindex_calls == [repo, repo]


def test_stale_repo_triggers_reindex(tmp_path, monkeypatch):
    repo = _mk_stale_repo(tmp_path)
    _reset_refresh_state()
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    reindex_calls = []
    monkeypatch.setattr(cgi, "reindex_codegraph_bg", lambda path: reindex_calls.append(path))

    cgi.maybe_refresh_codegraph(repo, force=True)

    assert reindex_calls == [repo]
    assert cgi.codegraph_status_reason == "files changed -- refreshing index"


def test_fresh_repo_does_not_reindex(tmp_path, monkeypatch):
    repo_path = tmp_path / "fresh"
    (repo_path / "src").mkdir(parents=True)
    (repo_path / "src" / "a.py").write_text("print('a')\n")
    (repo_path / ".codegraph").mkdir()
    (repo_path / ".codegraph" / "db").write_text("index")
    for path in (repo_path, repo_path / "src", repo_path / "src" / "a.py"):
        _set_mtime(path, _SOURCE_T)
    _set_mtime(repo_path / ".codegraph", _INDEX_T)
    _set_mtime(repo_path / ".codegraph" / "db", _INDEX_T)
    repo = str(repo_path)

    _reset_refresh_state()
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    reindex_calls = []
    monkeypatch.setattr(cgi, "reindex_codegraph_bg", lambda path: reindex_calls.append(path))

    cgi.maybe_refresh_codegraph(repo, force=True)
    assert reindex_calls == []


def test_fail_until_suppresses_auto_refresh(tmp_path, monkeypatch):
    repo = _mk_stale_repo(tmp_path)
    _reset_refresh_state()
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    reindex_calls = []
    monkeypatch.setattr(cgi, "reindex_codegraph_bg", lambda path: reindex_calls.append(path))
    cgi.codegraph_fail_until[repo] = time.monotonic() + 120.0

    cgi.maybe_refresh_codegraph(repo)

    assert reindex_calls == []
    assert repo not in cgi.codegraph_stale_check_at


def test_force_bypasses_fail_until(tmp_path, monkeypatch):
    repo = _mk_stale_repo(tmp_path)
    _reset_refresh_state()
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    reindex_calls = []
    monkeypatch.setattr(cgi, "reindex_codegraph_bg", lambda path: reindex_calls.append(path))
    cgi.codegraph_fail_until[repo] = time.monotonic() + 120.0

    cgi.maybe_refresh_codegraph(repo, force=True)

    assert reindex_calls == [repo]


def test_empty_repo_path_is_noop(monkeypatch):
    _reset_refresh_state()
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    reindex_calls = []
    monkeypatch.setattr(cgi, "reindex_codegraph_bg", lambda path: reindex_calls.append(path))

    cgi.maybe_refresh_codegraph("")
    cgi.maybe_refresh_codegraph(None)  # type: ignore[arg-type]

    assert reindex_calls == []


def test_indexing_status_skips_reindex(tmp_path, monkeypatch):
    repo = _mk_stale_repo(tmp_path)
    _reset_refresh_state()
    cgi.codegraph_status = "indexing"
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    reindex_calls = []
    monkeypatch.setattr(cgi, "reindex_codegraph_bg", lambda path: reindex_calls.append(path))

    cgi.maybe_refresh_codegraph(repo, force=True)

    assert reindex_calls == []
