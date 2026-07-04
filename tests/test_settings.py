"""Tests for settings GET/POST endpoints."""
import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest


def _server():
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _get(port, path, headers=None):
    h = dict(headers or {})
    if "X-Harness-Token" not in h:
        import harness.server as _srv
        h["X-Harness-Token"] = _srv._TOKEN
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=h, method="GET")
    return urllib.request.urlopen(req, timeout=10)


def _post(port, path, body, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=10)


def test_settings_get_returns_expected_shape():
    httpd, port, srv = _server()
    try:
        resp = _get(port, "/api/settings")
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        
        # Verify keys
        assert "driver" in data
        assert "reach" in data
        assert "budget" in data
        assert "models" in data
        assert "auto_distill" in data
        assert "wiki_auto" in data
        assert "state_dir" in data
        assert "repo" in data
    finally:
        httpd.shutdown()


def test_settings_post_rejected_without_token():
    httpd, port, srv = _server()
    try:
        try:
            _post(port, "/api/settings", {"budget": 10},
                  {"Content-Type": "application/json"})
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()


def test_settings_post_updates_settings_successfully():
    httpd, port, srv = _server()
    try:
        # Check initial budget & auto_distill
        resp = _get(port, "/api/settings")
        initial_data = json.loads(resp.read().decode())
        initial_budget = initial_data["budget"]
        initial_auto_distill = initial_data["auto_distill"]

        # Modify values
        target_budget = 7 if initial_budget != 7 else 12
        target_auto_distill = not initial_auto_distill

        # Post update
        post_resp = _post(port, "/api/settings",
                          {"budget": target_budget, "auto_distill": target_auto_distill},
                          {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert post_resp.status == 200
        post_data = json.loads(post_resp.read().decode())
        
        assert post_data["budget"] == target_budget
        assert post_data["auto_distill"] is target_auto_distill

        # Verify via subsequent GET
        get_resp2 = _get(port, "/api/settings")
        get_data2 = json.loads(get_resp2.read().decode())
        assert get_data2["budget"] == target_budget
        assert get_data2["auto_distill"] is target_auto_distill
    finally:
        httpd.shutdown()
