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
import ipaddress
from urllib.parse import urlparse
from typing import Dict, List, Optional

from .mcp_client import McpTool, McpError, PROTOCOL_VERSION, CLIENT_INFO


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
        self._opener = self._make_pinned_opener(self._pinned_ip) if self._pinned_ip else None

    def validate_url(self, url: str) -> Optional[str]:
        """Validate the URL for SSRF safety and return a pinned IP address.

        Returns the first validated resolved IP (or the literal IP if the
        hostname is already an IP literal), or None if the hostname could not
        be resolved. Raises McpError if the URL is unsafe.
        """
        try:
            u = urlparse(url)
        except Exception as e:
            raise McpError(f"Invalid URL: {e}")
        
        if u.scheme not in ("http", "https"):
            raise McpError(f"Invalid URL scheme '{u.scheme}'. Only http and https are allowed.")
        
        hostname = u.hostname
        if not hostname:
            raise McpError("Invalid URL: missing hostname.")
        
        # Block the cloud metadata IP 169.254.169.254 explicitly
        if hostname == "169.254.169.254":
            raise McpError("Access to cloud metadata IP is blocked.")
        
        allow_private = os.environ.get("PMHARNESS_MCP_ALLOW_PRIVATE", "").strip() in ("1", "true", "yes", "on")
        if allow_private:
            return hostname if self._is_ip_literal(hostname) else None
            
        # If it's an IP literal, check and pin it directly
        if self._is_ip_literal(hostname):
            try:
                ip = ipaddress.ip_address(hostname)
            except ValueError:
                raise McpError(f"Invalid IP address: {hostname}")
            if str(ip) == "169.254.169.254":
                raise McpError("Access to cloud metadata IP is blocked.")
            if (ip.is_private or ip.is_loopback or ip.is_link_local or 
                ip.is_reserved or ip.is_multicast):
                raise McpError(f"Access to private/local IP {ip} is blocked for security reasons.")
            return hostname  # pin the literal IP

        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            return None  # can't resolve, will fail on connect
            
        for family, socktype, proto, canonname, sockaddr in infos:
            ip_str = str(sockaddr[0])
            if "%" in ip_str:
                ip_str = ip_str.split("%")[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
                
            if str(ip) == "169.254.169.254":
                raise McpError("Access to cloud metadata IP is blocked.")
                
            if (ip.is_private or ip.is_loopback or ip.is_link_local or 
                ip.is_reserved or ip.is_multicast):
                raise McpError(f"Access to private/local IP {ip} is blocked for security reasons.")

        # First resolved address is the pin target
        first_ip = str(infos[0][4][0])
        if "%" in first_ip:
            first_ip = first_ip.split("%")[0]
        return first_ip

    @staticmethod
    def _is_ip_literal(hostname: str) -> bool:
        try:
            ipaddress.ip_address(hostname)
            return True
        except ValueError:
            return False

    @staticmethod
    def _make_pinned_opener(pinned_ip: str):
        """Build an opener that connects to *pinned_ip* while preserving the
        original hostname in HTTP Host headers and HTTPS SNI / certificate
        verification."""
        return urllib.request.build_opener(
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
            if self._opener:
                r = self._opener.open(req, timeout=timeout)
            else:
                r = urllib.request.urlopen(req, timeout=timeout)
            with r:
                # capture a session id if the server issued one
                sid = r.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                raw = r.read().decode()
                ctype = r.headers.get("Content-Type", "")
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
