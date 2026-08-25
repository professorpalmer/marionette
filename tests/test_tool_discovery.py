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
from harness.pilot import build_tools_schema, is_invalid_action, parse_tool_calls
from harness.pilot_tool_recovery import parse_native_tool_turn
from harness.tool_discovery import (
    ToolCatalog,
    CORE_PILOT,
    CORE_WORKER,
    _normalize_path_text,
    discovery_enabled,
    is_lazy_activatable_name,
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
    assert "mcp_github__create_issue" not in names


def test_hidden_mcp_visible_after_activation():
    mcp = [_mcp_tool("github", "create_issue", "Create issue")]
    catalog = ToolCatalog()
    catalog.refresh(mcp_tools=mcp)
    catalog.activate(["github.create_issue"])
    schema = catalog.visible_schema(mcp_tools=mcp)
    names = {t["function"]["name"] for t in schema}
    assert "mcp_github__create_issue" in names


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
    assert "mcp_github__create_issue" in names
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
    # Pin browser capability so parity does not depend on whether the host that
    # runs the suite happens to have a standalone Chrome installed.
    monkeypatch.setattr(
        "harness.browser.standalone_browser_available", lambda **_k: True,
    )
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


def test_search_state_visible_in_core_pilot():
    """Durable recall must be core-visible before swarm redispatch gates."""
    from harness.tool_discovery import CORE_PILOT

    assert "search_state" in CORE_PILOT
    catalog = ToolCatalog()
    catalog.refresh()
    names = {t["function"]["name"] for t in catalog.visible_schema()}
    assert "search_state" in names


def test_core_always_includes_hash_edit_when_enabled(monkeypatch):
    from harness.tool_discovery import _core_always

    monkeypatch.setenv("HARNESS_HASH_EDIT", "1")
    assert "hash_edit" in _core_always()
    monkeypatch.setenv("HARNESS_HASH_EDIT", "0")
    assert "hash_edit" not in _core_always()


def test_core_always_survives_hash_edit_import_failure(monkeypatch):
    """Import failures must not drop the core set; diag note absorbs the error."""
    import harness.tool_discovery as td
    import sys
    from types import ModuleType

    monkeypatch.setenv("HARNESS_HASH_EDIT", "1")
    boom = ModuleType("harness.hash_edit")

    def _raise():
        raise RuntimeError("simulated import failure")

    boom.hash_edit_enabled = _raise  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "harness.hash_edit", boom)
    core = td._core_always()
    assert "read_file" in core
    assert "hash_edit" not in core


def _fn_names(schema):
    return {t["function"]["name"] for t in schema}


def _tc(name, args=None, tc_id="tc_lazy"):
    return {
        "id": tc_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args if args is not None else {"query": "marionette"}),
        },
    }


class _CatalogSession:
    def __init__(self, catalog):
        self._tool_catalog = catalog
        self._history = []
        self._synthetic_tool_call_seq = 0


def test_hidden_web_and_browser_not_in_visible_schema(monkeypatch):
    monkeypatch.setattr(
        "harness.browser.standalone_browser_available", lambda **_k: True,
    )
    catalog = ToolCatalog()
    catalog.refresh(browser_enabled=True)
    names = _fn_names(catalog.visible_schema(browser_enabled=True))
    assert "web_search" not in names
    assert "web_fetch" not in names
    assert not any(n.startswith("browser_") for n in names)
    assert is_lazy_activatable_name("web_search")
    assert is_lazy_activatable_name("web_fetch")
    assert is_lazy_activatable_name("browser_navigate")
    assert not is_lazy_activatable_name("read_file")


def test_first_activate_resolves_on_empty_catalog(monkeypatch):
    """A real first key must not resolve to [] before refresh (repeat no-op bug)."""
    monkeypatch.setattr(
        "harness.browser.standalone_browser_available", lambda **_k: True,
    )
    catalog = ToolCatalog()
    activated = catalog.activate(["web_search"])
    assert "builtin:web_search" in activated
    assert "web_search" in _fn_names(catalog.visible_schema())


def test_search_tools_activate_is_not_a_noop(monkeypatch):
    monkeypatch.setattr(
        "harness.browser.standalone_browser_available", lambda **_k: True,
    )
    catalog = ToolCatalog()
    catalog.refresh(browser_enabled=True)
    assert "web_search" not in _fn_names(catalog.visible_schema())
    first = catalog.format_search_response("", activate=["web_search"])
    payload = json.loads(first)
    assert "builtin:web_search" in payload["activated"]
    assert "web_search" in _fn_names(catalog.visible_schema())
    # Second activate must still resolve and keep the tool visible (not a cache no-op).
    second = catalog.activate(["web_search"])
    assert "builtin:web_search" in second
    assert "web_search" in _fn_names(catalog.visible_schema())
    third = catalog.format_search_response("web", activate=["web_fetch"])
    payload3 = json.loads(third)
    assert "builtin:web_fetch" in payload3["activated"]
    assert "web_fetch" in _fn_names(catalog.visible_schema())


def test_lazy_activate_first_call_is_not_invalid_tool(monkeypatch):
    monkeypatch.setattr(
        "harness.browser.standalone_browser_available", lambda **_k: True,
    )
    catalog = ToolCatalog()
    catalog.refresh(browser_enabled=True)
    schema = catalog.visible_schema(browser_enabled=True)
    assert "web_search" not in _fn_names(schema)
    session = _CatalogSession(catalog)
    turn, _calls, _content = parse_native_tool_turn(
        "",
        [_tc("web_search", {"query": "marionette tracker"}, tc_id="tc_web")],
        "",
        schema,
        session=session,
    )
    assert len(turn.actions) == 1
    assert turn.actions[0].kind == "web_search"
    assert not is_invalid_action(turn.actions[0])
    assert "web_search" in _fn_names(catalog.visible_schema())

    schema2 = catalog.visible_schema()
    # web_fetch still hidden until first call / activate
    assert "web_fetch" not in _fn_names(schema2)
    turn2, _, _ = parse_native_tool_turn(
        "",
        [_tc("web_fetch", {"url": "https://example.com"}, tc_id="tc_fetch")],
        "",
        schema2,
        session=session,
    )
    assert turn2.actions[0].kind == "web_fetch"
    assert not is_invalid_action(turn2.actions[0])

    schema3 = catalog.visible_schema(browser_enabled=True)
    assert "browser_navigate" not in _fn_names(schema3)
    turn3, _, _ = parse_native_tool_turn(
        "",
        [_tc("browser_navigate", {"url": "https://example.com"}, tc_id="tc_b")],
        "",
        schema3,
        session=session,
    )
    assert turn3.actions[0].kind == "browser_navigate"
    assert not is_invalid_action(turn3.actions[0])


def test_unknown_hidden_name_stays_invalid_tool():
    catalog = ToolCatalog()
    catalog.refresh()
    schema = catalog.visible_schema()
    session = _CatalogSession(catalog)
    turn, _, _ = parse_native_tool_turn(
        "",
        [_tc("not_a_real_tool", {"q": 1}, tc_id="tc_bad")],
        "",
        schema,
        session=session,
    )
    assert is_invalid_action(turn.actions[0])
    assert "INVALID TOOL CALL" in (turn.actions[0].content or "")
