"""Characterization: table-driven Handler GET/POST dispatch + auth.

Locks status codes and auth behavior for a sample of public vs guarded routes
after compressing do_GET / _handle_post_json into path→handler maps.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest


def _serve():
    import harness.server as srv

    # Rebuild tables if a prior test imported server before this module's patches.
    srv._POST_JSON_ROUTES = None
    srv._GET_ROUTES = None
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    return srv, httpd, port


def _get(port, path, token=None):
    headers = {"X-Harness-Token": token} if token else {}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers=headers, method="GET"
    )
    return urllib.request.urlopen(req, timeout=10)


def _post(port, path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Harness-Token"] = token
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=headers,
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=10)


def test_route_tables_cover_known_paths():
    import harness.server as srv

    srv._POST_JSON_ROUTES = None
    srv._GET_ROUTES = None
    post = srv._post_json_routes()
    get = srv._get_routes()
    assert "/api/settings" in post
    assert "/api/sessions/relocate" in post and "/api/sessions/move" in post
    assert "/api/memory" in get
    assert "/api/wiki/connect" not in get  # pre-auth special case in do_GET
    assert len(post) >= 90
    assert len(get) >= 50


def test_public_get_shell_stays_open_without_token():
    srv, httpd, port = _serve()
    try:
        resp = _get(port, "/")
        assert resp.status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_guarded_get_rejects_missing_and_wrong_token():
    srv, httpd, port = _serve()
    try:
        for path in ("/api/memory", "/api/config", "/api/platform"):
            with pytest.raises(urllib.error.HTTPError) as ei:
                _get(port, path)
            assert ei.value.code == 403
            with pytest.raises(urllib.error.HTTPError) as ei:
                _get(port, path, token="wrong")
            assert ei.value.code == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_guarded_get_accepts_valid_token():
    srv, httpd, port = _serve()
    try:
        resp = _get(port, "/api/memory", token=srv._TOKEN)
        assert resp.status == 200
        body = json.loads(resp.read())
        assert isinstance(body, dict)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_unknown_get_is_404_when_authed():
    srv, httpd, port = _serve()
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            _get(port, "/api/this-route-does-not-exist", token=srv._TOKEN)
        assert ei.value.code == 404
        err = json.loads(ei.value.read())
        assert err.get("error") == "not found"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_post_rejects_missing_token():
    srv, httpd, port = _serve()
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(port, "/api/settings", body={})
        assert ei.value.code == 403
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_post_settings_accepts_valid_token():
    srv, httpd, port = _serve()
    try:
        resp = _post(port, "/api/settings", body={}, token=srv._TOKEN)
        assert resp.status == 200
        body = json.loads(resp.read())
        assert isinstance(body, dict)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_post_unknown_path_is_404_when_authed():
    srv, httpd, port = _serve()
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(port, "/api/this-route-does-not-exist", body={}, token=srv._TOKEN)
        assert ei.value.code == 404
        err = json.loads(ei.value.read())
        assert err.get("error") == "not found"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_post_invalid_json_is_400():
    srv, httpd, port = _serve()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/settings",
            data=b"{not-json",
            headers={
                "Content-Type": "application/json",
                "X-Harness-Token": srv._TOKEN,
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)
        assert ei.value.code == 400
        err = json.loads(ei.value.read())
        assert err.get("error") == "invalid JSON"
    finally:
        httpd.shutdown()
        httpd.server_close()
