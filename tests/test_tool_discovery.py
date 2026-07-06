"""Tests for on-demand tool discovery (search_tools + ToolCatalog)."""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Optional

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, ConvEvent
from harness.mcp_client import McpTool
from harness.pilot import build_tools_schema, parse_tool_calls
from harness.tool_discovery import (
    ToolCatalog,
    CORE_PILOT,
    CORE_WORKER,
    _normalize_path_text,
    discovery_enabled,
)


@pytest.fixture(autouse=True)
def _discovery_on(monkeypatch):
    monkeypatch.setenv("HARNESS_TOOL_DISCOVERY", "1")


def _mcp_tool(server: str, name: str, description: str) -> McpTool:
    return McpTool(
        server=server,
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )


def test_ranking_prefers_github_for_issue_query():
    catalog = ToolCatalog()
    catalog.refresh(
        mcp_tools=[
            _mcp_tool("filesystem", "read_file", "Read a file from C:\\Users\\dev\\repo"),
            _mcp_tool("github", "create_issue", "Create a new GitHub issue with title and body"),
            _mcp_tool("github", "search_code", "Search code across GitHub repositories"),
        ],
    )
    hits = catalog.search("github issue create", limit=5)
    names = [h.entry.qualified for h in hits]
    assert names[0].startswith("github.")
    assert "filesystem.read_file" not in names[:2]


def test_core_tools_always_visible():
    catalog = ToolCatalog()
    catalog.refresh(mcp_tools=[_mcp_tool("github", "create_issue", "Create issue")])
    schema = catalog.visible_schema()
    names = {t["function"]["name"] for t in schema}
    for core in CORE_PILOT:
        assert core in names
    assert "mcp_github_create_issue" not in names


def test_hidden_mcp_visible_after_activation():
    mcp = [_mcp_tool("github", "create_issue", "Create issue")]
    catalog = ToolCatalog()
    catalog.refresh(mcp_tools=mcp)
    catalog.activate(["github.create_issue"])
    schema = catalog.visible_schema(mcp_tools=mcp)
    names = {t["function"]["name"] for t in schema}
    assert "mcp_github_create_issue" in names


def test_worker_core_excludes_delegation():
    catalog = ToolCatalog()
    catalog.refresh(no_delegation=True)
    schema = catalog.visible_schema(no_delegation=True)
    names = {t["function"]["name"] for t in schema}
    for core in CORE_WORKER:
        assert core in names
    assert "run_implement" not in names
    assert "run_swarm" not in names


def test_windows_path_safe_mcp_metadata():
    raw = "Read/write under C:\\Users\\pwall\\Projects\\marionette\\data"
    normalized = _normalize_path_text(raw)
    assert "\\" not in normalized
    assert "C:/Users/pwall/Projects/marionette/data" in normalized

    catalog = ToolCatalog()
    tool = _mcp_tool("filesystem", "write_file", raw)
    catalog.refresh(mcp_tools=[tool])
    entry = next(e for e in catalog._entries.values() if e.source == "mcp")
    assert "\\" not in entry.description
    response = catalog.format_search_response("filesystem write", limit=5)
    assert "\\" not in response


def test_stable_output_size_cap():
    catalog = ToolCatalog()
    many = [
        _mcp_tool("srv", f"tool_{i}", f"Description number {i} " * 20)
        for i in range(40)
    ]
    catalog.refresh(mcp_tools=many)
    out = catalog.format_search_response("", limit=25)
    assert len(out) <= 8000
    payload = json.loads(out)
    assert payload["count"] <= 25


def test_discovery_disabled_exposes_all_tools(monkeypatch):
    monkeypatch.setenv("HARNESS_TOOL_DISCOVERY", "0")
    mcp = [_mcp_tool("github", "create_issue", "Create issue")]
    catalog = ToolCatalog()
    catalog.refresh(mcp_tools=mcp)
    schema = catalog.visible_schema(mcp_tools=mcp)
    names = {t["function"]["name"] for t in schema}
    assert "mcp_github_create_issue" in names
    assert "run_swarm" in names
    assert "search_tools" not in names


def test_build_tools_schema_includes_search_tools_when_requested():
    schema = build_tools_schema(include_search_tools=True)
    names = {t["function"]["name"] for t in schema}
    assert "search_tools" in names


def test_parse_search_tools_native_call():
    tool_calls = [
        {
            "id": "tc_search",
            "type": "function",
            "function": {
                "name": "search_tools",
                "arguments": json.dumps(
                    {"query": "browser screenshot", "activate": ["browser_screenshot"]}
                ),
            },
        }
    ]
    actions = parse_tool_calls(tool_calls)
    assert len(actions) == 1
    assert actions[0].kind == "search_tools"
    assert actions[0].query == "browser screenshot"
    assert actions[0].arguments["activate"] == ["browser_screenshot"]


