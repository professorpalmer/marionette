"""SSRF / redirect hardening for the HTTP MCP client."""
from __future__ import annotations

import http.server
import json
import threading
import urllib.error
import urllib.request
from typing import Optional

import pytest

from harness.mcp_http_client import (
    HttpMcpClient,
    SafeRedirectHandler,
    _sse_rpc_message,
)


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    """Local fixture server: /ok -> 200 JSON-RPC; /redir-safe -> 302 to /ok;
    /redir-meta -> 302 to metadata IP."""

    target_ok: str = ""
    target_meta: str = "http://169.254.169.254/latest/meta-data/"

    def log_message(self, format, *args):  # noqa: A003
        return

    def do_GET(self):
        # urllib converts POST+302 to GET on the redirect target.
        if self.path == "/ok":
            body = json.dumps(
                {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if self.path == "/ok":
            body = json.dumps(
                {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/redir-safe":
            self.send_response(302)
            self.send_header("Location", self.target_ok)
            self.end_headers()
            return
        if self.path == "/redir-meta":
            self.send_response(302)
            self.send_header("Location", self.target_meta)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()


@pytest.fixture
def local_mcp_server(monkeypatch):
    """Spin a loopback HTTP server; enable private-URL hatch for loopback."""
    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")
    server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    _RedirectHandler.target_ok = f"{base}/ok"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield base
    server.shutdown()
    server.server_close()


def test_plain_200_works(local_mcp_server):
    client = HttpMcpClient("t", f"{local_mcp_server}/ok")
    result = client._post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, timeout=5.0)
    assert result is not None
    assert result.get("result", {}).get("ok") is True


def test_safe_redirect_followed(local_mcp_server):
    client = HttpMcpClient("t", f"{local_mcp_server}/redir-safe")
    result = client._post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, timeout=5.0)
    assert result is not None
    assert result.get("result", {}).get("ok") is True


def test_blocked_redirect_raises(local_mcp_server):
    from harness.mcp_client import McpError

    client = HttpMcpClient("t", f"{local_mcp_server}/redir-meta")
    with pytest.raises(McpError) as exc:
        client._post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, timeout=5.0)
    assert str(exc.value) == "MCP server 't' HTTP 302"
    # The client message keeps only the status. The refusal is the cause.
    cause = exc.value.__context__
    assert isinstance(cause, urllib.error.HTTPError)
    assert cause.msg.startswith("redirect blocked: ")
    assert cause.url == _RedirectHandler.target_meta


def test_pinned_opener_includes_safe_redirect(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")

    def fake_getaddrinfo(host, port, *a, **kw):
        return [(2, 1, 6, "", ("127.0.0.1", port or 0))]

    monkeypatch.setattr("harness.url_safety.socket.getaddrinfo", fake_getaddrinfo)
    client = HttpMcpClient("t", "http://mcp.example.com/rpc")
    assert client._pinned_ip == "127.0.0.1"
    handler_types = [type(h) for h in client._opener.handlers]
    assert SafeRedirectHandler in handler_types


def test_redirect_updates_shared_pinned_ip(monkeypatch):
    """3xx hops must update the shared pin (same as web_tools), not keep the origin IP."""
    from harness.web_tools import _PinnedIP

    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")
    pin = _PinnedIP("10.0.0.1")

    def fake_getaddrinfo(host, port, *a, **kw):
        # Redirect target resolves to a different IP than the origin pin.
        return [(2, 1, 6, "", ("10.0.0.99", port or 0))]

    monkeypatch.setattr("harness.url_safety.socket.getaddrinfo", fake_getaddrinfo)
    handler = SafeRedirectHandler(pin=pin)
    handler.redirect_request(
        req=urllib.request.Request("http://mcp.example.com/rpc"),
        fp=None, code=302, msg="Found",
        headers={}, newurl="http://other.example.com/rpc",
    )
    assert pin.ip == "10.0.0.99"


def test_http_mcp_alive_clears_on_unreachable(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")

    def fake_getaddrinfo(host, port, *a, **kw):
        return [(2, 1, 6, "", ("127.0.0.1", port or 0))]

    monkeypatch.setattr("harness.url_safety.socket.getaddrinfo", fake_getaddrinfo)
    client = HttpMcpClient("t", "http://mcp.example.com/rpc")
    client._initialized = True
    assert client.alive is True

    class BoomOpener:
        def open(self, req, timeout=None):
            raise urllib.error.URLError("connection refused")

    client._opener = BoomOpener()
    from harness.mcp_client import McpError

    with pytest.raises(McpError):
        client._post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, timeout=1.0)
    assert client.alive is False


@pytest.fixture
def redirect_handler(monkeypatch):
    from harness.web_tools import _PinnedIP

    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")
    monkeypatch.setattr(
        "harness.url_safety.socket.getaddrinfo",
        lambda host, port, *a, **kw: [(2, 1, 6, "", ("10.0.0.99", port or 0))],
    )
    return SafeRedirectHandler(pin=_PinnedIP("10.0.0.1"))


def _credential_request(url, unredirected=False):
    req = urllib.request.Request(url, data=b"{}", method="POST")
    add = req.add_unredirected_header if unredirected else req.add_header
    for name in (
        "Authorization", "Mcp-Session-Id", "X-Arbitrary-Credential",
        "Cookie", "Proxy-Authorization",
    ):
        add(name, "test-placeholder")
    for name, value in {
        "Accept": "application/json, text/event-stream",
        "Accept-Encoding": "identity",
        "User-Agent": "test-client",
        "MCP-Protocol-Version": "2024-11-05",
        "Content-Type": "application/json",
        "Content-Length": "2",
        "Host": "mcp.example",
    }.items():
        add(name, value)
    return req


@pytest.mark.parametrize("unredirected", [False, True])
@pytest.mark.parametrize("source,target,same_origin", [
    ("https://mcp.example/rpc", "https://other.example/rpc", False),
    ("https://mcp.example/rpc", "http://mcp.example/rpc", False),
    ("https://mcp.example/rpc", "https://mcp.example:444/rpc", False),
    ("https://mcp.example/rpc", "https://mcp.example/next", True),
    ("https://MCP.example/rpc", "https://mcp.EXAMPLE:443/next", True),
    ("http://mcp.example:80/rpc", "http://mcp.example/next", True),
    ("https://mcp.example:444/rpc", "https://mcp.example:444/next", True),
    ("https://b\u00fccher.example/rpc", "https://xn--bcher-kva.example/next", True),
])
def test_redirect_header_boundary(redirect_handler, source, target, same_origin, unredirected):
    req = _credential_request(source, unredirected)
    original_headers = req.header_items()
    redirected = redirect_handler.redirect_request(req, None, 302, "Found", {}, target)
    names = {name.lower() for name, _ in redirected.header_items()}
    safe = {"accept", "accept-encoding", "user-agent", "mcp-protocol-version"}
    credentials = {
        "authorization", "mcp-session-id", "x-arbitrary-credential",
        "cookie", "proxy-authorization",
    }
    assert names == safe | (credentials if same_origin else set())
    assert redirected.get_method() == "GET"
    assert redirected.data is None
    assert req.header_items() == original_headers
    assert redirect_handler._pin.ip == "10.0.0.99"


@pytest.mark.parametrize("unredirected", [False, True])
def test_redirect_chain_never_restores_credentials(redirect_handler, unredirected):
    req = _credential_request("https://mcp.example/rpc", unredirected)
    for target in (
        "https://mcp.example/next", "https://other.example/rpc",
        "https://other.example/next", "https://mcp.example/back",
    ):
        req = redirect_handler.redirect_request(req, None, 302, "Found", {}, target)
        names = {name.lower() for name, _ in req.header_items()}
        assert ("authorization" in names) == (target == "https://mcp.example/next")
        if target != "https://mcp.example/next":
            assert names == {"accept", "accept-encoding", "user-agent", "mcp-protocol-version"}


def test_redirect_metadata_blocked_before_pin_update(redirect_handler):
    req = _credential_request("https://mcp.example/rpc")
    with pytest.raises(urllib.error.HTTPError, match="redirect blocked"):
        redirect_handler.redirect_request(
            req, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/",
        )
    assert redirect_handler._pin.ip == "10.0.0.1"


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_get_statuses_strip_credentials(redirect_handler, code):
    req = _credential_request("https://mcp.example/rpc")
    req.data = None
    req.method = "GET"
    redirected = redirect_handler.redirect_request(
        req, None, code, "Redirect", {}, "https://other.example/rpc",
    )
    assert {name.lower() for name, _ in redirected.header_items()} == {
        "accept", "accept-encoding", "user-agent", "mcp-protocol-version",
    }


@pytest.mark.parametrize("code", [307, 308])
def test_redirect_post_method_rules_unchanged(redirect_handler, code):
    req = _credential_request("https://mcp.example/rpc")
    with pytest.raises(urllib.error.HTTPError):
        redirect_handler.redirect_request(
            req, None, code, "Redirect", {}, "https://other.example/rpc",
        )


def test_redirect_unredirected_header_precedence(redirect_handler):
    req = _credential_request("https://mcp.example/rpc")
    req.add_unredirected_header("Authorization", "test-preferred")
    req.add_unredirected_header("Accept", "application/json")
    redirected = redirect_handler.redirect_request(
        req, None, 302, "Found", {}, "https://mcp.example/next",
    )
    # AbstractHTTPHandler.do_open gives unredirected headers precedence.
    assert redirected.get_header("Authorization") == "test-preferred"
    assert redirected.get_header("Accept") == "application/json"
    redirected = redirect_handler.redirect_request(
        redirected, None, 302, "Found", {}, "https://other.example/next",
    )
    assert not redirected.has_header("Authorization")
    assert redirected.get_header("Accept") == "application/json"


def test_sse_rpc_message_skips_trailing_progress_notification():
    raw = (
        'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"ping"}]}}\n\n'
        'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"p":1}}\n\n'
    )
    msg = _sse_rpc_message(raw)
    assert msg is not None
    assert msg.get("result", {}).get("tools")[0]["name"] == "ping"


def test_sse_rpc_message_joins_multiline_data_field():
    raw = (
        'data: {"jsonrpc":"2.0","id":2,\n'
        'data: "result":{"ok":true}}\n\n'
    )
    msg = _sse_rpc_message(raw)
    assert msg == {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}}


def test_sse_rpc_message_single_result_still_wins():
    raw = 'data: {"jsonrpc":"2.0","id":3,"result":{"ok":true}}\n\n'
    assert _sse_rpc_message(raw)["result"]["ok"] is True
