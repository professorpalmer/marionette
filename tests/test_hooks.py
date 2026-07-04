import os
import json
import shutil
import tempfile
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest
from harness import hooks as _hk


def _server():
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _get(port, path, headers=None):
    # GET endpoints now require the auth token (centralized do_GET gate), same as
    # POST. Default it in so existing GET calls stay authenticated.
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


def test_hooks_module_and_endpoints():
    tmp_dir = tempfile.mkdtemp()
    original_hooks_json = _hk._HOOKS_JSON
    # Point the hooks module to our temporary file
    _hk._HOOKS_JSON = os.path.join(tmp_dir, "hooks.json")
    
    httpd, port, srv = _server()
    
    try:
        # 1. Test direct python list / empty state
        assert _hk.get_hooks() == []
        
        # 2. Test direct python run_hooks with a harmless command like 'true'
        hooks_list = [{
            "id": "h1",
            "event": "preRun",
            "command": "echo 'hello from test_hooks' > " + os.path.join(tmp_dir, "hook_out.txt"),
            "enabled": True
        }]
        _hk.save_hooks(hooks_list)
        
        _hk.run_hooks("preRun", {"test_key": "test_val"})
        
        # Verify the command ran
        out_file = os.path.join(tmp_dir, "hook_out.txt")
        assert os.path.exists(out_file)
        with open(out_file, "r") as f:
            content = f.read().strip()
        assert content == "hello from test_hooks"
        
        # 3. Test that a failing hook does NOT crash or raise an exception
        failing_hooks = [{
            "id": "h2",
            "event": "postRun",
            "command": "non_existent_command_12345",
            "enabled": True
        }]
        _hk.save_hooks(failing_hooks)
        # Should not throw any exception
        _hk.run_hooks("postRun", {})
        
        # 4. Clear/reset for HTTP endpoint testing
        _hk.save_hooks([])
        
        # 5. Test endpoint GET /api/hooks
        resp = _get(port, "/api/hooks")
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert "hooks" in data
        assert "events" in data
        assert data["hooks"] == []
        assert "preRun" in data["events"]
        
        # 6. Test endpoint POST /api/hooks/add
        post_headers = {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN}
        resp = _post(port, "/api/hooks/add", {"event": "preRun", "command": "echo 'hi'"}, post_headers)
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["event"] == "preRun"
        assert data["command"] == "echo 'hi'"
        assert data["enabled"] is True
        assert "id" in data
        hid = data["id"]
        
        # Verify lists again
        resp = _get(port, "/api/hooks")
        data = json.loads(resp.read().decode())
        assert len(data["hooks"]) == 1
        assert data["hooks"][0]["id"] == hid
        
        # 7. Test invalid event name rejection (should be 400)
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(port, "/api/hooks/add", {"event": "invalidEvent", "command": "echo 'fail'"}, post_headers)
        assert excinfo.value.code == 400
        
        # 8. Test endpoint POST /api/hooks/update
        resp = _post(port, "/api/hooks/update", {"id": hid, "enabled": False}, post_headers)
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["enabled"] is False
        
        # Verify updated in list
        resp = _get(port, "/api/hooks")
        data = json.loads(resp.read().decode())
        assert data["hooks"][0]["enabled"] is False
        
        # 9. Test endpoint POST /api/hooks/remove
        resp = _post(port, "/api/hooks/remove", {"id": hid}, post_headers)
        assert resp.status == 200
        
        # Verify empty again
        resp = _get(port, "/api/hooks")
        data = json.loads(resp.read().decode())
        assert data["hooks"] == []
        
        # 10. Test security / API token protection on POST endpoints (should be 403)
        no_token_headers = {"Content-Type": "application/json"}
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(port, "/api/hooks/add", {"event": "preRun", "command": "echo 'no token'"}, no_token_headers)
        assert excinfo.value.code == 403
        
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(port, "/api/hooks/update", {"id": hid, "enabled": True}, no_token_headers)
        assert excinfo.value.code == 403
        
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(port, "/api/hooks/remove", {"id": hid}, no_token_headers)
        assert excinfo.value.code == 403
        
    finally:
        httpd.shutdown()
        # Restore original hooks path
        _hk._HOOKS_JSON = original_hooks_json
        shutil.rmtree(tmp_dir, ignore_errors=True)
