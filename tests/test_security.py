"""Security: the local server must reject cross-origin / rebound / unauthenticated
requests on mutating endpoints (the RCE fix)."""
import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.usefixtures("owned_server")


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
            _get(
                port,
                "/api/pilot?model=glm-5.2",
                headers={"X-Harness-Token": "bad-token"},
            )
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # A model mutation must identify the owning active session.
        session_id = srv._sessions.active
        resp = _get(
            port,
            f"/api/pilot?model=glm-5.2&session_id={session_id}",
            headers={"X-Harness-Token": srv._TOKEN},
        )
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
        resp = _get(
            port,
            "/api/sessions/transcript?session=foo",
            headers={"X-Harness-Token": srv._TOKEN},
        )
        assert resp.status == 200

        # GET /api/sessions/export without token -> 403
        try:
            _get(port, "/api/sessions/export?session=foo")
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # GET /api/sessions/export with token -> 200
        resp = _get(
            port,
            "/api/sessions/export?session=foo",
            headers={"X-Harness-Token": srv._TOKEN},
        )
        assert resp.status == 200
    finally:
        httpd.shutdown()


def test_mcp_url_ssrf_blocking(monkeypatch):
    # Default: user-configured MCP may use loopback/LAN (Docker). Metadata
    # stays blocked. Opt out with PMHARNESS_MCP_ALLOW_PRIVATE=0.
    monkeypatch.delenv("PMHARNESS_MCP_ALLOW_PRIVATE", raising=False)
    monkeypatch.delenv("HARNESS_ALLOW_PRIVATE_URLS", raising=False)
    from harness.mcp_http_client import HttpMcpClient
    from harness.mcp_client import McpError

    # 1. Cloud metadata always rejected
    with pytest.raises(McpError) as exc:
        HttpMcpClient("test", "http://169.254.169.254/")
    assert "blocked" in str(exc.value)

    # 2. Loopback allowed by default (Docker discord-mcp etc.)
    client = HttpMcpClient("test", "http://127.0.0.1:1/")
    assert client.url == "http://127.0.0.1:1/"
    client = HttpMcpClient("test", "http://localhost:8000/rpc")
    assert client.url == "http://localhost:8000/rpc"

    # 3. Public domain still allowed
    client = HttpMcpClient("test", "https://example.com/mcp")
    assert client.url == "https://example.com/mcp"

    # 4. Opt-out restores strict SSRF (loopback blocked)
    monkeypatch.setenv("PMHARNESS_MCP_ALLOW_PRIVATE", "0")
    with pytest.raises(McpError) as exc:
        HttpMcpClient("test", "http://127.0.0.1:1/")
    assert "blocked" in str(exc.value)
    with pytest.raises(McpError) as exc:
        HttpMcpClient("test", "http://localhost:8000/rpc")
    assert "blocked" in str(exc.value)
    monkeypatch.delenv("PMHARNESS_MCP_ALLOW_PRIVATE")

    # 5. Metadata stays blocked even when private MCP is allowed
    monkeypatch.setenv("PMHARNESS_MCP_ALLOW_PRIVATE", "1")
    for bad in (
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://metadata/latest/meta-data/",
        "http://[fd00:ec2::254]/latest/meta-data/",
    ):
        with pytest.raises(McpError):
            HttpMcpClient("test", bad)
    monkeypatch.delenv("PMHARNESS_MCP_ALLOW_PRIVATE")

    # 6. Rig-wide hatch still opens the client when MCP opt-out is set
    monkeypatch.setenv("PMHARNESS_MCP_ALLOW_PRIVATE", "0")
    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")
    client = HttpMcpClient("test", "http://127.0.0.1:1/")
    assert client.url == "http://127.0.0.1:1/"
    monkeypatch.delenv("HARNESS_ALLOW_PRIVATE_URLS")
    monkeypatch.delenv("PMHARNESS_MCP_ALLOW_PRIVATE")


def test_host_ok_loopback_forms():
    """The DNS-rebinding Host check must accept every literal loopback form
    (with and without a port, IPv6 bracketed) and reject anything else."""
    import harness.server as srv

    for good in ("127.0.0.1", "127.0.0.1:8000", "localhost", "localhost:53218",
                 "[::1]", "[::1]:8000"):
        assert srv._host_ok(good), good
    for bad in ("", "evil.com", "evil.com:8000", "127.0.0.1.evil.com",
                "[::2]:8000", "localhost.evil.com"):
        assert not srv._host_ok(bad), bad


