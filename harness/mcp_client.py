from __future__ import annotations

"""Minimal MCP (Model Context Protocol) client -- stdlib only, Python 3.9+.

The official `mcp` SDK needs Python 3.10+, but MCP is just JSON-RPC 2.0 over a
transport. The harness rig is stdlib-only (see AGENTS.md), so we implement the
stdio transport directly: spawn the server process, speak newline-delimited
JSON-RPC over its stdin/stdout, do the initialize handshake, then tools/list and
tools/call. This covers the common npx/uvx-launched servers (github, filesystem,
aws, vercel, puppeteer/browser, etc.). HTTP/SSE transport is a documented
follow-up.

Config shape is the standard Claude/Cursor mcp.json form so users can paste what
they already have:

    {"mcpServers": {
        "github":  {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "..."}},
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"]}
    }}
"""

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "pm-harness", "version": "0.1"}

# Safe environment baseline to prevent leaking parent API keys/tokens to MCP subprocesses
_SAFE_ENV_KEYS = {
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TERM",
    "SHELL",
    "TMPDIR",
    # Windows equivalents: without USERPROFILE/APPDATA npm-based servers can't
    # find their caches, and without SystemRoot/COMSPEC many Win32 APIs and
    # cmd shims fail outright.
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "COMSPEC",
    "PATHEXT",
    "USERNAME",
    "OS",
    "NUMBER_OF_PROCESSORS",
}


@dataclass
class McpTool:
    server: str
    name: str
    description: str
    input_schema: dict = field(default_factory=dict)

    @property
    def qualified(self) -> str:
        # namespaced so two servers can expose same-named tools
        return f"{self.server}.{self.name}"


class McpError(RuntimeError):
    pass


class StdioMcpClient:
    """One spawned MCP server, spoken to over stdio JSON-RPC."""

    def __init__(self, name: str, command: str, args: Optional[List[str]] = None,
                 env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None,
                 startup_timeout: float = 30.0):
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = cwd
        self.startup_timeout = startup_timeout
        self._proc: Optional[subprocess.Popen] = None
        self._id = 0
        self._lock = threading.Lock()
        self._server_info: dict = {}
        self._capabilities: dict = {}

    # ---- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        # Filter parent environment to avoid leaking sensitive credentials/keys to child processes
        # Compare uppercased: Windows env keys keep their original casing
        # ("SystemRoot", "ComSpec") while the whitelist is uppercase.
        full_env = {
            k: v for k, v in os.environ.items()
            if k.upper() in _SAFE_ENV_KEYS or k.upper().startswith("XDG_")
        }
        full_env.update({k: str(v) for k, v in self.env.items()})
        command = self.command
        if os.name == "nt":
            # npx/uvx/node CLIs are .cmd/.exe shims on Windows; a bare name
            # fails Popen without shell=True. Resolve to the real path instead
            # of using a shell (avoids quoting bugs and CVE-2024-27980-style
            # .cmd injection concerns).
            import shutil as _shutil
            resolved = _shutil.which(command, path=full_env.get("PATH") or os.environ.get("PATH"))
            if resolved:
                command = resolved
        try:
            self._proc = subprocess.Popen(
                [command, *self.args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=full_env, cwd=self.cwd,
                text=True, bufsize=1,
             encoding="utf-8", errors="replace")
        except FileNotFoundError as e:
            raise McpError(f"MCP server '{self.name}': command not found: {self.command} ({e})")
        # Drain stderr on a background thread so a chatty server cannot fill the OS
        # pipe buffer and deadlock the child (we only read stdout in _request).
        self._stderr_tail: List[str] = []
        def _drain():
            try:
                for line in self._proc.stderr:
                    self._stderr_tail.append(line)
                    if len(self._stderr_tail) > 50:
                        self._stderr_tail.pop(0)
            except Exception:
                pass
        self._stderr_thread = threading.Thread(target=_drain, daemon=True)
        self._stderr_thread.start()
        # handshake
        resp = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "clientInfo": CLIENT_INFO,
        }, timeout=self.startup_timeout)
        self._server_info = resp.get("serverInfo", {})
        self._capabilities = resp.get("capabilities", {})
        self._notify("notifications/initialized", {})

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            if os.name == "nt":
                # terminate() only hits the top-level shim; npx-spawned node
                # children detach and linger. taskkill /T fells the whole tree.
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                        capture_output=True, timeout=10,
                    )
                except Exception:
                    pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None

    @property
    def alive(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    # ---- JSON-RPC -----------------------------------------------------------
    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _send(self, payload: dict) -> None:
        if not self._proc or self._proc.poll() is not None:
            raise McpError(f"MCP server '{self.name}' is not running")
        line = json.dumps(payload) + "\n"
        self._proc.stdin.write(line)
        self._proc.stdin.flush()

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict, timeout: float = 60.0) -> dict:
        with self._lock:
            rid = self._next_id()
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            deadline = time.time() + timeout
            # read newline-delimited json until we see our id (skip notifications)
            while time.time() < deadline:
                line = self._proc.stdout.readline()
                if line == "":
                    # process died -- surface captured stderr tail (drained on a thread)
                    err = "".join(getattr(self, "_stderr_tail", []))
                    raise McpError(f"MCP server '{self.name}' closed the connection. {err[-400:]}")
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # servers sometimes log to stdout; ignore non-JSON noise
                    continue
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise McpError(f"{method} -> {msg['error']}")
                    return msg.get("result", {})
                # else: a notification or another response -> keep reading
            raise McpError(f"MCP server '{self.name}': timeout waiting for {method}")

    # ---- MCP methods --------------------------------------------------------
    def list_tools(self) -> List[McpTool]:
        result = self._request("tools/list", {})
        out = []
        for t in result.get("tools", []):
            out.append(McpTool(
                server=self.name, name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}) or {},
            ))
        return out

    def call_tool(self, tool_name: str, arguments: dict, timeout: float = 120.0) -> dict:
        result = self._request("tools/call", {"name": tool_name, "arguments": arguments or {}},
                               timeout=timeout)
        # MCP returns {content: [{type, text|data}], isError?}
        return result
