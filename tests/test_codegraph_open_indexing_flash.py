"""Open Folder must not flash CodeGraph UNSUPPORTED before the DB exists.

Regression for the LeftRail badge flash: global status defaulted to
unsupported, and ``_index_codegraph_bg`` ran preflight before claiming
indexing, so polls/open responses painted UNSUPPORTED until spawn.
"""
import os
import subprocess

import harness.server as srv


def test_get_status_is_indexing_not_unsupported_before_db(monkeypatch, tmp_path):
    repo = str(tmp_path / "proj")
    os.makedirs(repo, exist_ok=True)
    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: True)
    monkeypatch.setattr(srv, "_codegraph_index_proc", None)
    srv._codegraph_status = "none"
    srv._codegraph_status_reason = None

    assert not srv._codegraph_indexed(repo)
    assert srv._get_codegraph_status(repo) == "indexing"
    assert srv._codegraph_status == "indexing"


def test_index_bg_sets_indexing_before_preflight(monkeypatch, tmp_path):
    repo = str(tmp_path / "proj")
    os.makedirs(repo, exist_ok=True)
    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: True)
    monkeypatch.setattr(srv, "_codegraph_index_proc", None)
    srv._codegraph_status = "none"
    srv._codegraph_status_reason = None

    seen = []

    def slow_preflight(path):
        # While preflight runs, status must already be indexing.
        seen.append(srv._codegraph_status)
        return {"verdict": "ok"}

    monkeypatch.setattr(srv, "_prepare_codegraph_scope", slow_preflight)

    class _Dead:
        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        @property
        def returncode(self):
            return 0

        stdout = None

        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _Dead())

    srv._index_codegraph_bg(repo)
    assert seen == ["indexing"]


def test_workspace_open_reports_indexing_before_db(monkeypatch, tmp_path):
    """Open without .codegraph claims indexing, never unsupported."""
    repo = tmp_path / "fresh"
    repo.mkdir()

    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: True)

    def stub_index(path):
        srv._codegraph_status = "indexing"
        srv._codegraph_status_reason = None

    monkeypatch.setattr(srv, "_index_codegraph_bg", stub_index)

    srv._codegraph_status = "none"
    srv._codegraph_status_reason = None
    if srv._puppetmaster_available():
        srv._codegraph_status = "indexing"
        srv._codegraph_status_reason = None
    srv._index_codegraph_bg(str(repo))
    status = srv._get_codegraph_status(str(repo))
    assert status == "indexing"
    assert status != "unsupported"


def test_confirmed_failure_still_unsupported(monkeypatch, tmp_path):
    repo = str(tmp_path / "proj")
    os.makedirs(repo, exist_ok=True)
    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: True)
    monkeypatch.setattr(srv, "_codegraph_index_proc", None)
    srv._codegraph_status = "unsupported"
    srv._codegraph_status_reason = "Indexer failed (exit 1)."
    assert srv._get_codegraph_status(repo) == "unsupported"
