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


def test_schemas_state_the_read_spill_limit_and_fresh_shell_cwd():
    from harness.pilot import build_tools_schema

    by_name = {_tool_name(t): (t.get("function") or t)["description"] for t in build_tools_schema()}
    assert "spill file" in by_name["read_file"] and "150 lines" in by_name["read_file"]
    assert "fresh shell in the workspace root" in by_name["run_command"]
    assert PILOT_SYSTEM.count("`job_findings`:") == 1
