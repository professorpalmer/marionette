"""Every tool PILOT_SYSTEM tells the pilot to call must be callable.

A tool named in the prompt but hidden from the catalog (and not lazily
activatable) bounced the call and cost a round trip; the mandatory LATENCY
rule pointed at run_command_batch, which was hidden.
"""
from __future__ import annotations

import re

from harness.pilot import PILOT_SYSTEM
from harness.tool_discovery import ToolCatalog, is_lazy_activatable_name


def _tool_name(schema: dict) -> str:
    return (schema.get("function") or schema).get("name", "")


def test_prompt_named_tools_are_visible_or_lazily_activatable():
    catalog = ToolCatalog()
    catalog.refresh(mcp_tools=[], browser_enabled=False)
    tools = {tid.split(":", 1)[1] for tid in catalog._entries if tid.startswith("builtin:")}
    visible = {_tool_name(t) for t in catalog.visible_schema(profile="standard")}
    named = set(re.findall(r"`([a-z_]+)`", PILOT_SYSTEM)) & tools
    stranded = sorted(n for n in named if n not in visible and not is_lazy_activatable_name(n))
    assert named, "prompt tool names should resolve against the catalog"
    assert stranded == []
