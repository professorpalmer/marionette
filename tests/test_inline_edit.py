"""Tests for the CMD-K /api/inline-edit REST endpoint."""
import json
import os
import tempfile
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest
from pmharness.drivers.base import DriverResponse

def _server():
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv

def _post(port, path, body, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=10)

def test_inline_edit_flow(monkeypatch, tmp_path):
    import harness.server as srv
    
    repo_dir = str(tmp_path)
    dummy_file = os.path.join(repo_dir, "test.py")
    with open(dummy_file, "w") as f:
        f.write("def foo():\n    pass\n")
        
    old_repo = srv._cfg.repo
    srv._cfg.repo = repo_dir
    
    assert srv._pilot is not None
    
    class MockPilot:
        def complete(self, prompt, system=None):
            return DriverResponse(text="```python\ndef foo():\n    return 'bar'\n```")
            
    old_pilot_inst = getattr(srv._pilot, "pilot", None)
    monkeypatch.setattr(srv._pilot, "pilot", MockPilot())
    
    httpd, port, _ = _server()
    headers = {
        "Content-Type": "application/json",
        "X-Harness-Token": srv._TOKEN
    }
    
    try:
        # (a) POST /api/inline-edit with a valid body returns {ok:True, edit:<text>}
        body = {
            "path": "test.py",
            "selection": "    pass",
            "instruction": "return 'bar'",
            "prefix": "def foo():\n",
            "suffix": "\n",
            "language": "python"
        }
        res = _post(port, "/api/inline-edit", body, headers)
        assert res.status == 200
        data = json.loads(res.read().decode())
        assert data["ok"] is True
        assert data["edit"] == "def foo():\n    return 'bar'"
        
        # (b) 403 without the token
        try:
            _post(port, "/api/inline-edit", body, {"Content-Type": "application/json"})
            assert False, "should have failed with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
            
        # (c) path traversal (path="../../etc/passwd") -> 400 / rejected.
        body_traversal = dict(body, path="../../etc/passwd")
        try:
            _post(port, "/api/inline-edit", body_traversal, headers)
            assert False, "should have failed with 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            
        # (d) oversized selection -> 400
        body_oversized = dict(body, selection="A" * 20001)
        try:
            _post(port, "/api/inline-edit", body_oversized, headers)
            assert False, "should have failed with 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            
        # (e) when the canned complete() returns resp.error set, the endpoint returns {ok:False, error:...} (not a crash).
        class MockErrorPilot:
            def complete(self, prompt, system=None):
                return DriverResponse(text="", error="Rate limit exceeded")
                
        monkeypatch.setattr(srv._pilot, "pilot", MockErrorPilot())
        res_err = _post(port, "/api/inline-edit", body, headers)
        assert res_err.status == 200
        data_err = json.loads(res_err.read().decode())
        assert data_err["ok"] is False
        assert "Rate limit" in data_err["error"]
        
    finally:
        httpd.shutdown()
        srv._cfg.repo = old_repo
        if old_pilot_inst is not None:
            srv._pilot.pilot = old_pilot_inst
