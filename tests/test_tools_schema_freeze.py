from __future__ import annotations

"""Conversation-scoped tools[] snapshot: reuse until an explicit hatch."""

import json
import tempfile
from types import SimpleNamespace
from typing import Any, Optional

from harness.api.mcp import McpServices, post_mcp_refresh
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.mcp_client import McpTool
from harness.pilot import PilotAction
from harness.prompt_cache_scope import (
    clear_stable_prefixes,
    prompt_cache_scope,
    register_stable_prefix,
)
from harness.send_loop_phases import dispatch_local_action


def _mcp_tool(server: str, name: str, description: str = "d") -> McpTool:
    return McpTool(
        server=server,
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
    )


class _FreezePilot:
    name = "tools-schema-freeze-pilot"

    def __init__(self) -> None:
        self.tools_seen: list = []
        self.calls = 0

    def complete(self, task_prompt: str, *, system: Optional[str] = None) -> Any:
        from pmharness.drivers.openai_compat import DriverResponse

        return DriverResponse(text="ok", tokens_out=1, latency_ms=1.0)

    def chat(
        self,
        messages: list,
        *,
        tools: Optional[list] = None,
        system: Optional[str] = None,
    ) -> Any:
        from pmharness.drivers.openai_compat import DriverResponse

        self.calls += 1
        self.tools_seen.append(tools)
        return DriverResponse(
            text="ok",
            tokens_out=1,
            latency_ms=1.0,
            meta={"tool_calls": [], "finish_reason": "stop"},
        )


class _MutableMcp:
    def __init__(self, tools: Optional[list] = None) -> None:
        self._tools = list(tools or [])
        self.refresh_calls: list = []

    def discovered_tools(self) -> list:
        return list(self._tools)

    def manage(self, action: str, **_kwargs: Any) -> dict:
        return {"ok": True, "action": action}

    def effective_config(self) -> dict:
        return {t.server: {} for t in self._tools}

    def refresh_server(self, name: str) -> list:
        self.refresh_calls.append(name)
        return [t for t in self._tools if t.server == name]


def _session(mcp: Optional[_MutableMcp] = None) -> ConversationalSession:
    cfg = HarnessConfig(
        driver="stub-oracle-v2",
        state_dir=tempfile.mkdtemp(),
        repo=tempfile.mkdtemp(),
    )
    session = ConversationalSession(cfg)
    session.pilot = _FreezePilot()
    if mcp is not None:
        session._mcp = mcp
    return session


def _schema_bytes(schema: list) -> str:
    return json.dumps(schema, sort_keys=True, separators=(",", ":"))


def test_first_send_captures_snapshot_second_reuses_object(monkeypatch):
    monkeypatch.setenv("HARNESS_TOOL_DISCOVERY", "1")
    mcp = _MutableMcp([])
    session = _session(mcp)
    list(session.send("first turn"))
    first = session._tools_schema_snapshot
    assert first is not None
    assert session.pilot.tools_seen
    assert session.pilot.tools_seen[0] is first
    first_bytes = _schema_bytes(first)

    mcp._tools.append(_mcp_tool("github", "create_issue", "Create issue"))
    list(session.send("second turn"))
    second = session._build_visible_tools_schema()
    assert second is first
    assert _schema_bytes(second) == first_bytes
    assert session.pilot.tools_seen[-1] is first


def test_reload_hatch_rebuilds_after_discovered_tools_change(monkeypatch):
    monkeypatch.setenv("HARNESS_TOOL_DISCOVERY", "0")
    monkeypatch.setattr(
        "harness.browser.standalone_browser_available",
        lambda **_k: False,
    )
    mcp = _MutableMcp([])
    session = _session(mcp)
    first = session._build_visible_tools_schema()
    first_names = {t["function"]["name"] for t in first}
    assert "mcp_github__create_issue" not in first_names

    mcp._tools.append(_mcp_tool("github", "create_issue", "Create issue"))
    reused = session._build_visible_tools_schema()
    assert reused is first
    assert "mcp_github__create_issue" not in {
        t["function"]["name"] for t in reused
    }

    session._invalidate_tools_schema()
    rebuilt = session._build_visible_tools_schema()
    assert rebuilt is not first
    assert "mcp_github__create_issue" in {
        t["function"]["name"] for t in rebuilt
    }


