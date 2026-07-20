from __future__ import annotations

"""MCP server manager: loads the user's mcp.json, starts servers lazily, and
aggregates their tools so the pilot can call any of them. Config lives at
~/.pmharness/mcp.json in the standard Claude/Cursor shape.

This is the "access other MCPs people wanna add" layer: github, aws, vercel,
browser-control (puppeteer), filesystem -- anything with an MCP server -- plus
arbitrary user-added entries.
"""

import json
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional

from .mcp_client import StdioMcpClient, McpTool, McpError
from .mcp_http_client import HttpMcpClient
from .secure_files import restrict_to_owner
from .diag import note as _diag

CONFIG_DIR = Path(os.path.expanduser("~/.pmharness"))
CONFIG_PATH = CONFIG_DIR / "mcp.json"

# A small seed catalog of common servers so the UI can offer one-click adds.
# command/args only; the user supplies env (tokens) when enabling.
CATALOG = {
    "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
               "env_hint": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
               "desc": "GitHub repos, issues, PRs, code search"},
    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "~"],
                   "env_hint": [], "desc": "Local filesystem read/write (scoped path)"},
    "puppeteer": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-puppeteer"],
                  "env_hint": [], "desc": "Browser control (navigate, click, screenshot)"},
    "aws": {"command": "uvx", "args": ["awslabs.core-mcp-server@latest"],
            "env_hint": ["AWS_PROFILE", "AWS_REGION"],
            "desc": "AWS (via awslabs MCP servers)"},
    "vercel": {"command": "npx", "args": ["-y", "@vercel/mcp-adapter"],
               "env_hint": ["VERCEL_TOKEN"], "desc": "Vercel deployments + projects"},
    "firecrawl": {"command": "npx", "args": ["-y", "firecrawl-mcp"],
                  "env_hint": ["FIRECRAWL_API_KEY"],
                  "desc": "Firecrawl web search/scrape (set FIRECRAWL_API_KEY)"},
}


def _expand(server: dict) -> dict:
    out = dict(server)
    args = out.get("args") or []
    out["args"] = [os.path.expanduser(a) if isinstance(a, str) else a for a in args]
    return out


_REDACTED = "REDACTED"


def redact_mcp_secrets(value):
    """Return a deep copy of *value* with env/headers secret values redacted.

    Used for manage_mcp transcripts and any config dump that must not echo
    tokens from mcp.json into the chat history.
    """
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in ("env", "headers") and isinstance(item, dict):
                out[key] = {k: _REDACTED for k in item}
            elif key in ("env", "headers") and item:
                out[key] = _REDACTED
            else:
                out[key] = redact_mcp_secrets(item)
        return out
    if isinstance(value, list):
        return [redact_mcp_secrets(item) for item in value]
    return value


