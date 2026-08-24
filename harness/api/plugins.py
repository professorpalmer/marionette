"""Agent Plugins v1 HTTP route bodies (list / enable / disable / install)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from ..agent_plugins import AgentPluginError
from ..plugin_registry import (
    disable_plugin,
    enable_plugin,
    discover_plugins,
    install_from_source,
    namespaced_mcp_ids_for_plugin,
    plugin_record_to_dict,
)


@dataclass
class PluginServices:
    """Optional MCP manager so enable/disable can start/stop portable servers."""

    mcp: Any = None
    diag: Optional[Callable[..., None]] = None


JsonPayload = Dict[str, Any]


def get_plugins(svc: PluginServices) -> Tuple[int, JsonPayload]:
    """GET /api/plugins — installed portable packages + enablement."""
    del svc  # listing does not need MCP
    try:
        records = discover_plugins()
    except Exception as exc:
        return 200, {"plugins": [], "error": str(exc)}
    return 200, {"plugins": [plugin_record_to_dict(r) for r in records]}


def _install_source_from_body(body: dict) -> object:
    """Accept path, source string, or a resolved source object."""
    source = body.get("source")
    if isinstance(source, dict) or (isinstance(source, str) and source.strip()):
        return source
    return (body.get("path") or "").strip()


def post_plugins_install(body: dict, svc: PluginServices) -> Tuple[int, JsonPayload]:
    """POST /api/plugins/install — path or resolved git/https/github source."""
    del svc
    source = _install_source_from_body(body)
    try:
        record = install_from_source(source)
    except AgentPluginError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except Exception as exc:
        return 400, {"ok": False, "error": str(exc)}
    return 200, {"ok": True, "plugin": plugin_record_to_dict(record)}


def _start_plugin_mcp(svc: PluginServices, plugin_id: str) -> None:
    mcp = getattr(svc, "mcp", None)
    if mcp is None:
        return
    for name in namespaced_mcp_ids_for_plugin(plugin_id):
        try:
            mcp.start_server(name)
        except Exception as exc:
            note = getattr(svc, "diag", None)
            if note:
                note("agent_plugins.mcp_start", msg=f"{name}: {exc}")


def _stop_plugin_mcp(svc: PluginServices, plugin_id: str) -> None:
    mcp = getattr(svc, "mcp", None)
    if mcp is None:
        return
    for name in namespaced_mcp_ids_for_plugin(plugin_id):
        try:
            mcp.stop_server(name)
        except Exception:
            pass


def post_plugins_enable(body: dict, svc: PluginServices) -> Tuple[int, JsonPayload]:
    """POST /api/plugins/enable — enable package and start its stdio MCP."""
    plugin_id = (body.get("id") or body.get("plugin_id") or "").strip()
    try:
        record = enable_plugin(plugin_id)
    except AgentPluginError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except Exception as exc:
        return 400, {"ok": False, "error": str(exc)}
    _start_plugin_mcp(svc, plugin_id)
    return 200, {"ok": True, "plugin": plugin_record_to_dict(record)}


def post_plugins_disable(body: dict, svc: PluginServices) -> Tuple[int, JsonPayload]:
    """POST /api/plugins/disable — disable package and stop its MCP servers."""
    plugin_id = (body.get("id") or body.get("plugin_id") or "").strip()
    _stop_plugin_mcp(svc, plugin_id)
    try:
        record = disable_plugin(plugin_id)
    except AgentPluginError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except Exception as exc:
        return 400, {"ok": False, "error": str(exc)}
    return 200, {"ok": True, "plugin": plugin_record_to_dict(record)}
