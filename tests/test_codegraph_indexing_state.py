"""Tests for CodeGraph indexing-state self-heal + double-spawn guard.

Regression coverage for the bug where the panel stuck on INDEXING after the
index finished (required an app restart), locked up on click, and dropped its
metrics -- caused by a sticky global status flag and concurrent indexers
colliding on the same SQLite.
"""
import os
import tempfile

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
    monkeypatch.setattr(srv, "_codegraph_index_proc", None)
    assert srv._codegraph_index_alive() is False


def test_index_alive_tracks_proc(monkeypatch):
    fp = _FakeProc(alive=True)
    monkeypatch.setattr(srv, "_codegraph_index_proc", ("/repo", fp))
    assert srv._codegraph_index_alive() is True
    fp.finish(0)
    assert srv._codegraph_index_alive() is False


def test_status_self_heals_when_indexer_dead(monkeypatch, tmp_path):
    # Simulate the wedged state: global says "indexing" but no live indexer,
    # and the .codegraph dir exists on disk (index actually completed).
    repo = str(tmp_path)
    os.makedirs(os.path.join(repo, ".codegraph"), exist_ok=True)
    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: True)
    monkeypatch.setattr(srv, "_codegraph_index_proc", None)  # no live indexer
    srv._codegraph_status = "indexing"

    # The getter must NOT stay pinned on "indexing" -- it resolves from disk.
    result = srv._get_codegraph_status(repo)
    assert result == "ready"
    assert srv._codegraph_status == "ready"


def test_status_stays_indexing_while_alive(monkeypatch, tmp_path):
    repo = str(tmp_path)
    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: True)
    fp = _FakeProc(alive=True)
    monkeypatch.setattr(srv, "_codegraph_index_proc", (repo, fp))
    srv._codegraph_status = "indexing"
    assert srv._get_codegraph_status(repo) == "indexing"
