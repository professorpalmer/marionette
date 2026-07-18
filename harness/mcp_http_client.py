from __future__ import annotations

"""HTTP MCP client -- streamable-http JSON-RPC transport, stdlib only (urllib).

The official mcp SDK needs py3.10+; we already speak JSON-RPC for stdio, so HTTP
is the same protocol over POST instead of a pipe. This covers HOSTED MCP servers
(a URL endpoint) -- github-hosted, vercel, internal company MCP gateways, etc. --
which the stdio client cannot reach.

Config shape (standard, alongside stdio's command/args):
    {"mcpServers": {"acme": {"url": "https://mcp.acme.com/rpc",
                             "headers": {"Authorization": "Bearer ..."}}}}
"""

import http.client
import json
import os
import socket
import urllib.request
import urllib.error
from typing import Dict, List, Optional

from .mcp_client import (
    McpTool,
    McpError,
    PROTOCOL_VERSION,
    CLIENT_INFO,
    MCP_MAX_RESPONSE_BYTES,
)


def mcp_allow_private_urls() -> bool:
    """Whether HTTP MCP may target loopback/LAN (Docker, local gateways).

    MCP URLs are user-configured (State → MCP / mcp.json), so loopback and
    private ranges are allowed by default. Cloud metadata stays blocked in
    url_safety regardless. Opt out with ``PMHARNESS_MCP_ALLOW_PRIVATE=0``.
    The rig-wide ``HARNESS_ALLOW_PRIVATE_URLS`` hatch still opens this path
    when set.
    """
    from .url_safety import _is_truthy_value, allow_private_urls

    if allow_private_urls():
        return True
    raw = os.environ.get("PMHARNESS_MCP_ALLOW_PRIVATE")
    if raw is None or str(raw).strip() == "":
        return True
    return _is_truthy_value(raw)


