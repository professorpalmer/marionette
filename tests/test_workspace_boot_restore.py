"""Boot must restore the last project from workspace.json when HARNESS_REPO
is unset, and leave repo empty on first launch (no workspace.json).

Regression: Electron used to force HARNESS_REPO=<marionette checkout> on every
spawn, so this restore path never ran and the UI always opened Marionette.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def reload_server(monkeypatch, tmp_path):
    """Import harness.server against an isolated state dir (never real home)."""
    def _reload(**env):
        monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
        for key in ("HARNESS_REPO", "HARNESS_DRIVER"):
            if key in env:
                if env[key] is None:
                    monkeypatch.delenv(key, raising=False)
                else:
                    monkeypatch.setenv(key, env[key])
            else:
                monkeypatch.delenv(key, raising=False)
        import importlib
        import harness.server as server
        importlib.reload(server)
        return server

    return _reload


def test_boot_restores_last_project_when_harness_repo_unset(reload_server, tmp_path):
    repo = tmp_path / "last-project"
    repo.mkdir()
    (tmp_path / "workspace.json").write_text(
        json.dumps({"repo": str(repo), "recent": [str(repo)]}),
        encoding="utf-8",
    )
    srv = reload_server(HARNESS_REPO=None)
    assert srv._cfg.repo == str(repo)
    assert os.environ.get("HARNESS_REPO") == str(repo)


def test_boot_skips_workspace_when_harness_repo_already_set(reload_server, tmp_path):
    saved = tmp_path / "saved-project"
    saved.mkdir()
    forced = tmp_path / "forced-project"
    forced.mkdir()
    (tmp_path / "workspace.json").write_text(
        json.dumps({"repo": str(saved)}),
        encoding="utf-8",
    )
    srv = reload_server(HARNESS_REPO=str(forced))
    assert srv._cfg.repo == str(forced)
    assert os.environ.get("HARNESS_REPO") == str(forced)


def test_boot_first_launch_opens_no_project(reload_server, tmp_path):
    # No workspace.json under the isolated state dir.
    srv = reload_server(HARNESS_REPO=None)
    assert srv._cfg.repo == ""
    assert not os.environ.get("HARNESS_REPO")


def test_boot_ignores_vanished_workspace_repo(reload_server, tmp_path):
    vanished = tmp_path / "gone"
    (tmp_path / "workspace.json").write_text(
        json.dumps({"repo": str(vanished)}),
        encoding="utf-8",
    )
    srv = reload_server(HARNESS_REPO=None)
    assert srv._cfg.repo == ""
    assert not os.environ.get("HARNESS_REPO")
