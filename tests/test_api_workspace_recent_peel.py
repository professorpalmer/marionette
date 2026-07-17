"""Characterization tests for workspace recent/forget persistence peel."""
from __future__ import annotations

import json
import tempfile

import harness.api.workspace as ws_api
import harness.server as srv


def test_server_reexports_recent_helpers():
    assert srv._record_recent_workspace is ws_api.record_recent_workspace
    assert srv._forget_recent_workspace is ws_api.forget_recent_workspace


def test_record_recent_appends_and_caps(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/some/other/dummy/path")
    ws_file = tmp_path / "workspace.json"

    dirs = []
    for i in range(10):
        d = tmp_path / f"proj{i}"
        d.mkdir()
        dirs.append(str(d))
        recents = ws_api.record_recent_workspace(str(d))

    # Cap keeps the first 8 in stable append order (not newest-only).
    assert len(recents) == 8
    assert recents == dirs[:8]
    assert dirs[-1] not in recents
    with open(ws_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["repo"] == dirs[-1]
    assert data["recents"] == dirs[:8]


def test_record_recent_skips_app_install_root(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/some/other/dummy/path")
    app = tmp_path / "marionette-app"
    app.mkdir()
    user = tmp_path / "userproj"
    user.mkdir()
    monkeypatch.setattr(srv, "_is_app_install_root", lambda p: str(app) in str(p))
    recents = ws_api.record_recent_workspace(str(app))
    assert str(app) not in recents
    recents = ws_api.record_recent_workspace(str(user))
    assert str(user) in recents


def test_forget_recent_clears_active_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: "/some/other/dummy/path")
    proj = tmp_path / "proj"
    proj.mkdir()
    ws_api.record_recent_workspace(str(proj))
    recents = ws_api.forget_recent_workspace(str(proj))
    assert recents == []
    with open(tmp_path / "workspace.json", encoding="utf-8") as f:
        data = json.load(f)
    assert data["repo"] == ""
    assert data["recents"] == []
