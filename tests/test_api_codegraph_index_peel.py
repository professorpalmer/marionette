"""Characterization tests for codegraph indexer runtime peel."""
from __future__ import annotations

import os

import harness.api.codegraph_index as cgi
import harness.server as srv


def test_codegraph_indexed_requires_db_file(tmp_path):
    repo = str(tmp_path)
    os.makedirs(os.path.join(repo, ".codegraph"), exist_ok=True)
    assert cgi.codegraph_indexed(repo) is False
    open(os.path.join(repo, ".codegraph", "codegraph.db"), "wb").close()
    assert cgi.codegraph_indexed(repo) is True


def test_single_indexer_guard_skips_second_spawn(monkeypatch, tmp_path):
    repo = str(tmp_path / "proj")
    os.makedirs(repo, exist_ok=True)
    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: True)
    monkeypatch.setattr(cgi, "prepare_codegraph_scope", lambda path: {"verdict": "ok"})

    class _Alive:
        def poll(self):
            return None

    cgi.codegraph_index_proc = (repo, _Alive())
    cgi.codegraph_status = "indexing"

    import subprocess
    spawned = []

    def _no_spawn(*a, **k):
        spawned.append(1)
        raise AssertionError("second indexer must not spawn")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    try:
        cgi.index_codegraph_bg(repo)
        assert spawned == []
        assert cgi.codegraph_status == "indexing"
    finally:
        cgi.codegraph_index_proc = None
