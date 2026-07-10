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


def test_boot_clears_leaked_app_root_harness_repo_and_restores_user(
    reload_server, tmp_path, monkeypatch,
):
    """Packaged startup may inherit HARNESS_REPO=<app checkout>; restore user repo."""
    user_proj = tmp_path / "dugout"
    user_proj.mkdir()
    app_root = tmp_path / "app-checkout"
    app_root.mkdir()
    (tmp_path / "workspace.json").write_text(
        json.dumps({"repo": str(user_proj), "recents": [str(user_proj)]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MARIONETTE_APP_ROOT", str(app_root))
    srv = reload_server(HARNESS_REPO=str(app_root))
    assert srv._cfg.repo == str(user_proj)
    assert os.environ.get("HARNESS_REPO") == str(user_proj)


def test_boot_clears_leaked_app_root_without_saved_workspace(
    reload_server, tmp_path, monkeypatch,
):
    """Leaked app-root HARNESS_REPO with no workspace.json must open nothing."""
    app_root = tmp_path / "app-checkout"
    app_root.mkdir()
    monkeypatch.setenv("MARIONETTE_APP_ROOT", str(app_root))
    srv = reload_server(HARNESS_REPO=str(app_root))
    assert srv._cfg.repo == ""
    assert not os.environ.get("HARNESS_REPO")


def test_boot_preserves_explicit_cli_app_checkout_without_electron_marker(
    reload_server, monkeypatch,
):
    """Direct CLI may intentionally open the running checkout (no MARIONETTE_APP_ROOT)."""
    import harness

    running = Path(harness.__file__).resolve().parent.parent
    monkeypatch.delenv("MARIONETTE_APP_ROOT", raising=False)
    monkeypatch.delenv("HARNESS_APP_ROOT", raising=False)
    srv = reload_server(HARNESS_REPO=str(running))
    assert srv._cfg.repo == str(running)
    assert os.environ.get("HARNESS_REPO") == str(running)


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


def _mixed_case_spelling(path: str) -> str:
    """A differently-cased spelling of the same directory.

    On win32 ``os.path.normcase`` folds case, so the swapped spelling still
    denotes the same path and exercises the case-insensitive guard. On POSIX
    paths are case-sensitive, so return the original (the test degenerates to
    an exact match but still passes).
    """
    swapped = path.swapcase()
    if os.path.normcase(swapped) == os.path.normcase(path):
        return swapped
    return path


def test_app_root_guard_matches_mixed_case(tmp_path, monkeypatch):
    """Windows surfaces the same dir with mixed casing; the guard must still hit."""
    import harness.server as srv
    app = tmp_path / "App-Checkout"
    app.mkdir()
    monkeypatch.setenv("MARIONETTE_APP_ROOT", _mixed_case_spelling(str(app)))
    assert srv._is_app_install_root(str(app))
    assert srv._is_app_install_root(_mixed_case_spelling(str(app)))
    assert not srv._is_app_install_root(str(tmp_path))


def test_record_recent_refuses_mixed_case_app_root(tmp_path, monkeypatch):
    """_record_recent_workspace must refuse the app root in any spelling."""
    import harness.server as srv
    user = tmp_path / "user-proj"
    user.mkdir()
    app = tmp_path / "App-Checkout"
    app.mkdir()
    monkeypatch.setenv("MARIONETTE_APP_ROOT", _mixed_case_spelling(str(app)))
    monkeypatch.setattr(srv, "_workspace_json_path", lambda: str(tmp_path / "workspace.json"))
    monkeypatch.setattr(srv, "_resolve_existing_state_file", lambda name: str(tmp_path / name))
    srv._record_recent_workspace(str(user))
    srv._record_recent_workspace(str(app))
    srv._record_recent_workspace(_mixed_case_spelling(str(app)))
    data = json.loads((tmp_path / "workspace.json").read_text(encoding="utf-8"))
    assert data["repo"] == str(user)
    assert str(app) not in data["recents"]
    assert _mixed_case_spelling(str(app)) not in data["recents"]
    assert str(user) in data["recents"]


def test_boot_app_root_active_session_promotes_user_repo_session(reload_server, tmp_path, monkeypatch):
    """Stale active session rooted at the app checkout must not re-point the
    workspace: boot keeps the restored user repo and activates the newest
    session under it instead (same-workspace promotion)."""
    user_proj = tmp_path / "dugout"
    user_proj.mkdir()
    app_root = tmp_path / "App-Checkout"
    app_root.mkdir()
    (tmp_path / "workspace.json").write_text(
        json.dumps({"repo": str(user_proj), "recents": [str(user_proj)]}),
        encoding="utf-8",
    )
    (tmp_path / "harness_sessions.json").write_text(json.dumps({
        "sessions": [
            {"id": "user-old", "title": "Old", "created": 1.0,
             "workspace_root": str(user_proj), "input_tokens": 5},
            {"id": "user-new", "title": "New", "created": 2.0,
             "workspace_root": str(user_proj), "input_tokens": 5},
            {"id": "approot1", "title": "App work", "created": 3.0,
             "workspace_root": str(app_root), "input_tokens": 7},
        ],
        "active": "approot1",
    }), encoding="utf-8")
    monkeypatch.setenv("MARIONETTE_APP_ROOT", _mixed_case_spelling(str(app_root)))
    srv = reload_server(HARNESS_REPO=None)
    assert srv._cfg.repo == str(user_proj)
    assert srv._sessions.active == "user-new"
    # Non-empty app-root rows survive; they just must not drive selection.
    assert "approot1" in {s["id"] for s in srv._sessions.rows()}


def test_boot_purges_empty_app_root_session_rows(reload_server, tmp_path, monkeypatch):
    """Empty app-root rows (zero tokens, no transcript body) are deleted at
    boot together with their transcript files; non-empty rows are kept."""
    user_proj = tmp_path / "proj"
    user_proj.mkdir()
    app_root = tmp_path / "App-Checkout"
    app_root.mkdir()
    (tmp_path / "workspace.json").write_text(
        json.dumps({"repo": str(user_proj)}),
        encoding="utf-8",
    )
    trans_dir = tmp_path / "transcripts"
    trans_dir.mkdir()
    (trans_dir / "empty1.json").write_text(
        json.dumps({"history": [], "display": []}), encoding="utf-8"
    )
    (trans_dir / "busy1.json").write_text(
        json.dumps({"history": [{"role": "user", "content": "hi"}], "display": []}),
        encoding="utf-8",
    )
    (tmp_path / "harness_sessions.json").write_text(json.dumps({
        "sessions": [
            {"id": "empty1", "title": "New session", "created": 1.0,
             "workspace_root": str(app_root)},
            {"id": "busy1", "title": "Real work", "created": 2.0,
             "workspace_root": str(app_root)},
            {"id": "user1", "title": "User", "created": 3.0,
             "workspace_root": str(user_proj)},
        ],
        "active": "empty1",
    }), encoding="utf-8")
    monkeypatch.setenv("MARIONETTE_APP_ROOT", str(app_root))
    srv = reload_server(HARNESS_REPO=None)
    ids = {s["id"] for s in srv._sessions.rows()}
    assert "empty1" not in ids
    assert "busy1" in ids
    assert "user1" in ids
    assert not (trans_dir / "empty1.json").exists()
    assert (trans_dir / "busy1.json").exists()
    # Active was the purged empty row; promotion lands on the restored repo.
    assert srv._sessions.active == "user1"


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


def test_app_install_roots_include_running_harness_checkout(monkeypatch):
    """The checkout executing this process is always treated as app-root,
    even when MARIONETTE_APP_ROOT is unset (covers Projects/marionette)."""
    import harness
    import harness.server as srv

    monkeypatch.delenv("MARIONETTE_APP_ROOT", raising=False)
    monkeypatch.delenv("HARNESS_APP_ROOT", raising=False)
    monkeypatch.delenv("MARIONETTE_CHECKOUT", raising=False)
    monkeypatch.delenv("HARNESS_CHECKOUT", raising=False)
    running = Path(harness.__file__).resolve().parent.parent
    assert srv._is_app_install_root(str(running)) is True


def test_session_switch_does_not_repoint_to_app_install_root(tmp_path, monkeypatch):
    """Stale app-checkout sessions must not yank the live workspace back."""
    import json as _json
    import threading
    from http.server import ThreadingHTTPServer
    import harness.server as srv

    user = tmp_path / "user-proj"
    user.mkdir()
    app = tmp_path / "app-checkout"
    app.mkdir()
    monkeypatch.setenv("MARIONETTE_APP_ROOT", str(app))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(srv, "_puppetmaster_available", lambda: False)
    monkeypatch.setattr(srv, "_index_codegraph_bg", lambda repo: None)

    srv._cfg.repo = str(user)
    os.environ["HARNESS_REPO"] = str(user)
    srv._sessions.path = str(tmp_path / "harness_sessions.json")
    srv._sessions._sessions = [
        {"id": "user1", "title": "User", "created": 1.0,
         "repo": str(user), "workspace_root": str(user), "archived": False},
        {"id": "app1", "title": "App audit", "created": 2.0,
         "repo": str(app), "workspace_root": str(app), "archived": False},
    ]
    srv._sessions._active = "user1"
    srv._sessions._save()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/sessions/switch",
            data=_json.dumps({"id": "app1"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Harness-Token": srv._TOKEN,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = _json.loads(resp.read().decode())
        assert body.get("ok") is True
        assert srv._sessions.active == "app1"
        # Workspace must stay on the user project.
        assert srv._cfg.repo == str(user)
        assert body.get("repo") == str(user)
    finally:
        httpd.shutdown()
