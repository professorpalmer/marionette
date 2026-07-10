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


def test_boot_skips_app_install_root_and_scrubs_recents(reload_server, tmp_path, monkeypatch):
    """~/.marionette/marionette (and MARIONETTE_APP_ROOT) must never auto-open."""
    user_proj = tmp_path / "dugout"
    user_proj.mkdir()
    app_root = tmp_path / "app-checkout"
    app_root.mkdir()
    (tmp_path / "workspace.json").write_text(
        json.dumps({
            "repo": str(app_root),
            "recents": [str(app_root), str(user_proj)],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("MARIONETTE_APP_ROOT", str(app_root))
    srv = reload_server(HARNESS_REPO=None)
    assert srv._cfg.repo == str(user_proj)
    assert os.environ.get("HARNESS_REPO") == str(user_proj)
    data = json.loads((tmp_path / "workspace.json").read_text(encoding="utf-8"))
    assert data["repo"] == str(user_proj)
    assert str(app_root) not in data["recents"]
    assert str(user_proj) in data["recents"]


def test_pick_boot_workspace_prefers_repo_then_recents(tmp_path, monkeypatch):
    import harness.server as srv
    app = tmp_path / "marionette-app"
    app.mkdir()
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("MARIONETTE_APP_ROOT", str(app))
    assert srv._pick_boot_workspace({"repo": str(a), "recents": [str(b)]}) == str(a)
    assert srv._pick_boot_workspace({"repo": str(app), "recents": [str(app), str(b)]}) == str(b)
    assert srv._pick_boot_workspace({"repo": str(app), "recents": [str(app)]}) == ""


def test_record_recent_skips_app_install_root(tmp_path, monkeypatch):
    import harness.server as srv
    user = tmp_path / "user-proj"
    user.mkdir()
    app = tmp_path / "app-checkout"
    app.mkdir()
    monkeypatch.setenv("MARIONETTE_APP_ROOT", str(app))
    monkeypatch.setattr(srv, "_workspace_json_path", lambda: str(tmp_path / "workspace.json"))
    monkeypatch.setattr(srv, "_resolve_existing_state_file", lambda name: str(tmp_path / name))
    srv._record_recent_workspace(str(user))
    srv._record_recent_workspace(str(app))
    data = json.loads((tmp_path / "workspace.json").read_text(encoding="utf-8"))
    assert data["repo"] == str(user)
    assert str(app) not in data["recents"]
    assert str(user) in data["recents"]
