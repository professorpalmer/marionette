"""Regression: every non-public GET endpoint requires the auth token.

do_POST has one centralized token gate, but do_GET historically relied on each
handler re-adding a copy-pasted check -- and ~11 endpoints (/api/memory,
/api/config, /api/skills, /api/rules, /api/commands, /api/settings, /api/platform,
/api/jobs, /api/workspace, /api/mcp*) were left UNAUTHENTICATED, returning durable
memory/config/skills to any local caller with no token. A centralized gate at the
top of do_GET now authenticates every non-public path. This test locks that in so
the hole cannot silently reopen.
"""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest


def _serve():
    import harness.server as srv
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)
    return srv, httpd, port


def _get(port, path, token=None):
    headers = {"X-Harness-Token": token} if token else {}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers, method="GET")
    return urllib.request.urlopen(req, timeout=10)


# Endpoints that previously leaked with no token. All must 403 without the token.
_PROTECTED = [
    "/api/memory", "/api/config", "/api/skills", "/api/rules",
    "/api/commands", "/api/settings", "/api/platform", "/api/jobs",
]


def test_protected_get_endpoints_reject_missing_token():
    srv, httpd, port = _serve()
    try:
        for path in _PROTECTED:
            with pytest.raises(urllib.error.HTTPError) as ei:
                _get(port, path)  # no token
            assert ei.value.code == 403, f"{path} must 403 without a token, got {ei.value.code}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_protected_get_endpoints_reject_wrong_token():
    srv, httpd, port = _serve()
    try:
        for path in _PROTECTED:
            with pytest.raises(urllib.error.HTTPError) as ei:
                _get(port, path, token="definitely-wrong")
            assert ei.value.code == 403, f"{path} must 403 with a wrong token"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_public_assets_stay_open():
    # The renderer bootstrap assets must load BEFORE the page has the token.
    srv, httpd, port = _serve()
    try:
        for path in ("/", "/index.html"):
            resp = _get(port, path)  # no token
            assert resp.status == 200, f"{path} should be public"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_protected_get_accepts_valid_token():
    srv, httpd, port = _serve()
    try:
        resp = _get(port, "/api/memory", token=srv._TOKEN)
        assert resp.status == 200
        json.loads(resp.read())  # valid JSON body
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_protected_get_rejects_query_token_even_if_correct():
    """Query-string tokens must not authenticate (header-only policy)."""
    srv, httpd, port = _serve()
    token = srv._TOKEN
    try:
        url = f"http://127.0.0.1:{port}/api/memory?token={token}"
        req = urllib.request.Request(url, method="GET")
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "expected 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
            body = e.read().decode("utf-8", errors="ignore")
            assert token not in body
    finally:
        httpd.shutdown()
        httpd.server_close()