def test_reload_mcp_tools_is_explicit_hatch(monkeypatch):
    monkeypatch.setenv("HARNESS_TOOL_DISCOVERY", "1")
    mcp = _MutableMcp([_mcp_tool("github", "create_issue")])
    session = _session(mcp)
    first = session._build_visible_tools_schema()
    assert session._tools_schema_snapshot is first
    out = session.reload_mcp_tools()
    assert out["ok"] is True
    assert out["reloaded"] is True
    assert session._tools_schema_snapshot is None
    assert "github" in mcp.refresh_calls
    rebuilt = session._build_visible_tools_schema()
    assert rebuilt is not first


def test_slash_reload_mcp_hatches_without_pilot_call(monkeypatch):
    monkeypatch.setenv("HARNESS_TOOL_DISCOVERY", "1")
    mcp = _MutableMcp([_mcp_tool("github", "create_issue")])
    session = _session(mcp)
    session._build_visible_tools_schema()
    assert session._tools_schema_snapshot is not None
    events = list(session.send("/reload-mcp"))
    assert session.pilot.calls == 0
    assert session._tools_schema_snapshot is None
    kinds = [e.kind for e in events]
    assert "message" in kinds
    assert "assistant_done" in kinds


def test_discovery_enabled_toggle_rebuilds(monkeypatch):
    monkeypatch.setenv("HARNESS_TOOL_DISCOVERY", "1")
    session = _session()
    first = session._build_visible_tools_schema()
    names_on = {t["function"]["name"] for t in first}
    assert "search_tools" in names_on
    monkeypatch.setenv("HARNESS_TOOL_DISCOVERY", "0")
    rebuilt = session._build_visible_tools_schema()
    assert rebuilt is not first
    assert "search_tools" not in {t["function"]["name"] for t in rebuilt}


def test_manage_mcp_refresh_hatches_list_does_not():
    session = SimpleNamespace(
        _tools_schema_snapshot=[{"frozen": True}],
        _mcp=SimpleNamespace(manage=lambda *_a, **_k: {"ok": True}),
        _history=[],
    )

    def _invalidate() -> None:
        session._tools_schema_snapshot = None

    session._invalidate_tools_schema = _invalidate
    session._append_action_result = lambda *_a, **_k: None

    list(
        dispatch_local_action(
            session,
            PilotAction(kind="manage_mcp", arguments={"action": "list"}),
            "a-list",
            False,
            [],
        )
    )
    assert session._tools_schema_snapshot == [{"frozen": True}]

    list(
        dispatch_local_action(
            session,
            PilotAction(
                kind="manage_mcp",
                arguments={"action": "refresh", "name": "x"},
            ),
            "a-refresh",
            False,
            [],
        )
    )
    assert session._tools_schema_snapshot is None


def test_api_mcp_refresh_fires_tools_changed():
    hits: list = []
    mcp = SimpleNamespace(
        refresh_server=lambda _name: [_mcp_tool("github", "create_issue")],
    )
    svc = McpServices(mcp=mcp, on_tools_changed=lambda: hits.append(True))
    code, body = post_mcp_refresh({"name": "github"}, svc)
    assert code == 200 and body["ok"] is True
    assert hits == [True]


def test_prompt_cache_scope_unchanged_across_frozen_sends(monkeypatch):
    monkeypatch.setenv("HARNESS_TOOL_DISCOVERY", "1")
    mcp = _MutableMcp([])
    session = _session(mcp)
    session.conversation_key = "lineage-ROOT-tools-freeze"
    list(session.send("first"))
    prefix = session._history[0]["content"]
    clear_stable_prefixes()
    register_stable_prefix("system_v1", prefix)
    key_before = session._prompt_cache_conversation_key()
    scope_before = prompt_cache_scope(key_before)
    mcp._tools.append(_mcp_tool("github", "create_issue"))
    list(session.send("second"))
    assert session._prompt_cache_conversation_key() == key_before
    assert prompt_cache_scope(session._prompt_cache_conversation_key()) == scope_before
    clear_stable_prefixes()
