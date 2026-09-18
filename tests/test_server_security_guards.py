"""Regression guards for the local-API trust boundary.

Companion to tests/test_security.py. That module proves the headline rejections
(no token, cross-origin, rebound Host); this one pins the subtler edges that the
Fort Knox hardening pass either fixed or found untested:

* `Origin: null` (Electron file:// / a sandboxed iframe) must be *accepted* but
  must NEVER be reflected as Access-Control-Allow-Origin -- otherwise any page
  could read the local API's responses from a sandboxed frame.
* Host matching must reject lookalikes such as 127.0.0.1.evil.com, not just
  obviously-foreign names.
* Query-string tokens stay rejected on every path except the tightly-scoped
  legacy streaming shim, whose own matrix is asserted directly.
* Duplicate identity headers (CL/CL, CL/TE) are refused as smuggling attempts.
* The token file is only "secure" when the observed mode is owner-only -- a
  chmod that silently did not take must be reported, not assumed.
"""
import json
import os
import re
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("owned_server")


def _server():
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _get(port, path, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers=headers or {}, method="GET",
    )
    return urllib.request.urlopen(req, timeout=10)


def _status(port, path, headers=None):
    """Return (status, headers) without raising on 4xx."""
    try:
        r = _get(port, path, headers)
        return r.status, r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.headers


# --- CORS / Origin ----------------------------------------------------------

def test_origin_null_is_accepted_but_never_reflected():
    """Electron's file:// renderer sends Origin: null and must keep working.

    The same value is what a sandboxed iframe sends, so the response must carry
    no Access-Control-Allow-Origin and stay unreadable cross-origin.
    """
    httpd, port, srv = _server()
    try:
        status, headers = _status(
            port, "/api/mcp",
            {"X-Harness-Token": srv._TOKEN, "Origin": "null"},
        )
        assert status == 200
        assert headers.get("Access-Control-Allow-Origin") is None
    finally:
        httpd.shutdown()


def test_loopback_origin_is_reflected():
    httpd, port, srv = _server()
    try:
        origin = "http://127.0.0.1:9999"
        status, headers = _status(
            port, "/api/mcp",
            {"X-Harness-Token": srv._TOKEN, "Origin": origin},
        )
        assert status == 200
        assert headers.get("Access-Control-Allow-Origin") == origin
    finally:
        httpd.shutdown()


def test_foreign_origin_is_rejected_even_with_a_valid_token():
    httpd, port, srv = _server()
    try:
        status, _ = _status(
            port, "/api/mcp",
            {"X-Harness-Token": srv._TOKEN, "Origin": "https://evil.example"},
        )
        assert status == 403
    finally:
        httpd.shutdown()


# --- Host (DNS rebinding) ---------------------------------------------------

@pytest.mark.parametrize("host", [
    "evil.com",
    "127.0.0.1.evil.com",
    "localhost.evil.com",
    "evil.com:127.0.0.1",
    "",
])
def test_host_lookalikes_are_rejected(host):
    httpd, port, srv = _server()
    try:
        headers = {"X-Harness-Token": srv._TOKEN}
        if host:
            headers["Host"] = host
        else:
            headers["Host"] = ""
        status, _ = _status(port, "/api/mcp", headers)
        assert status == 403
    finally:
        httpd.shutdown()


# --- query-string tokens ----------------------------------------------------

def test_query_token_rejected_on_non_legacy_path():
    """Only the legacy streaming GETs may carry the token in the URL."""
    httpd, port, srv = _server()
    try:
        status, _ = _status(port, f"/api/mcp?token={srv._TOKEN}")
        assert status == 403
    finally:
        httpd.shutdown()


def test_legacy_stream_query_token_matrix():
    """Pin the exact shape of the temporary compatibility shim.

    It exists so pre-v0.9.95 installed Electron shells keep streaming; every
    axis below is part of why it is not a general second auth path.
    """
    import harness.server as srv
    token = "t" * 32
    ok = srv.legacy_stream_query_token_ok

    assert ok(method="GET", path="/api/chat", query=f"token={token}",
              peer_address="127.0.0.1", expected_token=token) is True
    assert ok(method="GET", path="/api/auto", query=f"token={token}",
              peer_address="::1", expected_token=token) is True
    # Wrong verb.
    assert ok(method="POST", path="/api/chat", query=f"token={token}",
              peer_address="127.0.0.1", expected_token=token) is False
    # Any other path, including other GET APIs.
    assert ok(method="GET", path="/api/mcp", query=f"token={token}",
              peer_address="127.0.0.1", expected_token=token) is False
    # Non-loopback peer.
    assert ok(method="GET", path="/api/chat", query=f"token={token}",
              peer_address="10.0.0.5", expected_token=token) is False
    # Bad or missing token.
    assert ok(method="GET", path="/api/chat", query="token=nope",
              peer_address="127.0.0.1", expected_token=token) is False
    assert ok(method="GET", path="/api/chat", query="",
              peer_address="127.0.0.1", expected_token=token) is False
    # Token passed under a different name must not be honoured.
    assert ok(method="GET", path="/api/chat", query=f"access_token={token}",
              peer_address="127.0.0.1", expected_token=token) is False