def test_search_tools_handler_on_session():
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession
    from harness.pilot import PilotAction

    cfg = HarnessConfig(repo=os.getcwd())
    session = ConversationalSession(cfg)
    session._tool_catalog.refresh(
        mcp_tools=[_mcp_tool("github", "create_issue", "Create a GitHub issue")],
    )
    act = PilotAction(
        kind="search_tools",
        query="github issue",
        arguments={"limit": 3, "activate": ["github.create_issue"]},
    )
    ok, status, text = session._do_search_tools(act)
    assert ok is True
    payload = json.loads(text)
    assert payload["activated"] == ["mcp:github.create_issue"]
    assert payload["count"] >= 1
    assert "github.create_issue" in {r["qualified"] for r in payload["results"]}


def test_discovery_enabled_default():
    assert discovery_enabled() is True


class _SearchToolsLoopPilot:
    name = "search-tools-loop-pilot"

    def __init__(self):
        self.calls = 0

    def complete(self, task_prompt: str, *, system: Optional[str] = None) -> Any:
        from pmharness.drivers.openai_compat import DriverResponse
        return DriverResponse(text="")

    def chat(self, messages: list, *, tools: list | None = None, system: str | None = None) -> Any:
        from pmharness.drivers.openai_compat import DriverResponse
        self.calls += 1
        if self.calls == 1:
            tool_calls = [
                {
                    "id": "tc_search_lsp",
                    "type": "function",
                    "function": {
                        "name": "search_tools",
                        "arguments": json.dumps(
                            {"query": "lsp diagnostics", "activate": ["lsp"]}
                        ),
                    },
                }
            ]
            return DriverResponse(
                text="",
                tokens_out=10,
                latency_ms=1.0,
                meta={"tool_calls": tool_calls, "finish_reason": "tool_calls"},
            )
        return DriverResponse(
            text="Activated lsp via search_tools.",
            tokens_out=8,
            latency_ms=1.0,
            meta={"tool_calls": [], "finish_reason": "stop"},
        )


def test_search_tools_loop_executes_and_activates(monkeypatch):
    monkeypatch.setenv("HARNESS_TOOL_DISCOVERY", "1")
    cfg = HarnessConfig(
        driver="stub-oracle-v2",
        state_dir=tempfile.mkdtemp(),
        repo=tempfile.mkdtemp(),
    )
    session = ConversationalSession(cfg)
    session.pilot = _SearchToolsLoopPilot()

    schema_before = {t["function"]["name"] for t in session._build_visible_tools_schema()}
    assert "lsp" not in schema_before

    events = list(session.send("Find the lsp tool and activate it."))
    action_results = [e for e in events if e.kind == "action_result"]
    assert action_results, "expected at least one action_result event"
    assert not any(e.data.get("error") for e in action_results), (
        "search_tools action_result should not carry error"
    )

    tool_msgs = [m for m in session._history if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "search_tools returned" in tool_msgs[0]["content"]
    assert "lsp" in tool_msgs[0]["content"].lower()

    schema_after = {t["function"]["name"] for t in session._build_visible_tools_schema()}
    assert "lsp" in schema_after


def test_visible_schema_parity_when_discovery_disabled(monkeypatch):
    monkeypatch.setenv("HARNESS_TOOL_DISCOVERY", "0")
    cfg = HarnessConfig(
        driver="stub-oracle-v2",
        state_dir=tempfile.mkdtemp(),
        repo=tempfile.mkdtemp(),
    )
    session = ConversationalSession(cfg)
    mcp = [_mcp_tool("github", "create_issue", "Create issue")]
    session._tool_catalog.refresh(mcp_tools=mcp)

    visible = session._build_visible_tools_schema()
    full = build_tools_schema(
        mcp,
        no_delegation=getattr(cfg, "no_delegation", False),
        browser_enabled=getattr(cfg, "browser_enabled", True),
    )
    visible_names = sorted(t["function"]["name"] for t in visible)
    full_names = sorted(t["function"]["name"] for t in full)
    assert visible_names == full_names


def test_search_state_hidden_until_activated():
    catalog = ToolCatalog()
    catalog.refresh()
    schema_before = catalog.visible_schema()
    names_before = {t["function"]["name"] for t in schema_before}
    assert "search_state" not in names_before

    catalog.activate(["search_state"])
    schema_after = catalog.visible_schema()
    names_after = {t["function"]["name"] for t in schema_after}
    assert "search_state" in names_after
