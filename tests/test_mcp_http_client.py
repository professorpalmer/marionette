"""SSRF / redirect hardening for the HTTP MCP client."""
from __future__ import annotations

import http.server
import json
import threading
import urllib.error
import urllib.request
from typing import Optional

import pytest

from harness.mcp_http_client import HttpMcpClient, SafeRedirectHandler


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
    msg = str(exc.value).lower()
    assert "redirect" in msg or "blocked" in msg or "http" in msg


def test_unresolvable_hostname_still_gets_redirect_handler(monkeypatch):
    """When DNS fails, opener must still include SafeRedirectHandler."""
    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")

    def boom(host, port, *a, **kw):
        raise OSError("name resolution failed")

    monkeypatch.setattr("harness.url_safety.socket.getaddrinfo", boom)
    client = HttpMcpClient("t", "http://does-not-resolve.invalid/rpc")
    assert client._pinned_ip is None
    assert client._opener is not None
    handler_types = [type(h) for h in client._opener.handlers]
    assert SafeRedirectHandler in handler_types


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
    # Avoid actually building a follow-up Request; we only care about pin update.
    monkeypatch.setattr(
        urllib.request.HTTPRedirectHandler,
        "redirect_request",
        lambda self, *a, **k: None,
    )
    handler.redirect_request(
        req=None, fp=None, code=302, msg="Found",
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