# --- request smuggling ------------------------------------------------------

def _raw(port, payload: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(payload)
        try:
            s.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        chunks = []
        while True:
            try:
                b = s.recv(4096)
            except OSError:
                break
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks)


def test_duplicate_content_length_is_refused_as_smuggling():
    httpd, port, srv = _server()
    try:
        payload = (
            "POST /api/mcp/remove HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"X-Harness-Token: {srv._TOKEN}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 16\r\n"
            "Content-Length: 16\r\n"
            "\r\n"
            '{"name":"nope"}\n'
        ).encode()
        raw = _raw(port, payload)
        assert raw.startswith(b"HTTP/1."), raw[:80]
        assert b" 400 " in raw.split(b"\r\n", 1)[0] + b" "
    finally:
        httpd.shutdown()


def test_conflicting_content_length_and_transfer_encoding_is_refused():
    httpd, port, srv = _server()
    try:
        payload = (
            "POST /api/mcp/remove HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"X-Harness-Token: {srv._TOKEN}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 16\r\n"
            "Transfer-Encoding: chunked\r\n"
            "\r\n"
            "10\r\n" + '{"name":"nope"}\n' + "\r\n0\r\n\r\n"
        ).encode()
        raw = _raw(port, payload)
        assert raw.startswith(b"HTTP/1."), raw[:80]
        assert b" 400 " in raw.split(b"\r\n", 1)[0] + b" "
    finally:
        httpd.shutdown()


# --- token file permissions -------------------------------------------------

def test_restrict_token_file_verifies_the_observed_mode(tmp_path):
    import harness.server as srv
    path = tmp_path / "token"
    path.write_text("tok", encoding="utf-8")
    os.chmod(path, 0o644)
    assert srv._restrict_token_file(str(path)) is True
    if os.name != "posix":
        return
    assert (os.stat(path).st_mode & 0o777) & 0o077 == 0


def test_restrict_token_file_rejects_a_chmod_that_did_not_take(tmp_path, monkeypatch):
    """A no-op restrict_to_owner must NOT be reported as secure."""
    import harness.server as srv
    path = tmp_path / "token"
    path.write_text("tok", encoding="utf-8")
    os.chmod(path, 0o644)
    monkeypatch.setattr(srv, "restrict_to_owner", lambda _p: True)
    if os.name != "posix":
        pytest.skip("mode verification is POSIX-only")
    assert srv._restrict_token_file(str(path)) is False


# --- Electron main-process invariants ---------------------------------------

def _main_cjs() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "webapp" / "electron" / "main.cjs").read_text(encoding="utf-8")


def test_privileged_ipc_handlers_validate_their_sender():
    """Every privileged IPC channel must gate on the sender.

    browser:openExternal (hands a URL to the OS) and browser:popout (creates a
    persistent BrowserWindow) were the two that did not, while their neighbours
    did -- the inconsistency was the defect.
    """
    src = _main_cjs()
    channels = [
        "browser:openExternal",
        "browser:popout",
        "browser:setContext",
        "computer:setSession",
        "computer:releaseSession",
        "computer:revoke",
    ]
    for channel in channels:
        m = re.search(
            r'ipcMain\.handle\(\s*"' + re.escape(channel) + r'"\s*,\s*(?:async\s+)?'
            r'(?:\(([^)]*)\)|(\w+))\s*=>\s*\{(.{0,400})',
            src,
            re.S,
        )
        assert m, f"handler for {channel} not found in main.cjs"
        params = m.group(1) or m.group(2)
        body = m.group(3)
        assert not params.strip().startswith("_"), (
            f"{channel} ignores its event argument, so it cannot check the sender"
        )
        assert "isAllowedSender(" in body or "event.sender !== win.webContents" in body, (
            f"{channel} does not validate its sender"
        )


def test_token_injection_filter_covers_loopback_only():
    """Coarse check that the filter exists; tests/test_security.py asserts the
    per-pattern semantics against the same source."""
    src = _main_cjs()
    m = re.search(
        r"onBeforeSendHeaders\(\s*\{\s*urls:\s*\[(.*?)\]\s*\},(.*?)harnessToken",
        src,
        re.S,
    )
    assert m, "token-injection interceptor not found in main.cjs"
    patterns = re.findall(r'"([^"]+)"', m.group(1))
    assert patterns
    for pattern in patterns:
        assert "*/*" in pattern
