"""Tests for CodeGraph GET/POST endpoints."""
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
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {}, method="GET")
    return urllib.request.urlopen(req, timeout=10)


def _post(port, path, body, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=10)


def test_codegraph_get_without_token():
    httpd, port, srv = _server()
    try:
        try:
            _get(port, "/api/codegraph")
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()


def test_codegraph_get_graceful_when_no_repo():
    httpd, port, srv = _server()
    # Save original repo
    orig_repo = srv._cfg.repo
    srv._cfg.repo = ""  # simulate no repo
    try:
        resp = _get(port, "/api/codegraph", {"X-Harness-Token": srv._TOKEN})
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["indexed"] is False
        assert data["status"] == "none"
        assert data["nodes"] is None
        assert data["edges"] is None
        assert data["files"] is None
        assert data["languages"] is None
        assert data["last_indexed"] is None
        assert data["repo"] == ""
    finally:
        srv._cfg.repo = orig_repo
        httpd.shutdown()


def test_codegraph_reindex_rejected_without_token():
    httpd, port, srv = _server()
    try:
        try:
            _post(port, "/api/codegraph/reindex", {},
                  {"Content-Type": "application/json"})
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()


def test_codegraph_reindex_indexing_state():
    httpd, port, srv = _server()
    orig_repo = srv._cfg.repo
    # Setup simulated directory that exists to avoid 400 No Open Workspace
    import os
    srv._cfg.repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        resp = _post(port, "/api/codegraph/reindex", {},
                     {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["ok"] is True
        assert data["status"] == "indexing"
        
        # Verify GET reflects "indexing" status
        get_resp = _get(port, "/api/codegraph", {"X-Harness-Token": srv._TOKEN})
        get_data = json.loads(get_resp.read().decode())
        assert get_data["status"] == "indexing"
    finally:
        srv._cfg.repo = orig_repo
        httpd.shutdown()