class _PinnedIPHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection pinned to a specific IP (TOCTOU DNS-rebinding fix)."""

    def __init__(self, *args, pinned_ip=None, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def connect(self):
        if self._pinned_ip:
            self.sock = socket.create_connection(
                (self._pinned_ip, self.port), self.timeout, self.source_address,
            )
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        else:
            super().connect()


class _PinnedIPHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection pinned to a specific IP (TOCTOU DNS-rebinding fix)."""

    def __init__(self, *args, pinned_ip=None, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def connect(self):
        if self._pinned_ip:
            self.sock = socket.create_connection(
                (self._pinned_ip, self.port), self.timeout, self.source_address,
            )
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self._tunnel_host:
                self._tunnel()
            # Use the *original* hostname for TLS SNI / cert verification
            self.sock = self._context.wrap_socket(
                self.sock, server_hostname=self.host,
            )
        else:
            super().connect()


class _PinnedIPHTTPHandler(urllib.request.HTTPHandler):
    """HTTPHandler that injects a pinned-IP transport."""

    def __init__(self, pinned_ip=None, *args, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def http_open(self, req):
        return self.do_open(
            lambda *a, **kw: _PinnedIPHTTPConnection(
                *a, pinned_ip=self._pinned_ip, **kw
            ),
            req,
        )


class _PinnedIPHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPSHandler that injects a pinned-IP transport."""

    def __init__(self, pinned_ip=None, *args, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def https_open(self, req):
        return self.do_open(
            lambda *a, **kw: _PinnedIPHTTPSConnection(
                *a, pinned_ip=self._pinned_ip, **kw
            ),
            req,
        )


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every 3xx Location through the SSRF gate before following.

    urllib follows redirects by default without re-checking the target, so a
    malicious MCP server can 302 to metadata or an internal host after the
    initial URL passed validation. Cap at urllib's default max_redirections.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from .url_safety import is_safe_url_pinned

        ok, reason, _ = is_safe_url_pinned(
            newurl, allow_private=mcp_allow_private_urls(),
        )
        if not ok:
            raise urllib.error.HTTPError(
                newurl, code, f"redirect blocked: {reason}", headers, fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpMcpClient:
    """One hosted MCP server, spoken to over HTTP JSON-RPC POST."""

    def __init__(self, name: str, url: str, headers: Optional[Dict[str, str]] = None,
                 timeout: float = 30.0):
        self.name = name
        self.url = url
        self._pinned_ip = self.validate_url(url)
        self.headers = dict(headers or {})
        self.timeout = timeout
        self._id = 0
        self._session_id: Optional[str] = None
        self._initialized = False
        self._server_info: dict = {}
        # Always install SafeRedirectHandler so 3xx targets are re-validated,
        # including the unresolvable-hostname path that has no pinned IP.
        self._opener = (
            self._make_pinned_opener(self._pinned_ip)
            if self._pinned_ip
            else urllib.request.build_opener(SafeRedirectHandler)
        )

    def validate_url(self, url: str) -> Optional[str]:
        """Validate the URL for SSRF safety and return a pinned IP address.

        Delegates to harness.url_safety so this client and the web tools share
        ONE blocklist -- a bespoke copy here had drifted (it missed the
        metadata hostnames and the AWS IPv6 metadata address, and read a
        different escape-hatch env var). Metadata endpoints stay blocked even
        with a hatch set. Returns the first validated resolved IP. Unresolvable
        hostnames fail closed unless private/loopback MCP URLs are allowed
        (see mcp_allow_private_urls()), in which case None is returned and the
        request proceeds without a pinned IP. Raises McpError if the URL is
        unsafe.
        """
        from .url_safety import is_safe_url_pinned

        # User-configured MCP endpoints (Docker localhost, LAN) are allowed by
        # default; metadata stays blocked. See mcp_allow_private_urls().
        allow_private = mcp_allow_private_urls()
        ok, reason, pinned_ip = is_safe_url_pinned(
            url, allow_private=allow_private,
        )
        if ok:
            ip = pinned_ip or ""
            return ip.split("%")[0] if "%" in ip else (pinned_ip or None)
        # Fail closed on DNS failure unless private URLs are allowed (local /
        # Docker MCP hosts may not resolve in the parent namespace).
        if "could not be resolved" in reason:
            if allow_private:
                return None
            raise McpError(
                f"Unsafe MCP URL: hostname could not be resolved: {reason}"
            )
        raise McpError(f"Unsafe MCP URL: {reason}")

    @staticmethod
    def _make_pinned_opener(pinned_ip: str):
        """Build an opener that connects to *pinned_ip* while preserving the
        original hostname in HTTP Host headers and HTTPS SNI / certificate
        verification. Includes SafeRedirectHandler so redirect targets are
        re-validated through the SSRF gate."""
        return urllib.request.build_opener(
            SafeRedirectHandler,
            _PinnedIPHTTPHandler(pinned_ip=pinned_ip),
            _PinnedIPHTTPSHandler(pinned_ip=pinned_ip),
        )

    # ---- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        resp = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "clientInfo": CLIENT_INFO,
        }, timeout=self.timeout)
        self._server_info = resp.get("serverInfo", {})
        self._initialized = True
        # best-effort initialized notification
        try:
            self._notify("notifications/initialized", {})
        except McpError:
            pass

    def stop(self) -> None:
        # HTTP is stateless from our side; nothing to tear down.
        self._initialized = False

    @property
    def alive(self) -> bool:
        return self._initialized

    # ---- JSON-RPC over HTTP -------------------------------------------------
    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _post(self, payload: dict, timeout: float) -> Optional[dict]:
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            # streamable-http servers may reply as JSON or an SSE stream
            "Accept": "application/json, text/event-stream",
        }
        headers.update(self.headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            r = self._opener.open(req, timeout=timeout)
            with r:
                # capture a session id if the server issued one
                sid = r.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                # Cap body size so a malicious MCP server cannot OOM the harness.
                raw_bytes = r.read(MCP_MAX_RESPONSE_BYTES + 1)
                if len(raw_bytes) > MCP_MAX_RESPONSE_BYTES:
                    raise McpError(
                        f"MCP server '{self.name}': response exceeded "
                        f"{MCP_MAX_RESPONSE_BYTES} bytes"
                    )
                raw = raw_bytes.decode()
                ctype = r.headers.get("Content-Type", "")
        except McpError:
            raise
        except urllib.error.HTTPError as e:
            raise McpError(f"MCP server '{self.name}' HTTP {e.code}: {e.read()[:200].decode(errors='replace')}")
        except urllib.error.URLError as e:
            raise McpError(f"MCP server '{self.name}' unreachable: {e}")
        if not raw.strip():
            return None  # notification -> empty 202
        # SSE-framed response: extract the JSON from the last data: line
        if "text/event-stream" in ctype:
            obj = None
            for line in raw.splitlines():
                if line.startswith("data:"):
                    try:
                        obj = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
            return obj
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise McpError(f"MCP server '{self.name}': non-JSON response: {raw[:200]}")

    def _notify(self, method: str, params: dict) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": params}, self.timeout)

    def _request(self, method: str, params: dict, timeout: float = 60.0) -> dict:
        rid = self._next_id()
        msg = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, timeout)
        if msg is None:
            raise McpError(f"MCP server '{self.name}': empty response to {method}")
        if "error" in msg:
            raise McpError(f"{method} -> {msg['error']}")
        return msg.get("result", {})

    # ---- MCP methods --------------------------------------------------------
    def list_tools(self) -> List[McpTool]:
        result = self._request("tools/list", {})
        return [McpTool(server=self.name, name=t.get("name", ""),
                        description=t.get("description", ""),
                        input_schema=t.get("inputSchema", {}) or {})
                for t in result.get("tools", [])]

    def call_tool(self, tool_name: str, arguments: dict, timeout: float = 120.0) -> dict:
        return self._request("tools/call", {"name": tool_name, "arguments": arguments or {}},
                             timeout=timeout)
