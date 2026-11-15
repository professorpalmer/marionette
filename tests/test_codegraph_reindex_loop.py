"""A finished CodeGraph index must not start again until files change, and
Marionette Home or the home folder must never index on their own.

Before the fix the indexer rewrote ``.codegraph/codegraph.db`` in place, so
``.codegraph`` kept the mtime of its creation. After one later edit, each
staleness check (session switch, turn, workspace open) found the tree stale
and started a full index again. One live log had 568 starts for one repo.
"""
from __future__ import annotations

import os
import sys
import time

import harness.api.codegraph_index as cgi

_OLD_INDEX_T = 1_700_000_000.0
_EDIT_T = 1_700_000_200.0
_REINDEX_T = 1_700_000_400.0
_LATER_EDIT_T = 1_700_000_600.0


def _set_mtime(path, when):
    os.utime(os.fspath(path), (when, when))


def _indexed_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("print('a')\n")
    (repo / ".codegraph").mkdir()
    (repo / ".codegraph" / "codegraph.db").write_text("db")
    for path in (repo, repo / "src", repo / "src" / "a.py"):
        _set_mtime(path, _OLD_INDEX_T - 100)
    _set_mtime(repo / ".codegraph", _OLD_INDEX_T)
    return repo


def _bind(monkeypatch, tmp_path, repo, *, cmd=None, home=None):
    calls = []

    def _cmd(*args):
        calls.append(args)
        return cmd or [sys.executable, "-c", "pass"]

    deps = cgi.CodegraphIndexDeps(
        puppetmaster_available=lambda: True,
        puppetmaster_cmd=_cmd,
        diag=lambda *a, **k: None,
        get_state_dir=lambda: str(tmp_path),
        get_repo=lambda: str(repo),
        is_home_workspace=lambda path: bool(home) and os.path.realpath(path) == os.path.realpath(home),
    )
    monkeypatch.setattr(cgi, "_deps", deps)
    monkeypatch.setattr(cgi, "prepare_codegraph_scope", lambda path: {"verdict": "ok"})
    monkeypatch.setattr(cgi, "prewarm_codegraph_context", lambda path: None)
    monkeypatch.setattr(cgi, "codegraph_index_log_path", lambda: str(tmp_path / "index.log"))
    monkeypatch.setattr(cgi, "codegraph_index_proc", None)
    monkeypatch.setattr(cgi, "codegraph_status", "none")
    monkeypatch.setattr(cgi, "codegraph_status_reason", None)
    monkeypatch.setattr(cgi, "codegraph_fail_until", {})
    monkeypatch.setattr(cgi, "codegraph_status_cache", {})
    monkeypatch.setattr(cgi, "codegraph_stale_check_at", {})
    return calls


def test_reindex_stamp_ends_the_stale_loop(tmp_path):
    repo = _indexed_repo(tmp_path)
    (repo / "src" / "a.py").write_text("print('edited')\n")
    _set_mtime(repo / "src" / "a.py", _EDIT_T)
    assert cgi.codegraph_is_stale(str(repo)) is True

    # The reindex rewrites codegraph.db in place: the old code saw the same
    # stale tree after it, forever. The start-time stamp makes it fresh.
    cgi._mark_index_fresh(str(repo), _REINDEX_T)
    assert cgi.codegraph_is_stale(str(repo)) is False

    (repo / "src" / "a.py").write_text("print('edited again')\n")
    _set_mtime(repo / "src" / "a.py", _LATER_EDIT_T)
    assert cgi.codegraph_is_stale(str(repo)) is True


def test_successful_index_stamps_its_start_time(tmp_path, monkeypatch):
    repo = _indexed_repo(tmp_path)
    (repo / "src" / "a.py").write_text("print('edited')\n")
    _set_mtime(repo / "src" / "a.py", _EDIT_T)
    _bind(monkeypatch, tmp_path, repo)
    before = time.time()
    cgi.index_codegraph_bg(str(repo))
    deadline = time.monotonic() + 15
    while cgi.codegraph_status != "ready" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cgi.codegraph_status == "ready"
    stamp = os.path.getmtime(repo / ".codegraph")
    assert before - 2 <= stamp <= time.time() + 2
    assert cgi.codegraph_is_stale(str(repo)) is False


def test_marionette_home_never_starts_an_indexer(tmp_path, monkeypatch):
    home = tmp_path / "state" / "home"
    home.mkdir(parents=True)
    (home / "notes.md").write_text("x\n")
    calls = _bind(monkeypatch, tmp_path, home, home=str(home))
    cgi.index_codegraph_bg(str(home))
    cgi.maybe_refresh_codegraph(str(home), force=True)
    assert calls == []
    assert cgi.codegraph_index_proc is None
    assert cgi.get_codegraph_status(str(home)) == "none"
    assert "Marionette Home" in (cgi.auto_index_refused(str(home)) or "")


def test_home_folder_and_its_parents_are_refused(tmp_path, monkeypatch):
    user_home = tmp_path / "Users" / "me"
    project = user_home / "Projects" / "app"
    project.mkdir(parents=True)
    _bind(monkeypatch, tmp_path, project)
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    assert cgi.auto_index_refused(str(user_home))
    assert cgi.auto_index_refused(str(user_home.parent))
    assert cgi.auto_index_refused(str(project)) is None


def test_stale_indexer_waiter_does_not_overwrite_a_newer_status(tmp_path, monkeypatch):
    repo = _indexed_repo(tmp_path)
    _bind(monkeypatch, tmp_path, repo,
          cmd=[sys.executable, "-c", "import time; time.sleep(0.5); raise SystemExit(1)"])
    cgi.index_codegraph_bg(str(repo))
    stale = cgi.codegraph_index_proc[1]
    # A newer indexer owns the status before the stale one exits.
    newer = (str(repo), object())
    cgi.codegraph_index_proc = newer
    cgi.codegraph_status = "ready"
    stale.wait(timeout=15)
    deadline = time.monotonic() + 5
    while cgi.codegraph_status_cache.get(str(repo), "absent") != "absent" and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    assert cgi.codegraph_status == "ready"
    assert cgi.codegraph_index_proc is newer
