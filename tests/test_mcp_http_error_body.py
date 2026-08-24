"""MCP HTTP errors keep status/code and drop raw response bodies."""
from __future__ import annotations

import http.server
import threading

import pytest

from harness.mcp_client import McpError
from harness.mcp_http_client import HttpMcpClient


class _SecretHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = b"sk-abc123456789 leaked ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        self.send_response(403)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _NonJsonHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = b"not-json sk-abc123456789"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler):
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_http_error_keeps_status_and_drops_secret_body(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")
    server = _serve(_SecretHandler)
    try:
        client = HttpMcpClient("sec", f"http://127.0.0.1:{server.server_address[1]}/rpc")
        with pytest.raises(McpError) as exc:
            client._post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, timeout=5.0)
        msg = str(exc.value)
        assert "HTTP 403" in msg
        assert "sk-abc" not in msg
        assert "ghp_" not in msg
    finally:
        server.shutdown()
        server.server_close()


def test_non_json_response_does_not_echo_body(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")
    server = _serve(_NonJsonHandler)
    try:
        client = HttpMcpClient("nj", f"http://127.0.0.1:{server.server_address[1]}/rpc")
        with pytest.raises(McpError) as exc:
            client._post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, timeout=5.0)
        msg = str(exc.value)
        assert "non-JSON" in msg
        assert "sk-abc" not in msg
    finally:
        server.shutdown()
        server.server_close()
