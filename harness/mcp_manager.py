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
}


def _expand(server: dict) -> dict:
    out = dict(server)
    args = out.get("args") or []
    out["args"] = [os.path.expanduser(a) if isinstance(a, str) else a for a in args]
    return out


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
            with os.fdopen(tmp_fd, 'w') as f:
                json.dump(data, f, indent=2)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
            os.chmod(path, 0o600)
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
        with self._lock:
            if name in self._clients and self._clients[name].alive:
                return [t for t in self._tools.values() if t.server == name]
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
                self._errors[name] = str(e)
                try:
                    client.stop()
                except Exception:
                    pass
                raise
            self._clients[name] = client
            self._errors.pop(name, None)
            for t in tools:
                self._tools[t.qualified] = t
            return tools

    def stop_server(self, name: str) -> None:
        c = self._clients.pop(name, None)
        if c:
            c.stop()
        for q in [q for q, t in self._tools.items() if t.server == name]:
            del self._tools[q]

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

    def stop_all(self) -> None:
        for name in list(self._clients):
            self.stop_server(name)

    # ---- tools --------------------------------------------------------------
    def tools(self) -> List[McpTool]:
        return list(self._tools.values())

    def status(self) -> List[dict]:
        cfg = self.load_config()
        out = []
        for name, server in cfg.items():
            alive = name in self._clients and self._clients[name].alive
            ntools = sum(1 for t in self._tools.values() if t.server == name)
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
