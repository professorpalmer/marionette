"""Security: the local server must reject cross-origin / rebound / unauthenticated
requests on mutating endpoints (the RCE fix)."""
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


def _post(port, path, body, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=10)


def test_mcp_add_rejected_without_token():
    httpd, port, srv = _server()
    try:
        try:
            _post(port, "/api/mcp/add", {"name": "evil", "command": "touch", "args": ["/tmp/pwned_pmharness"]},
                  {"Content-Type": "application/json"})
            assert False, "should have been rejected"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()


def test_mcp_add_rejected_cross_origin_even_with_token():
    httpd, port, srv = _server()
    try:
        try:
            _post(port, "/api/mcp/add", {"name": "evil", "command": "touch", "args": ["/tmp/x"]},
                  {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN,
                   "Origin": "https://evil.com"})
            assert False, "cross-origin should be rejected"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()


def test_rebind_host_rejected():
    httpd, port, srv = _server()
    try:
        try:
            _post(port, "/api/mcp/add", {"name": "evil", "command": "touch", "args": ["/tmp/x"]},
                  {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN,
                   "Host": "evil.attacker.com"})
            assert False, "non-loopback Host should be rejected"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()


def test_legit_request_with_token_allowed():
    httpd, port, srv = _server()
    try:
        # remove is harmless + idempotent; proves a properly-tokened same-origin call passes the guard
        r = _post(port, "/api/mcp/remove", {"name": "nonexistent"},
                  {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert r.status == 200
    finally:
        httpd.shutdown()


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {}, method="GET")
    return urllib.request.urlopen(req, timeout=10)


def test_pilot_swap_requires_token():
    httpd, port, srv = _server()
    try:
        # GET /api/pilot?model=... without token -> 403
        try:
            _get(port, "/api/pilot?model=glm-5.2")
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # GET /api/pilot?model=... with bad token -> 403
        try:
            _get(port, "/api/pilot?model=glm-5.2&token=bad-token")
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # GET /api/pilot?model=... with valid token -> 200 and model changed
        resp = _get(port, f"/api/pilot?model=glm-5.2&token={srv._TOKEN}")
        assert resp.status == 200
        assert srv._cfg.driver == "glm-5.2"
    finally:
        httpd.shutdown()


def test_sensitive_gets_require_token():
    httpd, port, srv = _server()
    try:
        # GET /api/sessions/transcript without token -> 403
        try:
            _get(port, "/api/sessions/transcript?session=foo")
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # GET /api/sessions/transcript with token -> 200
        resp = _get(port, f"/api/sessions/transcript?session=foo&token={srv._TOKEN}")
        assert resp.status == 200

        # GET /api/sessions/export without token -> 403
        try:
            _get(port, "/api/sessions/export?session=foo")
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # GET /api/sessions/export with token -> 200
        resp = _get(port, f"/api/sessions/export?session=foo&token={srv._TOKEN}")
        assert resp.status == 200
    finally:
        httpd.shutdown()


def test_mcp_url_ssrf_blocking(monkeypatch):
    monkeypatch.delenv("PMHARNESS_MCP_ALLOW_PRIVATE", raising=False)
    from harness.mcp_http_client import HttpMcpClient
    from harness.mcp_client import McpError

    # 1. 169.254.169.254 should be rejected
    with pytest.raises(McpError) as exc:
        HttpMcpClient("test", "http://169.254.169.254/")
    assert "blocked" in str(exc.value)

    # 2. Localhost IP 127.0.0.1 should be rejected
    with pytest.raises(McpError) as exc:
        HttpMcpClient("test", "http://127.0.0.1:1/")
    assert "blocked" in str(exc.value)

    # 3. Localhost hostname should be rejected
    with pytest.raises(McpError) as exc:
        HttpMcpClient("test", "http://localhost:8000/rpc")
    assert "blocked" in str(exc.value)

    # 4. Valid public domain should be allowed by validator
    client = HttpMcpClient("test", "https://example.com/mcp")
    assert client.url == "https://example.com/mcp"

    # 5. With PMHARNESS_MCP_ALLOW_PRIVATE=1, local IP should be allowed
    import os
    os.environ["PMHARNESS_MCP_ALLOW_PRIVATE"] = "1"
    try:
        client = HttpMcpClient("test", "http://127.0.0.1:1/")
        assert client.url == "http://127.0.0.1:1/"
    finally:
        del os.environ["PMHARNESS_MCP_ALLOW_PRIVATE"]


def test_api_run_image_path_traversal_blocked():
    import os
    httpd, port, srv = _server()
    try:
        # Request /api/run with an image path outside upload directory
        url = f"/api/run?prompt=hello&images=/etc/hosts&token={srv._TOKEN}"
        try:
            _get(port, url)
            assert False, "should have been rejected with 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            data = json.loads(e.read().decode())
            assert "Invalid image path" in data["error"]

        # Request with a path inside the upload directory (should pass validation gate)
        temp_img_path = os.path.join(srv._UPLOAD_DIR, "test.png")
        with open(temp_img_path, "wb") as f:
            f.write(b"fake png content")
            
        try:
            url_ok = f"/api/run?prompt=hello&images={temp_img_path}&token={srv._TOKEN}"
            resp = _get(port, url_ok)
            assert resp.status == 200
        finally:
            try:
                os.remove(temp_img_path)
            except Exception:
                pass
    finally:
        httpd.shutdown()


def test_settings_rejected_when_pilot_busy():
    httpd, port, srv = _server()
    import harness.providers
    orig_av = harness.providers.available_pilots
    try:
        harness.providers.available_pilots = lambda: ["qwen3-coder-30b", "glm-5.2"]

        # Set current driver to glm-5.2 so that swapping to qwen3-coder-30b triggers a rebuild
        srv._cfg.driver = "glm-5.2"
        srv._rebuild_pilot_and_session()

        # Simulate a busy pilot by acquiring its lock
        srv._pilot._busy.acquire()
        
        # Now make a settings POST request that requires rebuild (driver change)
        try:
            body = {"driver": "qwen3-coder-30b"}
            _post(port, "/api/settings", body, {
                "Content-Type": "application/json",
                "X-Harness-Token": srv._TOKEN
            })
            assert False, "should have been rejected with 409"
        except urllib.error.HTTPError as e:
            assert e.code == 409
            data = json.loads(e.read().decode())
            assert "pilot busy" in data["error"]
            
        # A settings POST request that does NOT require rebuild (e.g., budget change)
        # should still pass even if pilot is busy!
        body_budget = {"budget": 5}
        resp = _post(port, "/api/settings", body_budget, {
            "Content-Type": "application/json",
            "X-Harness-Token": srv._TOKEN
        })
        assert resp.status == 200
        assert srv._cfg.budget == 5
        
        # Release lock
        srv._pilot._busy.release()
        
        # Now the driver change should succeed!
        body_driver = {"driver": "qwen3-coder-30b"}
        try:
            resp = _post(port, "/api/settings", body_driver, {
                "Content-Type": "application/json",
                "X-Harness-Token": srv._TOKEN
            })
            assert resp.status == 200
            assert srv._cfg.driver == "qwen3-coder-30b"
        except urllib.error.HTTPError as e:
            body_err = e.read().decode()
            print("ERROR BODY:", body_err)
            raise
        
    finally:
        harness.providers.available_pilots = orig_av
        httpd.shutdown()


def test_low_level_security_and_strict_parsing():
    httpd, port, srv = _server()
    try:
        # 1. Malformed JSON to /api/settings should return 400 "invalid JSON"
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/settings",
            data=b"this is { not valid json",
            headers={"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
            method="POST"
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "should have failed with 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            data = json.loads(e.read().decode())
            assert "invalid JSON" in data["error"]

        # 2. /api/mcp/call with non-dict arguments should return 400
        body_mcp_bad = {"tool": "fake.echo", "arguments": "not-a-dict"}
        try:
            _post(port, "/api/mcp/call", body_mcp_bad, {
                "Content-Type": "application/json",
                "X-Harness-Token": srv._TOKEN
            })
            assert False, "should have failed with 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            data = json.loads(e.read().decode())
            assert "arguments must be a dictionary" in data["error"]

        # 3. Test _parse_bool helper directly
        from harness.server import _parse_bool
        assert _parse_bool(True) is True
        assert _parse_bool(False) is False
        assert _parse_bool("true") is True
        assert _parse_bool("TRUE") is True
        assert _parse_bool("1") is True
        assert _parse_bool("yes") is True
        assert _parse_bool("on") is True
        assert _parse_bool("false") is False
        assert _parse_bool("0") is False
        assert _parse_bool("no") is False
        assert _parse_bool("off") is False
        assert _parse_bool(None) is False
        assert _parse_bool([]) is False

    finally:
        httpd.shutdown()


def test_context_usage_security_and_api():
    httpd, port, srv = _server()
    try:
        # 1. Without token -> 403
        try:
            _get(port, "/api/context/usage")
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # 2. With bad token -> 403
        try:
            _get(port, "/api/context/usage?token=bad-token")
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # 3. With good token -> 200 and valid breakdown
        resp = _get(port, f"/api/context/usage?token={srv._TOKEN}")
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert "total" in data
        assert "limit" in data
        assert "categories" in data
        
        cats = {c["name"]: c["tokens"] for c in data["categories"]}
        assert "System prompt" in cats
        assert "Conversation" in cats
    finally:
        httpd.shutdown()
