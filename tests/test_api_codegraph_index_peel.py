"""Characterization tests for codegraph indexer runtime peel."""
from __future__ import annotations

import os

import harness.api.codegraph as cg_api
import harness.api.codegraph_index as cgi
import harness.server as srv


def test_server_reexports_index_runtime_helpers():
    assert srv._codegraph_indexed is cgi.codegraph_indexed
    assert srv._codegraph_index_alive is cgi.codegraph_index_alive
    assert srv._prepare_codegraph_scope is cgi.prepare_codegraph_scope
    assert srv._index_codegraph_bg is cgi.index_codegraph_bg
    assert srv._reindex_codegraph_bg is cgi.reindex_codegraph_bg
    assert srv._get_codegraph_status is cgi.get_codegraph_status
    assert srv._codegraph_is_stale is cgi.codegraph_is_stale
    assert srv._maybe_refresh_codegraph is cgi.maybe_refresh_codegraph
    assert srv._maybe_auto_index_codegraph is cgi.maybe_auto_index_codegraph


def test_codegraph_module_reexports_index_runtime():
    assert cg_api.codegraph_indexed is cgi.codegraph_indexed
    assert cg_api.index_codegraph_bg is cgi.index_codegraph_bg
    assert cg_api.get_codegraph_status is cgi.get_codegraph_status
    assert cg_api.codegraph_status_cache is cgi.codegraph_status_cache
    assert cg_api.codegraph_fail_until is cgi.codegraph_fail_until


def test_status_cache_and_fail_until_are_shared_dicts():
    assert srv._codegraph_status_cache is cgi.codegraph_status_cache
    assert srv._codegraph_fail_until is cgi.codegraph_fail_until
    assert srv._codegraph_index_lock is cgi.codegraph_index_lock


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
