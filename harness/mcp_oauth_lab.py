"""Default-disabled MCP OAuth lab.

Provider OAuth (xAI, Nous, Codex) is a separate, already-shipped path.
This gate refuses MCP-server OAuth client registration unless the operator
opts in. Off is the only safe default.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

LAB_ENV = "HARNESS_MCP_OAUTH_LAB"
_TRUTHY = ("1", "true", "yes", "on")


def mcp_oauth_lab_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    values = env if env is not None else os.environ
    raw = (values.get(LAB_ENV) or "").strip().lower()
    return raw in _TRUTHY


def refuse_mcp_oauth(action: str = "start") -> dict:
    return {
        "allowed": False,
        "action": action,
        "error": (
            "MCP OAuth lab is default-disabled. "
            "Set HARNESS_MCP_OAUTH_LAB=1 only on an isolated lab host."
        ),
    }


def gate_mcp_oauth(action: str = "start", env: Optional[Mapping[str, str]] = None) -> dict:
    if mcp_oauth_lab_enabled(env):
        return {"allowed": True, "action": action}
    return refuse_mcp_oauth(action)
