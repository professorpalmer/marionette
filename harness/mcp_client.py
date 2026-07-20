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
they already have. Optional per-server ``allowed_tools`` (bare names) is enforced
by ``McpManager``:

    {"mcpServers": {
        "github":  {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                    "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "..."},
                    "allowed_tools": ["search_repositories", "list_issues"]},
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"]}
    }}
"""

import json
import os
import queue
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "pm-harness", "version": "0.1"}


def _is_windows() -> bool:
    """Platform check (testable; do not monkeypatch ``os.name`` — breaks pathlib)."""
    return os.name == "nt"

# Cap MCP tool / JSON-RPC response bodies so a malicious or buggy server cannot
# exhaust harness memory. Shared by stdio and HTTP transports.
MCP_MAX_RESPONSE_BYTES = 16 * 1024 * 1024

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
        # Serializes request writes + pending-map bookkeeping only. Blocking
        # waits for responses happen outside the lock (see _request / _read_loop).
        self._lock = threading.Lock()
        self._pending: Dict[int, "queue.Queue[Union[dict, BaseException]]"] = {}
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_error: Optional[BaseException] = None
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
        if _is_windows():
            # npx/uvx/node CLIs are .cmd/.exe shims on Windows; a bare name
            # fails Popen without shell=True. Resolve to the real path instead
            # of using a shell (avoids quoting bugs and CVE-2024-27980-style
            # .cmd injection concerns).
            import shutil as _shutil
            resolved = _shutil.which(command, path=full_env.get("PATH") or os.environ.get("PATH"))
            if resolved:
                command = resolved
        popen_kwargs: dict = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": full_env,
            "cwd": self.cwd,
            "text": True,
            "bufsize": 1,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if _is_windows():
            # Defense-in-depth alongside harness.win_console's process-wide
            # Popen patch. CREATE_NO_WINDOW does not reach Node→MCP grandchildren
            # spawned by Cursor Agent — that remains an upstream fix.
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            # Own process group so stop() can killpg the npx/uvx → node/python tree.
            popen_kwargs["start_new_session"] = True
        try:
            self._proc = subprocess.Popen([command, *self.args], **popen_kwargs)
        except FileNotFoundError as e:
            raise McpError(f"MCP server '{self.name}': command not found: {self.command} ({e})")
        # Drain stderr on a background thread so a chatty server cannot fill the OS
        # pipe buffer and deadlock the child (stdout is read on _read_loop).
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
        self._reader_error = None
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
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
            if _is_windows():
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
            else:
                # Kill the whole session started with start_new_session=True.
                try:
                    pgid = os.getpgid(self._proc.pid)
                    os.killpg(pgid, signal.SIGTERM)
                    try:
                        self._proc.wait(timeout=3)
                    except Exception:
                        os.killpg(pgid, signal.SIGKILL)
                        try:
                            self._proc.wait(timeout=2)
                        except Exception:
                            pass
                except Exception:
                    try:
                        self._proc.terminate()
                        self._proc.wait(timeout=3)
                    except Exception:
                        try:
                            self._proc.kill()
                        except Exception:
                            pass
        self._proc = None
        self._fail_pending(McpError(f"MCP server '{self.name}' stopped"))

    def cancel(self) -> int:
        """Unblock in-flight JSON-RPC waiters without killing the server.

        Unlike ``_fail_pending`` / ``stop``, this does not mark the reader dead,
        so later calls can reuse the same process. Returns how many waiters
        were cancelled.
        """
        exc = McpError(f"MCP server '{self.name}': request cancelled")
        with self._lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for q in waiters:
            try:
                q.put_nowait(exc)
            except queue.Full:
                pass
        return len(waiters)

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
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _readline_bounded(self) -> Optional[str]:
        """Read one stdout line, rejecting bodies larger than MCP_MAX_RESPONSE_BYTES."""
        assert self._proc is not None and self._proc.stdout is not None
        # TextIO.readline(size) returns at most `size` characters; a full line
        # longer than the cap arrives without a trailing newline.
        line = self._proc.stdout.readline(MCP_MAX_RESPONSE_BYTES + 1)
        if line == "":
            return None
        if len(line) > MCP_MAX_RESPONSE_BYTES:
            raise McpError(
                f"MCP server '{self.name}': response exceeded "
                f"{MCP_MAX_RESPONSE_BYTES} bytes"
            )
        return line

    def _fail_pending(self, exc: BaseException) -> None:
        self._reader_error = exc
        with self._lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for q in waiters:
            try:
                q.put_nowait(exc)
            except queue.Full:
                pass

    def _read_loop(self) -> None:
        """Demux stdout JSON-RPC responses to per-request queues (no lock held)."""
        try:
            while True:
                if self._proc is None:
                    break
                line = self._readline_bounded()
                if line is None:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # servers sometimes log to stdout; ignore non-JSON noise
                    continue
                mid = msg.get("id")
                if mid is None:
                    continue  # notification
                with self._lock:
                    q = self._pending.get(mid)
                if q is not None:
                    q.put(msg)
        except McpError as e:
            self._fail_pending(e)
            return
        except Exception as e:
            self._fail_pending(
                McpError(f"MCP server '{self.name}' read failed: {e}")
            )
            return
        err = "".join(getattr(self, "_stderr_tail", []))
        self._fail_pending(
            McpError(
                f"MCP server '{self.name}' closed the connection. {err[-400:]}"
            )
        )

    def _request(self, method: str, params: dict, timeout: float = 60.0) -> dict:
        if self._reader_error is not None:
            raise McpError(str(self._reader_error))
        waiter: "queue.Queue[Union[dict, BaseException]]" = queue.Queue(maxsize=1)
        with self._lock:
            rid = self._next_id()
            self._pending[rid] = waiter
            try:
                self._send(
                    {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
                )
            except Exception:
                self._pending.pop(rid, None)
                raise
        try:
            msg = waiter.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._pending.pop(rid, None)
            raise McpError(f"MCP server '{self.name}': timeout waiting for {method}")
        with self._lock:
            self._pending.pop(rid, None)
        if isinstance(msg, BaseException):
            raise msg if isinstance(msg, McpError) else McpError(str(msg))
        if "error" in msg:
            raise McpError(f"{method} -> {msg['error']}")
        return msg.get("result", {})

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
