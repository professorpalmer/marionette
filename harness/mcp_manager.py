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
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .mcp_client import StdioMcpClient, McpTool, McpError
from .mcp_http_client import HttpMcpClient
from .secure_files import restrict_to_owner
from .diag import note as _diag

CONFIG_DIR = Path(os.path.expanduser("~/.pmharness"))
CONFIG_PATH = CONFIG_DIR / "mcp.json"

# Last tool-call receipts (not lifecycle health) live outside the repo under
# harness state. Never store arguments/results/secrets — only tool, ok, error, at.
_DEFAULT_INVOCATIONS_PATH = CONFIG_DIR / "state" / "mcp_invocations.json"
_MAX_INVOCATION_ERROR_CHARS = 200


def _invocations_path() -> Path:
    state_dir = (os.environ.get("HARNESS_STATE_DIR") or "").strip()
    if state_dir:
        return Path(state_dir) / "mcp_invocations.json"
    return _DEFAULT_INVOCATIONS_PATH


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sanitize_lifecycle_error(error: str) -> str:
    """Secret-redact lifecycle health errors before ``_errors`` / status / manage."""
    from .api.redaction import redact_secret_text

    return redact_secret_text((error or "").strip().replace("\n", " "))


def _sanitize_invocation_error(error: str) -> str:
    """Bound + secret-redact receipt errors before memory/disk/API/UI.

    Uses the same token/key conventions as ``harness.api.redaction`` so
    Bearer/basic auth, sk-/ghp_/github_pat shapes, and generic key/token
    assignments never persist in invocation receipts.
    """
    text = _sanitize_lifecycle_error(error)
    if len(text) > _MAX_INVOCATION_ERROR_CHARS:
        return text[: _MAX_INVOCATION_ERROR_CHARS - 3].rstrip() + "..."
    return text

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


def _allowed_tool_names(server_cfg: dict) -> Optional[set]:
    """Return the per-server tool allowlist, or None when unrestricted.

    ``allowed_tools`` in mcp.json is an optional list of bare tool names
    (e.g. ``["search", "read_file"]``). Absent / null → all tools allowed.
    A non-list value fails closed (empty set).
    """
    if not isinstance(server_cfg, dict) or "allowed_tools" not in server_cfg:
        return None
    raw = server_cfg.get("allowed_tools")
    if raw is None:
        return None
    if not isinstance(raw, list):
        return set()
    return {str(x).strip() for x in raw if str(x).strip()}


