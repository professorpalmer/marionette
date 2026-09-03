"""MCP HTTP route bodies (peeled from ``harness.server``).

List/add/remove/start/stop/call/catalog JSON handlers take a
:class:`McpServices` so this module never imports ``harness.server`` at top
level. ``server.Handler`` keeps auth/token gates and thin path delegates.
SSRF/allowlist checks stay inside ``HttpMcpClient`` / ``url_safety`` when a
URL server is started — unchanged by this peel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..mcp_manager import CATALOG
from .redaction import redact_secret_text


def _lifecycle_error(exc: BaseException) -> str:
    """Mask secrets in MCP start/add/refresh failure text before JSON leaves the API."""
    return redact_secret_text(str(exc))


def _fire_tools_changed(svc: "McpServices") -> None:
    """Hatch: drop the conversation tools[] snapshot after MCP lifecycle."""
    cb = getattr(svc, "on_tools_changed", None)
    if not callable(cb):
        return
    try:
        cb()
    except Exception:
        pass


@dataclass
class McpServices:
    """Explicit deps for MCP HTTP handlers (injected by ``server.py``)."""

    mcp: Any
    on_tools_changed: Optional[Any] = None


def get_mcp(svc: McpServices) -> tuple[int, dict]:
    """GET /api/mcp — configured servers + alive-only discovered tools.

    Uses ``discovered_tools()`` (same as the pilot prompt) so dead servers
    do not keep stale tools visible in the UI.
    """
    return 200, {
        "servers": svc.mcp.status(),
        "tools": [
            {
                "server": t.server,
                "name": t.name,
                "qualified": t.qualified,
                "description": t.description,
            }
            for t in svc.mcp.discovered_tools()
        ],
    }


def get_mcp_catalog() -> tuple[int, dict]:
    """GET /api/mcp/catalog — one-click seed catalog."""
    return 200, {"catalog": CATALOG}


def post_mcp_add(body: dict, svc: McpServices) -> tuple[int, dict]:
    """POST /api/mcp/add — persist server config and try to start it."""
    name = body.get("name", "")
    server = {
        k: body[k]
        for k in ("command", "args", "env", "cwd", "url", "headers")
        if k in body
    }
    svc.mcp.save_server(name, server)
    try:
        tools = svc.mcp.start_server(name)
        _fire_tools_changed(svc)
        return 200, {"ok": True, "tools": len(tools)}
    except Exception as e:
        _fire_tools_changed(svc)
        return 200, {"ok": False, "error": _lifecycle_error(e)}


def post_mcp_remove(body: dict, svc: McpServices) -> tuple[int, dict]:
    """POST /api/mcp/remove — drop config and stop the server."""
    svc.mcp.remove_server(body.get("name", ""))
    _fire_tools_changed(svc)
    return 200, {"ok": True}


def post_mcp_start(body: dict, svc: McpServices) -> tuple[int, dict]:
    """POST /api/mcp/start — start a configured server."""
    try:
        tools = svc.mcp.start_server(body.get("name", ""))
        _fire_tools_changed(svc)
        return 200, {"ok": True, "tools": len(tools)}
    except Exception as e:
        return 200, {"ok": False, "error": _lifecycle_error(e)}


def post_mcp_stop(body: dict, svc: McpServices) -> tuple[int, dict]:
    """POST /api/mcp/stop — stop a running server."""
    svc.mcp.stop_server(body.get("name", ""))
    _fire_tools_changed(svc)
    return 200, {"ok": True}


def post_mcp_refresh(body: dict, svc: McpServices) -> tuple[int, dict]:
    """POST /api/mcp/refresh — force reconnect (stop then start)."""
    try:
        tools = svc.mcp.refresh_server(body.get("name", ""))
        _fire_tools_changed(svc)
        return 200, {"ok": True, "tools": len(tools)}
    except Exception as e:
        _fire_tools_changed(svc)
        return 200, {"ok": False, "error": _lifecycle_error(e)}


def post_mcp_call(body: dict, svc: McpServices) -> tuple[int, dict]:
    """POST /api/mcp/call — invoke a qualified MCP tool."""
    args = body.get("arguments")
    if args is not None and not isinstance(args, dict):
        return 400, {"error": "arguments must be a dictionary"}
    try:
        out = svc.mcp.call(body.get("tool", ""), args or {})
        return 200, {"ok": True, "result": out}
    except Exception as e:
        # Same secret-masking as invocation receipts before the error reaches UI.
        return 200, {"ok": False, "error": redact_secret_text(str(e))}