def test_api_run_image_path_traversal_blocked():
    import os
    httpd, port, srv = _server()
    try:
        # Request /api/run with an image path outside upload directory
        try:
            _get(
                port,
                "/api/run?prompt=hello&images=/etc/hosts",
                headers={"X-Harness-Token": srv._TOKEN},
            )
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
            resp = _get(
                port,
                f"/api/run?prompt=hello&images={temp_img_path}",
                headers={"X-Harness-Token": srv._TOKEN},
            )
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
    orig_srv_av = srv._available_pilots
    try:
        harness.providers.available_pilots = lambda: ["qwen3-coder-30b", "glm-5.2"]
        # Validation reads the picker list via server._available_pilots(); make
        # qwen3-coder-30b a valid target so the test exercises the busy-409 gate
        # rather than the driver-validation 400.
        srv._available_pilots = lambda: ["qwen3-coder-30b", "glm-5.2"]

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
        srv._available_pilots = orig_srv_av
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
            _get(
                port,
                "/api/context/usage",
                headers={"X-Harness-Token": "bad-token"},
            )
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # 3. With good token -> 200 and valid breakdown
        resp = _get(
            port,
            "/api/context/usage",
            headers={"X-Harness-Token": srv._TOKEN},
        )
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


def test_api_chat_multi_image_path_traversal_blocked():
    import os
    import urllib.parse
    httpd, port, srv = _server()
    try:
        # 1. Request /api/chat with multiple images, one of which is outside upload_dir
        temp_img_path = os.path.join(srv._UPLOAD_DIR, "valid_test.png")
        with open(temp_img_path, "wb") as f:
            f.write(b"fake png content")

        bad_images = f"{temp_img_path}|/etc/hosts"
        try:
            _get(
                port,
                f"/api/chat?message=hello&images={urllib.parse.quote(bad_images)}",
                headers={"X-Harness-Token": srv._TOKEN},
            )
            assert False, "should have been rejected with 400 due to traversal image"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            data = json.loads(e.read().decode())
            assert "Invalid image path" in data["error"]

        # 2. Request /api/chat with multiple valid images
        temp_img_path2 = os.path.join(srv._UPLOAD_DIR, "valid_test2.png")
        with open(temp_img_path2, "wb") as f:
            f.write(b"fake png content 2")

        good_images = f"{temp_img_path}|{temp_img_path2}"
        # We mock _stream_chat to avoid running actual VLM/Pilot during this security test
        original_stream_chat = srv.Handler._stream_chat
        called_with_imgs = []
        def mock_stream_chat(handler_self, message, images=None, plan=False, resume=False):
            called_with_imgs.append(images)
            handler_self.send_response(200)
            handler_self.end_headers()
            handler_self.wfile.write(b"ok")

        srv.Handler._stream_chat = mock_stream_chat
        try:
            resp = _get(
                port,
                f"/api/chat?message=hello&images={urllib.parse.quote(good_images)}",
                headers={"X-Harness-Token": srv._TOKEN},
            )
            assert resp.status == 200
            assert called_with_imgs == [[temp_img_path, temp_img_path2]]
        finally:
            srv.Handler._stream_chat = original_stream_chat
            try:
                os.remove(temp_img_path)
            except Exception:
                pass
            try:
                os.remove(temp_img_path2)
            except Exception:
                pass
    finally:
        httpd.shutdown()


def test_external_urls_never_receive_auth_header():
    """The token-injection filter must cover loopback only, asserted against the
    REAL interceptor in webapp/electron/main.cjs.

    This used to re-implement Electron's regexes in Python, which meant it kept
    passing even if main.cjs regressed to injecting X-Harness-Token into
    non-loopback requests -- a documentation test masquerading as a guard. It
    now reads the shipped source, so the assertion can actually fail.
    """
    import re
    from pathlib import Path

    main_cjs = (
        Path(__file__).resolve().parents[1] / "webapp" / "electron" / "main.cjs"
    ).read_text(encoding="utf-8")

    # Bind the filter to the site that actually injects the token.
    m = re.search(
        r"onBeforeSendHeaders\(\s*\{\s*urls:\s*\[(.*?)\]\s*\},"
        r"(.*?)harnessToken",
        main_cjs,
        re.S,
    )
    assert m, (
        "could not find the onBeforeSendHeaders token-injection block in main.cjs "
        "-- if the interceptor moved, update this guard rather than deleting it"
    )
    patterns = re.findall(r'"([^"]+)"', m.group(1))
    assert patterns, "the loopback url filter is empty"

    loopback = ("127.0.0.1", "localhost", "[::1]")
    for pattern in patterns:
        assert any(host in pattern for host in loopback), (
            f"token-injection filter covers a non-loopback origin: {pattern}"
        )

    # And the filter, expressed as a regex, must not admit a foreign host.
    for pattern in patterns:
        rx = re.compile("^" + re.escape(pattern).replace(r"\*", ".*"))
        for external in (
            "http://example.com/api/chat",
            "http://attacker.com:8000/api/image",
            "https://malicious.site/api/export",
            "http://192.168.1.100:8000/api/run",
        ):
            assert not rx.match(external), f"{external} must not match {pattern}"
        # Each loopback URL must be admitted by at least one pattern.
        for internal in (
            "http://127.0.0.1:8000/api/chat",
            "http://localhost:8000/api/export",
            "http://[::1]:8000/api/run",
        ):
            assert any(
                internal.startswith(p2.replace("*/*", "").rstrip("*")) for p2 in patterns
            ), internal