def _filter_tools_by_allowlist(tools: List[McpTool], allowed: Optional[set]) -> List[McpTool]:
    if allowed is None:
        return list(tools)
    return [t for t in tools if t.name in allowed]


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
        # Names the operator explicitly stopped (lazy-boot idle until cleared).
        self._operator_stopped = set()
        # Generation token per server: stop/refresh bumps it so an in-flight
        # refresh that finishes after a concurrent stop does not resurrect
        # a client the operator just halted.
        self._lifecycle_gen: Dict[str, int] = {}
        # Per-server last actual tool invocation (separate from lifecycle health).
        self._last_invocations: Dict[str, dict] = {}
        self._load_invocations()

    # ---- config -------------------------------------------------------------
    def load_config(self) -> Dict[str, dict]:
        """Native ``mcp.json`` servers only (never includes portable plugins)."""
        if not self.config_path.exists():
            return {}
        try:
            data = json.loads(self.config_path.read_text())
        except Exception:
            return {}
        return data.get("mcpServers", {}) or {}

    def _portable_plugin_servers(self) -> Dict[str, dict]:
        """Enabled Agent Plugins v1 stdio servers (best-effort; never raises)."""
        try:
            from .plugin_registry import list_enabled_mcp_servers

            return dict(list_enabled_mcp_servers() or {})
        except Exception as exc:
            _diag("mcp.portable_plugins", exc)
            return {}

    def effective_config(self) -> Dict[str, dict]:
        """Native mcp.json plus enabled portable plugin servers.

        Portable ids that collide with a native server name are skipped so
        user-authored ``mcp.json`` always wins.
        """
        native = self.load_config()
        merged: Dict[str, dict] = dict(native)
        for name, server in self._portable_plugin_servers().items():
            if name in merged:
                continue
            merged[name] = server
        return merged

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
        with self._lock:
            if name in self._last_invocations:
                del self._last_invocations[name]
                self._persist_invocations_unlocked()

    # ---- invocation receipts ------------------------------------------------
    def _load_invocations(self) -> None:
        path = _invocations_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        servers = data.get("servers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            return
        loaded: Dict[str, dict] = {}
        for name, row in servers.items():
            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(row, dict):
                continue
            tool = row.get("tool")
            if not isinstance(tool, str) or not tool.strip():
                continue
            ok = bool(row.get("ok"))
            error = _sanitize_invocation_error(str(row.get("error") or ""))
            at = row.get("at")
            if not isinstance(at, str) or not at.strip():
                continue
            # Ignore any accidental secret/payload keys; only keep the receipt.
            loaded[name.strip()] = {
                "tool": tool.strip(),
                "ok": ok,
                "error": error if not ok else "",
                "at": at.strip(),
            }
        with self._lock:
            self._last_invocations = loaded

    def _persist_invocations_unlocked(self) -> None:
        """Best-effort write; never raise into the call path."""
        path = _invocations_path()
        payload = {"servers": dict(self._last_invocations)}
        try:
            os.makedirs(str(path.parent), exist_ok=True)
            import tempfile
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), prefix="mcp_inv_",
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="\n") as f:
                    json.dump(payload, f, indent=2)
                os.replace(tmp_path, str(path))
                if not restrict_to_owner(str(path)):
                    _diag("secure_files.restrict_failed", msg=str(path))
            except Exception:
                if os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
        except Exception as e:
            _diag("mcp.invocations_persist", e)

    def _record_invocation(self, server: str, tool: str, *, ok: bool, error: str = "") -> None:
        server = (server or "").strip()
        tool = (tool or "").strip()
        if not server or not tool:
            return
        row = {
            "tool": tool,
            "ok": bool(ok),
            "error": "" if ok else _sanitize_invocation_error(error),
            "at": _utc_now_iso(),
        }
        with self._lock:
            self._last_invocations[server] = row
            self._persist_invocations_unlocked()

    # ---- lifecycle ----------------------------------------------------------
    def start_server(
        self,
        name: str,
        server: Optional[dict] = None,
        *,
        expect_gen: Optional[int] = None,
    ) -> List[McpTool]:
        """Start one MCP server.

        The manager lock only covers map mutations (clients/tools/errors).
        ``client.start()`` / ``list_tools()`` run outside the lock so stop /
        call / status are not head-of-line blocked on a slow handshake.

        ``expect_gen`` (used by ``refresh_server``) rejects the install when a
        concurrent ``stop_server`` bumped the lifecycle generation mid-handshake.
        """
        with self._lock:
            gen_now = self._lifecycle_gen.get(name, 0)
            if expect_gen is not None and gen_now != expect_gen:
                raise McpError(f"MCP server '{name}' start superseded by stop/refresh")
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
            cfg = _expand(server or self.effective_config().get(name, {}))
            gen_at_start = self._lifecycle_gen.get(name, 0)

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
            tools = _filter_tools_by_allowlist(
                client.list_tools(), _allowed_tool_names(cfg)
            )
        except McpError as e:
            with self._lock:
                # Redact before store so status()/GET /api/mcp never echo secrets.
                self._errors[name] = _sanitize_lifecycle_error(str(e))
            try:
                client.stop()
            except Exception:
                pass
            raise

        with self._lock:
            required = expect_gen if expect_gen is not None else gen_at_start
            # Concurrent stop/refresh bumped the generation — do not resurrect.
            if self._lifecycle_gen.get(name, 0) != required:
                try:
                    client.stop()
                except Exception:
                    pass
                raise McpError(f"MCP server '{name}' start superseded by stop/refresh")
            self._clients[name] = client
            self._errors.pop(name, None)
            self._operator_stopped.discard(name)
            for t in tools:
                self._tools[t.qualified] = t
            return list(tools)

    def stop_server(self, name: str) -> None:
        with self._lock:
            self._lifecycle_gen[name] = self._lifecycle_gen.get(name, 0) + 1
            self._operator_stopped.add(name)
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

        Bumps a per-server generation under the lock so a concurrent
        ``stop_server`` cannot leave a late ``start_server`` resurrecting the
        client after the operator halted it.
        """
        name = (name or "").strip()
        if not name:
            raise McpError("refresh requires a server name")
        if name not in self.effective_config():
            raise McpError(f"unknown MCP server '{name}'")
        with self._lock:
            self._lifecycle_gen[name] = self._lifecycle_gen.get(name, 0) + 1
            refresh_gen = self._lifecycle_gen[name]
            c = self._clients.pop(name, None)
            for q in [q for q, t in self._tools.items() if t.server == name]:
                del self._tools[q]
            self._errors.pop(name, None)
        if c:
            try:
                c.stop()
            except Exception:
                pass
        return self.start_server(name, expect_gen=refresh_gen)

    def start_all(self) -> Dict[str, object]:
        """Start every configured server; return {name: tool_count | error_str}.

        Explicit operator path (manage_mcp / Settings). Boot uses lazy connect
        instead — see ``ensure_server`` and ``boot_mcp_servers``.
        """
        report: Dict[str, object] = {}
        for name in self.effective_config():
            try:
                tools = self.start_server(name)
                report[name] = len(tools)
            except McpError as e:
                report[name] = f"error: {_sanitize_lifecycle_error(str(e))}"
        return report

    def ensure_server(self, name: str) -> List[McpTool]:
        """Start ``name`` only if it is not already alive; return its tools."""
        name = (name or "").strip()
        if not name:
            raise McpError("ensure_server requires a server name")
        with self._lock:
            existing = self._clients.get(name)
            if existing is not None and existing.alive:
                return [t for t in self._tools.values() if t.server == name]
        return self.start_server(name)

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
                    "error": _sanitize_lifecycle_error(str(e)),
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
                return {
                    "ok": False,
                    "name": name,
                    "error": _sanitize_lifecycle_error(str(e)),
                }
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
                return {
                    "ok": False,
                    "name": name,
                    "error": _sanitize_lifecycle_error(str(e)),
                }
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
        cfg = self.effective_config()
        with self._lock:
            clients = dict(self._clients)
            tools = list(self._tools.values())
            errors = dict(self._errors)
            operator_stopped = set(self._operator_stopped)
            invocations = {k: dict(v) for k, v in self._last_invocations.items()}
        out = []
        for name, server in cfg.items():
            client = clients.get(name)
            alive = client is not None and client.alive
            err = errors.get(name, "")
            if alive:
                lifecycle = "running"
            elif err:
                lifecycle = "error"
            elif name in operator_stopped:
                lifecycle = "stopped"
            else:
                lifecycle = "idle"
            ntools = (
                sum(1 for t in tools if t.server == name)
                if alive
                else 0
            )
            allowed = _allowed_tool_names(server)
            row = {
                "name": name, "command": server.get("command", "") or server.get("url", ""),
                "transport": "http" if server.get("url") else "stdio",
                "running": alive, "lifecycle": lifecycle, "tools": ntools,
                "error": err,
            }
            last = invocations.get(name)
            if last:
                # Lifecycle health (running/error/tools) stays separate from the
                # last actual tool call receipt.
                row["last_invocation"] = last
            if allowed is not None:
                row["allowed_tools"] = sorted(allowed)
            out.append(row)
        return out

    def _reject_if_disallowed(self, server: str, tool_name: str) -> None:
        cfg = self.load_config().get(server) or {}
        allowed = _allowed_tool_names(cfg)
        if allowed is None:
            return
        if tool_name not in allowed:
            raise McpError(
                f"MCP tool '{server}.{tool_name}' is not on the server allowlist"
            )

    def call(self, qualified: str, arguments: dict) -> dict:
        """Invoke a qualified MCP tool and record a per-server last_invocation.

        Receipts capture only ``{tool, ok, error, at}`` — never arguments or
        results. Lifecycle health (running/error/tool count) is unchanged.
        """
        server = ""
        tool_name = ""
        cached = self._tools.get(qualified)
        if cached:
            server, tool_name = cached.server, cached.name
        elif isinstance(qualified, str) and "." in qualified:
            server, tool_name = qualified.split(".", 1)

        try:
            if cached is not None:
                self._reject_if_disallowed(cached.server, cached.name)
                client = self._clients.get(cached.server)
                if not client or not client.alive:
                    # Dead/missing client: reconnect on demand (unchanged).
                    self.ensure_server(cached.server)
                    client = self._clients.get(cached.server)
                out = client.call_tool(cached.name, arguments)
                self._record_invocation(cached.server, cached.name, ok=True)
                return out

            # Allow "server.tool" for configured servers that are idle or whose
            # tools are not yet in the local cache (lazy boot / fresh ads).
            if server and tool_name:
                self._reject_if_disallowed(server, tool_name)
                client = self._clients.get(server)
                if (not client or not client.alive) and server in self.effective_config():
                    self.ensure_server(server)
                    client = self._clients.get(server)
                if client and client.alive:
                    out = client.call_tool(tool_name, arguments)
                    self._record_invocation(server, tool_name, ok=True)
                    return out
            raise McpError(f"unknown MCP tool '{qualified}'")
        except Exception as e:
            if server and tool_name:
                self._record_invocation(server, tool_name, ok=False, error=str(e))
            raise

    def discovered_tools(self) -> List[McpTool]:
        """Return tools for currently connected (alive) servers."""
        with self._lock:
            alive_servers = {name for name, client in self._clients.items() if client.alive}
            return [t for t in self._tools.values() if t.server in alive_servers]