class McpManager:
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = Path(config_path) if config_path else CONFIG_PATH
        self._clients: Dict[str, StdioMcpClient] = {}
        self._tools: Dict[str, McpTool] = {}   # qualified name -> tool
        self._lock = threading.Lock()
        self._errors: Dict[str, str] = {}

    # ---- config -------------------------------------------------------------
    def load_config(self) -> Dict[str, dict]:
        if not self.config_path.exists():
            return {}
        try:
            data = json.loads(self.config_path.read_text())
        except Exception:
            return {}
        return data.get("mcpServers", {}) or {}

    def _write_config(self, data: dict) -> None:
        path = str(self.config_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        import tempfile
        tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix="mcp_")
        try:
            with os.fdopen(tmp_fd, 'w', encoding='utf-8', newline='\n') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, path)
            if not restrict_to_owner(path):
                _diag("secure_files.restrict_failed", msg=path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

    def save_server(self, name: str, server: dict) -> None:
        data = {"mcpServers": self.load_config()}
        data["mcpServers"][name] = server
        self._write_config(data)

    def remove_server(self, name: str) -> None:
        data = {"mcpServers": self.load_config()}
        if name in data["mcpServers"]:
            del data["mcpServers"][name]
            self._write_config(data)
        self.stop_server(name)

    # ---- lifecycle ----------------------------------------------------------
    def start_server(self, name: str, server: Optional[dict] = None) -> List[McpTool]:
        """Start one MCP server.

        The manager lock only covers map mutations (clients/tools/errors).
        ``client.start()`` / ``list_tools()`` run outside the lock so stop /
        call / status are not head-of-line blocked on a slow handshake.
        """
        with self._lock:
            existing = self._clients.get(name)
            if existing is not None and existing.alive:
                return [t for t in self._tools.values() if t.server == name]
            # Drop a dead client so a later start (or Refresh) can reconnect
            # after Docker/HTTP came back online.
            if existing is not None:
                try:
                    existing.stop()
                except Exception:
                    pass
                self._clients.pop(name, None)
                for q in [q for q, t in self._tools.items() if t.server == name]:
                    del self._tools[q]
            cfg = _expand(server or self.load_config().get(name, {}))

        if cfg.get("url"):
            client = HttpMcpClient(name=name, url=cfg["url"], headers=cfg.get("headers"))
        elif cfg.get("command"):
            client = StdioMcpClient(
                name=name, command=cfg["command"], args=cfg.get("args"),
                env=cfg.get("env"), cwd=cfg.get("cwd"))
        else:
            raise McpError(f"MCP server '{name}' needs a 'command' (stdio) or 'url' (http)")
        try:
            client.start()
            tools = client.list_tools()
        except McpError as e:
            with self._lock:
                self._errors[name] = str(e)
            try:
                client.stop()
            except Exception:
                pass
            raise
        with self._lock:
            self._clients[name] = client
            self._errors.pop(name, None)
            for t in tools:
                self._tools[t.qualified] = t
            return list(tools)

    def stop_server(self, name: str) -> None:
        with self._lock:
            c = self._clients.pop(name, None)
            for q in [q for q, t in self._tools.items() if t.server == name]:
                del self._tools[q]
            self._errors.pop(name, None)
        if c:
            c.stop()

    def refresh_server(self, name: str) -> List[McpTool]:
        """Force reconnect: stop (clear client/tools/error) then start again.

        Used by the State MCP Refresh button so Docker/HTTP servers that were
        unreachable at first start can be re-probed without app restart.
        """
        name = (name or "").strip()
        if not name:
            raise McpError("refresh requires a server name")
        if name not in self.load_config():
            raise McpError(f"unknown MCP server '{name}'")
        self.stop_server(name)
        return self.start_server(name)

    def start_all(self) -> Dict[str, object]:
        """Start every configured server; return {name: tool_count | error_str}."""
        report: Dict[str, object] = {}
        for name in self.load_config():
            try:
                tools = self.start_server(name)
                report[name] = len(tools)
            except McpError as e:
                report[name] = f"error: {e}"
        return report

    def manage(
        self,
        action: str,
        *,
        name: str = "",
        url: str = "",
        command: str = "",
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> dict:
        """Pilot-facing add/start/stop/remove/list for MCP servers.

        For Docker / streamable-HTTP servers prefer ``url`` only (secrets stay
        in the container env, not mcp.json).
        """
        action = (action or "").strip().lower()
        name = (name or "").strip()
        if action == "list":
            return {"ok": True, "servers": self.status()}
        if action == "add":
            if not name:
                return {"ok": False, "error": "manage_mcp add requires name"}
            url = (url or "").strip()
            command = (command or "").strip()
            if not url and not command:
                return {
                    "ok": False,
                    "error": "manage_mcp add requires url (HTTP/Docker) or command (stdio)",
                }
            server: dict = {}
            if url:
                server["url"] = url
            else:
                server["command"] = command
                if args:
                    server["args"] = list(args)
                if env:
                    server["env"] = dict(env)
            self.save_server(name, server)
            try:
                tools = self.start_server(name)
                return {
                    "ok": True,
                    "name": name,
                    "transport": "http" if url else "stdio",
                    "tools": len(tools),
                    "hint": "Visible under State → MCP. Call tools via call_mcp.",
                }
            except Exception as e:
                return {
                    "ok": False,
                    "name": name,
                    "error": str(e),
                    "saved": True,
                    "hint": "Saved to mcp.json but start failed; fix the URL/command and manage_mcp start.",
                }
        if action == "start":
            if not name:
                return {"ok": False, "error": "manage_mcp start requires name"}
            try:
                tools = self.start_server(name)
                return {"ok": True, "name": name, "tools": len(tools)}
            except Exception as e:
                return {"ok": False, "name": name, "error": str(e)}
        if action == "stop":
            if not name:
                return {"ok": False, "error": "manage_mcp stop requires name"}
            self.stop_server(name)
            return {"ok": True, "name": name, "stopped": True}
        if action == "refresh":
            if not name:
                return {"ok": False, "error": "manage_mcp refresh requires name"}
            try:
                tools = self.refresh_server(name)
                return {"ok": True, "name": name, "tools": len(tools), "refreshed": True}
            except Exception as e:
                return {"ok": False, "name": name, "error": str(e)}
        if action == "remove":
            if not name:
                return {"ok": False, "error": "manage_mcp remove requires name"}
            self.remove_server(name)
            return {"ok": True, "name": name, "removed": True}
        return {
            "ok": False,
            "error": f"unknown manage_mcp action {action!r} (list|add|start|stop|refresh|remove)",
        }

    def stop_all(self) -> None:
        for name in list(self._clients):
            self.stop_server(name)

    # ---- tools --------------------------------------------------------------
    def tools(self) -> List[McpTool]:
        return list(self._tools.values())

    def status(self) -> List[dict]:
        """Per-server running flag + tool count for Settings / manage_mcp list.

        Tool count matches ``discovered_tools()``: only alive clients. A
        dead-but-not-stopped client can still hold cached tool rows in
        ``_tools``; reporting those as ``tools: N`` with ``running: false``
        mismatched the alive-only tools list on GET /api/mcp.
        """
        cfg = self.load_config()
        out = []
        for name, server in cfg.items():
            alive = name in self._clients and self._clients[name].alive
            ntools = (
                sum(1 for t in self._tools.values() if t.server == name)
                if alive
                else 0
            )
            out.append({
                "name": name, "command": server.get("command", "") or server.get("url", ""),
                "transport": "http" if server.get("url") else "stdio",
                "running": alive, "tools": ntools,
                "error": self._errors.get(name, ""),
            })
        return out

    def call(self, qualified: str, arguments: dict) -> dict:
        tool = self._tools.get(qualified)
        if not tool:
            # allow "server.tool" where server is running but tool not cached
            if "." in qualified:
                sv, tn = qualified.split(".", 1)
                client = self._clients.get(sv)
                if client and client.alive:
                    return client.call_tool(tn, arguments)
            raise McpError(f"unknown MCP tool '{qualified}'")
        client = self._clients.get(tool.server)
        if not client or not client.alive:
            self.start_server(tool.server)
            client = self._clients.get(tool.server)
        return client.call_tool(tool.name, arguments)

    def discovered_tools(self) -> List[McpTool]:
        """Return tools for currently connected (alive) servers."""
        with self._lock:
            alive_servers = {name for name, client in self._clients.items() if client.alive}
            return [t for t in self._tools.values() if t.server in alive_servers]

