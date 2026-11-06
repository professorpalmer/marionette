"""Open Folder must not flash CodeGraph UNSUPPORTED before the DB exists.

Regression for the LeftRail badge flash: global status defaulted to
unsupported, and ``_index_codegraph_bg`` ran preflight before claiming
indexing, so polls/open responses painted UNSUPPORTED until spawn.
"""
import os
import subprocess

import harness.api.codegraph_index as cgi
import harness.server as srv


def test_get_status_is_indexing_not_unsupported_before_db(monkeypatch, tmp_path):
    repo = str(tmp_path / "proj")
    os.makedirs(repo, exist_ok=True)
    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: True)
    monkeypatch.setattr(cgi, "codegraph_index_proc", None)
    cgi.codegraph_status = "none"
    cgi.codegraph_status_reason = None

    assert not srv._codegraph_indexed(repo)
    assert srv._get_codegraph_status(repo) == "indexing"
    assert cgi.codegraph_status == "indexing"


def test_index_bg_sets_indexing_before_preflight(monkeypatch, tmp_path):
    repo = str(tmp_path / "proj")
    os.makedirs(repo, exist_ok=True)
    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: True)
    monkeypatch.setattr(cgi, "codegraph_index_proc", None)
    cgi.codegraph_status = "none"
    cgi.codegraph_status_reason = None

    seen = []

    def slow_preflight(path):
        # While preflight runs, status must already be indexing.
        seen.append(cgi.codegraph_status)
        return {"verdict": "ok"}

    monkeypatch.setattr(cgi, "prepare_codegraph_scope", slow_preflight)

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
    """Open without .codegraph claims indexing before the indexer starts."""
    from types import SimpleNamespace

    from harness.api.workspace import WorkspaceServices, post_workspace_open

    repo = tmp_path / "fresh"
    repo.mkdir()
    monkeypatch.setenv("HARNESS_REPO", "")
    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: True)
    monkeypatch.setattr(cgi, "codegraph_index_proc", None)
    cgi.codegraph_status = "unsupported"
    cgi.codegraph_status_reason = "stale failure from the last repo"

    status_at_spawn = []
    svc = WorkspaceServices(
        cfg=SimpleNamespace(repo="", driver="m1"),
        parse_bool=bool,
        ws=None,
        paths_same_workspace=lambda a, b: a == b,
        forget_recent_workspace=lambda p: [],
        clear_active_codegraph=lambda: None,
        get_codegraph_status=srv._get_codegraph_status,
        workspace_json_path=lambda: str(tmp_path / "workspace.json"),
        home_workspace_path=lambda: str(tmp_path),
        prepare_home_workspace=lambda: str(tmp_path),
        is_app_install_root=lambda p: False,
        diag=lambda *a: None,
        puppetmaster_available=srv._puppetmaster_available,
        set_codegraph_status=cgi.set_codegraph_status,
        # The indexer subprocess is the boundary: record the status it sees.
        index_codegraph_bg=lambda path: status_at_spawn.append(
            (path, cgi.codegraph_status, cgi.codegraph_status_reason)
        ),
    )

    code, payload = post_workspace_open({"path": str(repo)}, svc)
    assert code == 200
    assert status_at_spawn == [(str(repo), "indexing", None)]
    assert payload["codegraph"] == "indexing"


def test_confirmed_failure_still_unsupported(monkeypatch, tmp_path):
    repo = str(tmp_path / "proj")
    os.makedirs(repo, exist_ok=True)
    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: True)
    monkeypatch.setattr(cgi, "codegraph_index_proc", None)
    cgi.codegraph_status = "unsupported"
    cgi.codegraph_status_reason = "Indexer failed (exit 1)."
    assert srv._get_codegraph_status(repo) == "unsupported"
