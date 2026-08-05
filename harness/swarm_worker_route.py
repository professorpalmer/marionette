from __future__ import annotations

"""Choose product swarm/implement worker adapter from live credentials.

Default remains agentic (keyed HTTP). When no agentic provider is keyed but
CURSOR_API_KEY is present, fall back to platform ``cursor`` so Cursor-plan
installs can still run swarms/implements. Agent-login alone (cursor-cli) is
never treated as sufficient for this path.
"""

from typing import Optional

from .diag import note as _diag


def resolve_product_worker_adapter(
    configured: Optional[str] = None,
) -> str:
    """Return ``agentic``, ``openai``, or ``cursor`` for product dispatches."""
    try:
        from .swarm_adapter import resolve_bridge_swarm_adapter

        base = resolve_bridge_swarm_adapter(configured)
    except Exception as e:
        _diag("swarm_worker_route.base", e)
        base = "agentic"

    if base in ("openai",):
        return base
    if base == "cursor":
        return "cursor" if _cursor_platform_ready() else "agentic"
    if base != "agentic":
        return "agentic"

    try:
        from .auto_registry import keyed_agentic_providers

        if keyed_agentic_providers():
            return "agentic"
    except Exception as e:
        _diag("swarm_worker_route.keyed", e)
        return "agentic"

    if _cursor_platform_ready():
        _diag(
            "swarm_worker_route.cursor_fallback",
            msg="no keyed agentic providers; CURSOR_API_KEY ready → cursor",
        )
        return "cursor"
    return "agentic"


def _cursor_platform_ready() -> bool:
    try:
        from .provider_capabilities import cursor_platform_workers_ready

        return bool(cursor_platform_workers_ready())
    except Exception as e:
        _diag("swarm_worker_route.cursor_ready", e)
        return False
