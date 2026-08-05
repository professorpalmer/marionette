from __future__ import annotations

"""Live swarm-adapter resolution.

Product rule: Marionette NEVER runs or surfaces the demo substrate in the
shipping product. Demo findings look like a successful audit and are worse
than a loud failure.

Demo is opt-in only via ``HARNESS_ALLOW_DEMO_SWARM=1`` (driver-eval benches
and hermetic tests that intentionally exercise the local substrate).
``HARNESS_SWARM_ADAPTER=demo`` alone is not enough -- that value is often
process-poison from an early boot ``from_env()`` before workspace.json
restore, not a user choice.

Default for any live product path is ``agentic``.
"""

import os
from typing import Any, Optional

# Adapters the bridge can actually drive for product analysis.
# ``cursor`` is the platform Cursor SDK path (CURSOR_API_KEY), not agent-login.
REAL_SWARM_ADAPTERS = frozenset({"agentic", "openai", "cursor"})

DEMO_REFUSED_MSG = "demo substrate -- not real codebase analysis"


def allow_demo_swarm() -> bool:
    """True only when demo is explicitly opted in for eval/tests.

    Production default is False. Pytest does NOT imply allow -- tests that
    need the local substrate must set ``HARNESS_ALLOW_DEMO_SWARM=1``.
    """
    flag = (os.environ.get("HARNESS_ALLOW_DEMO_SWARM") or "").strip().lower()
    return flag in ("1", "true", "yes")


def normalize_swarm_adapter(raw: str) -> str:
    name = (raw or "").strip().lower()
    if not name:
        return "agentic"
    # cursor-cli (agent login) is never a swarm adapter — map to agentic so
    # product resolution can still fall through to CURSOR_API_KEY cursor later.
    if name in ("cursor-cli",):
        return "agentic"
    if name in ("cursor", "cursor-sdk"):
        return "cursor"
    if name == "demo":
        if allow_demo_swarm():
            return "demo"
        return "agentic"
    return name


def ensure_repo_swarm_adapter(cfg: Any) -> bool:
    """Force a real adapter on cfg (and env). Returns True when changed."""
    current = normalize_swarm_adapter(str(getattr(cfg, "swarm_adapter", "") or ""))
    if current in REAL_SWARM_ADAPTERS:
        if (os.environ.get("HARNESS_SWARM_ADAPTER") or "").strip().lower() != current:
            os.environ["HARNESS_SWARM_ADAPTER"] = current
        if str(getattr(cfg, "swarm_adapter", "") or "").lower() != current:
            cfg.swarm_adapter = current
            return True
        return False
    if current == "demo" and allow_demo_swarm():
        return False
    cfg.swarm_adapter = "agentic"
    os.environ["HARNESS_SWARM_ADAPTER"] = "agentic"
    return True


def publish_swarm_adapter(adapter: str, *, repo: str = "") -> None:
    """Publish adapter to the process env. Never stamps demo in product mode."""
    del repo  # reserved for callers; product path ignores demo regardless
    name = normalize_swarm_adapter(adapter)
    if name == "demo":
        if allow_demo_swarm():
            os.environ["HARNESS_SWARM_ADAPTER"] = "demo"
        else:
            os.environ["HARNESS_SWARM_ADAPTER"] = "agentic"
        return
    if name in REAL_SWARM_ADAPTERS:
        os.environ["HARNESS_SWARM_ADAPTER"] = name
        return
    os.environ["HARNESS_SWARM_ADAPTER"] = "agentic"


def resolve_bridge_swarm_adapter(
    configured: Optional[str] = None,
    *,
    repo_cwd: str = "",
) -> str:
    """Adapter the bridge must use for this execute_intent call.

    Never returns ``demo`` unless ``HARNESS_ALLOW_DEMO_SWARM=1``.
    Product default is always ``agentic`` (openai only when explicitly set).
    """
    del repo_cwd  # product path does not special-case empty repo to demo
    raw = configured
    if raw is None:
        raw = os.environ.get("HARNESS_SWARM_ADAPTER", "")
    name = (str(raw or "")).strip().lower()
    if name in REAL_SWARM_ADAPTERS:
        return name
    if name in ("cursor", "cursor-sdk"):
        return "cursor"
    if name == "cursor-cli":
        return "agentic"
    if name == "demo" and allow_demo_swarm():
        return "demo"
    return "agentic"


def refuse_demo_result(adapter: str) -> bool:
    """True when a bridge/dispatch result must be treated as a hard failure."""
    return (adapter or "").strip().lower() == "demo" and not allow_demo_swarm()
