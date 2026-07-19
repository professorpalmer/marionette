"""Characterization tests for legacy browser-shell static asset peel."""
from __future__ import annotations

from pathlib import Path

import harness.api.static as static_api
import harness.server as srv


def test_public_get_paths_match_handler():
    assert srv.Handler._PUBLIC_GET_PATHS is static_api.PUBLIC_GET_PATHS
    assert static_api.PUBLIC_GET_PATHS == frozenset(
        {"/", "/index.html", "/app.js", "/app.css"}
    )


def test_try_static_shell_injects_harness_token_when_opted_in(tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text(
        "<html><head><title>t</title></head><body></body></html>",
        encoding="utf-8",
    )
    (web / "app.js").write_text("console.log(1)", encoding="utf-8")
    (web / "app.css").write_text("body{}", encoding="utf-8")

    monkeypatch.setenv("HARNESS_DEV_ALLOW_TOKEN_META", "1")
    status, html, ctype = static_api.try_static_shell("/", web_root=web, token="abc123")
    assert status == 200 and ctype == "text/html"
    assert 'name="harness-token" content="abc123"' in html
    assert "</head>" in html

    status, js, ctype = static_api.try_static_shell(
        "/app.js", web_root=web, token="abc123"
    )
    assert status == 200 and ctype == "application/javascript"
    assert js == "console.log(1)"

    status, css, ctype = static_api.try_static_shell(
        "/app.css", web_root=web, token="abc123"
    )
    assert status == 200 and ctype == "text/css"
    assert css == "body{}"


def test_try_static_shell_ignores_api_paths(tmp_path):
    assert static_api.try_static_shell(
        "/api/config", web_root=Path(tmp_path), token="x"
    ) is None


def test_try_static_shell_does_not_inject_harness_token_by_default(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text(
        "<html><head><title>t</title></head><body></body></html>",
        encoding="utf-8",
    )
    status, html, ctype = static_api.try_static_shell("/", web_root=web, token="abc123")
    assert status == 200 and ctype == "text/html"
    assert 'name="harness-token"' not in html


def test_server_web_root_still_points_at_harness_web():
    assert srv._WEB.name == "web"
    assert (srv._WEB / "index.html").is_file()
