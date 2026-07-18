"""Security/robustness hardening for MCP clients (malicious / buggy servers)."""
from __future__ import annotations

import http.server
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from harness.mcp_client import MCP_MAX_RESPONSE_BYTES, McpError, StdioMcpClient
from harness.mcp_http_client import HttpMcpClient, SafeRedirectHandler

FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")
FAKE_SLOW = str(Path(__file__).parent / "fixtures" / "fake_mcp_server_slow.py")


# -- P3a: response size caps -------------------------------------------------


class _OversizedHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        # Body larger than the (possibly monkeypatched) cap.
        from harness import mcp_http_client as http_mod

        cap = http_mod.MCP_MAX_RESPONSE_BYTES
        body = b"x" * (cap + 64)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_http_oversized_response_errors_cleanly(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")
    monkeypatch.setattr("harness.mcp_client.MCP_MAX_RESPONSE_BYTES", 4096)
    monkeypatch.setattr("harness.mcp_http_client.MCP_MAX_RESPONSE_BYTES", 4096)

    server = http.server.HTTPServer(("127.0.0.1", 0), _OversizedHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = HttpMcpClient("huge", f"http://127.0.0.1:{port}/rpc")
        with pytest.raises(McpError) as exc:
            client._post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, timeout=5.0)
        assert "exceeded" in str(exc.value).lower()
    finally:
        server.shutdown()
        server.server_close()


def test_stdio_oversized_response_errors_cleanly(tmp_path, monkeypatch):
    monkeypatch.setattr("harness.mcp_client.MCP_MAX_RESPONSE_BYTES", 2048)
    # Emit a single oversized JSON-RPC line for tools/call; handshake stays small.
    script = tmp_path / "huge_mcp.py"
    script.write_text(
        '''
import json, sys
CAP = 2048
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": mid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "huge", "version": "1.0"},
            },
        }) + "\\n")
        sys.stdout.flush()
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": mid,
            "result": {"tools": [{"name": "boom", "description": "x", "inputSchema": {}}]},
        }) + "\\n")
        sys.stdout.flush()
    elif method == "tools/call":
        pad = "y" * (CAP + 128)
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": mid,
            "result": {"content": [{"type": "text", "text": pad}]},
        }) + "\\n")
        sys.stdout.flush()
'''.lstrip(),
        encoding="utf-8",
    )
    c = StdioMcpClient(name="huge", command=sys.executable, args=[str(script)])
    c.start()
    try:
        with pytest.raises(McpError) as exc:
            c.call_tool("boom", {}, timeout=5.0)
        assert "exceeded" in str(exc.value).lower()
    finally:
        c.stop()


# -- P3b: stdio lock not held across blocking read ---------------------------


def test_hung_stdio_response_does_not_block_concurrent_write():
    c = StdioMcpClient(name="slow", command=sys.executable, args=[FAKE_SLOW])
    c.start()
    try:
        hung = []

        def _hung_call():
            try:
                c.call_tool("slow", {"seconds": 30}, timeout=4.0)
            except McpError as e:
                hung.append(e)

        t = threading.Thread(target=_hung_call, daemon=True)
        t.start()
        time.sleep(0.4)  # first call has written and is waiting outside the lock

        t0 = time.time()
        acquired = c._lock.acquire(timeout=1.0)
        elapsed = time.time() - t0
        assert acquired, "stdio lock still held across blocking read"
        assert elapsed < 0.5, f"lock acquire took too long: {elapsed:.3f}s"
        # Concurrent write must also complete quickly while peer is hung.
        c._send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
        )
        c._lock.release()
        t.join(timeout=6.0)
        assert hung, "hung call should have timed out with McpError"
    finally:
        c.stop()


# -- P3c: unresolvable hostname fail-closed ----------------------------------


def test_unresolvable_hostname_blocked_when_private_disallowed(monkeypatch):
    monkeypatch.delenv("HARNESS_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.setenv("PMHARNESS_MCP_ALLOW_PRIVATE", "0")

    def boom(host, port, *a, **kw):
        raise OSError("name resolution failed")

    monkeypatch.setattr("harness.url_safety.socket.getaddrinfo", boom)
    with pytest.raises(McpError) as exc:
        HttpMcpClient("t", "http://does-not-resolve.invalid/rpc")
    msg = str(exc.value).lower()
    assert "could not be resolved" in msg or "unsafe" in msg


def test_unresolvable_hostname_allowed_when_private_allowed(monkeypatch):
    """Private/loopback MCP hatch keeps DNS-miss path (no pinned IP)."""
    monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")

    def boom(host, port, *a, **kw):
        raise OSError("name resolution failed")

    monkeypatch.setattr("harness.url_safety.socket.getaddrinfo", boom)
    client = HttpMcpClient("t", "http://does-not-resolve.invalid/rpc")
    assert client._pinned_ip is None
    handler_types = [type(h) for h in client._opener.handlers]
    assert SafeRedirectHandler in handler_types


def test_mcp_max_response_bytes_shared_constant():
    assert MCP_MAX_RESPONSE_BYTES == 16 * 1024 * 1024
    from harness import mcp_http_client as http_mod

    assert http_mod.MCP_MAX_RESPONSE_BYTES == MCP_MAX_RESPONSE_BYTES
