"""Tests for CodeGraph indexing-state self-heal + double-spawn guard.

Regression coverage for the bug where the panel stuck on INDEXING after the
index finished (required an app restart), locked up on click, and dropped its
metrics -- caused by a sticky global status flag and concurrent indexers
colliding on the same SQLite.
"""
import os
import sys
import time

import pytest

import harness.api.codegraph_index as cgi
import harness.server as srv


class _FakeProc:
    def __init__(self, alive=True, rc=0):
        self._alive = alive
        self.returncode = rc

    def poll(self):
        return None if self._alive else self.returncode

    def finish(self, rc=0):
        self._alive = False
        self.returncode = rc


def test_index_alive_false_when_no_proc(monkeypatch):
    monkeypatch.setattr(cgi, "codegraph_index_proc", None)
    assert srv._codegraph_index_alive() is False


def test_index_alive_tracks_proc(monkeypatch):
    fp = _FakeProc(alive=True)
    monkeypatch.setattr(cgi, "codegraph_index_proc", ("/repo", fp))
    assert srv._codegraph_index_alive() is True
    fp.finish(0)
    assert srv._codegraph_index_alive() is False


def test_status_self_heals_when_indexer_dead(monkeypatch, tmp_path):
    # Simulate the wedged state: global says "indexing" but no live indexer,
    # and a built CodeGraph DB exists on disk (index actually completed).
    repo = str(tmp_path)
    cg_dir = os.path.join(repo, ".codegraph")
    os.makedirs(cg_dir, exist_ok=True)
    # Gate is the DB file, not the directory alone (config-only checkouts).
    open(os.path.join(cg_dir, "codegraph.db"), "wb").close()
    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: True)
    monkeypatch.setattr(cgi, "codegraph_index_proc", None)  # no live indexer
    cgi.codegraph_status = "indexing"

    # The getter must NOT stay pinned on "indexing" -- it resolves from disk.
    result = srv._get_codegraph_status(repo)
    assert result == "ready"
    assert cgi.codegraph_status == "ready"


def test_status_clears_indexing_when_indexer_dead_without_db(monkeypatch, tmp_path):
    repo = str(tmp_path)
    os.makedirs(os.path.join(repo, ".codegraph"), exist_ok=True)
    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: True)
    # Dead proc still held in the handle -- distinguishes "exited" from
    # "preflight, not spawned yet" (which must stay indexing).
    fp = _FakeProc(alive=False)
    monkeypatch.setattr(cgi, "codegraph_index_proc", (repo, fp))
    cgi.codegraph_status = "indexing"
    assert srv._get_codegraph_status(repo) == "unsupported"
    assert cgi.codegraph_status == "unsupported"


def test_status_stays_indexing_during_preflight_before_spawn(monkeypatch, tmp_path):
    """No proc yet + indexing must NOT flash unsupported (open-folder race)."""
    repo = str(tmp_path)
    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: True)
    monkeypatch.setattr(cgi, "codegraph_index_proc", None)
    cgi.codegraph_status = "indexing"
    cgi.codegraph_status_reason = None
    assert srv._get_codegraph_status(repo) == "indexing"
    assert cgi.codegraph_status == "indexing"


def test_status_stays_indexing_while_alive(monkeypatch, tmp_path):
    repo = str(tmp_path)
    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: True)
    fp = _FakeProc(alive=True)
    monkeypatch.setattr(cgi, "codegraph_index_proc", (repo, fp))
    cgi.codegraph_status = "indexing"
    assert srv._get_codegraph_status(repo) == "indexing"


# An indexer that hits its timeout is stopped with its whole process tree.

CHILD = (
    "import subprocess, sys, time;"
    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
    "open(sys.argv[1], 'w').write(str(c.pid));"
    "time.sleep(60)"
)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # a zombie reaped by nobody still answers kill(0)
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_timed_out_indexer_takes_its_grandchild_with_it(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    pid_file = tmp_path / "grandchild.pid"
    deps = cgi.CodegraphIndexDeps(
        puppetmaster_available=lambda: True,
        puppetmaster_cmd=lambda *a: [sys.executable, "-c", CHILD, str(pid_file)],
        diag=lambda *a, **k: None,
        get_state_dir=lambda: str(tmp_path),
        get_repo=lambda: str(repo),
    )
    monkeypatch.setattr(cgi, "_deps", deps)
    monkeypatch.setattr(cgi, "INDEX_TIMEOUT_S", 1)
    monkeypatch.setattr(cgi, "prepare_codegraph_scope", lambda path: {"verdict": "ok"})
    monkeypatch.setattr(cgi, "codegraph_index_log_path", lambda: str(tmp_path / "index.log"))
    monkeypatch.setattr(cgi, "codegraph_index_proc", None)
    monkeypatch.setattr(cgi, "codegraph_status", "none")
    monkeypatch.setattr(cgi, "codegraph_status_reason", None)
    monkeypatch.setattr(cgi, "codegraph_fail_until", {})
    monkeypatch.setattr(cgi, "codegraph_status_cache", {})
    cgi.index_codegraph_bg(str(repo))
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while (cgi.codegraph_index_proc is not None or "timed out" not in str(cgi.codegraph_status_reason)) \
            and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    try:
        # macOS CI once saw the child exit 1 before the timeout with no
        # output; keep the whole log for the next occurrence.
        log = tmp_path / "index.log"
        assert "timed out" in str(cgi.codegraph_status_reason), (
            log.read_text() if log.exists() else "no index log"
        )
        assert not _alive(grandchild)
    finally:
        try:
            os.kill(grandchild, 9)
        except OSError:
            pass
