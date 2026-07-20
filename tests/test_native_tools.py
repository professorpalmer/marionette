from __future__ import annotations
import json
import tempfile
import pytest
from typing import Any, Optional

from pmharness.reasoning import extract_reasoning
from pmharness.drivers.stub import StubDriver
from harness.pilot import build_tools_schema, parse_tool_calls, PilotAction, PilotTurn, PilotError
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, ConvEvent


class FakeMcpTool:
    def __init__(self, server: str, name: str, description: str, input_schema: dict):
        self.server = server
        self.name = name
        self.description = description
        self.input_schema = input_schema


def test_build_tools_schema():
    # Call build_tools_schema with built-ins only
    schemas = build_tools_schema()
    names = [s["function"]["name"] for s in schemas]
    assert "read_file" in names
    assert "write_file" in names
    assert "run_command" in names
    assert "list_dir" in names
    assert "web_search" in names
    assert "web_fetch" in names
    assert "read_pdf" in names
    assert "run_swarm" in names
    assert "search_codegraph" in names
    assert "query_wiki" in names

    # Call with MCP tools
    fake_tool = FakeMcpTool(
        server="todo",
        name="add_item",
        description="Add a todo item",
        input_schema={
            "type": "object",
            "properties": {"item": {"type": "string"}},
            "required": ["item"]
        }
    )
    schemas_mcp = build_tools_schema([fake_tool])
    mcp_names = [s["function"]["name"] for s in schemas_mcp]
    assert "mcp_todo__add_item" in mcp_names
    mcp_schema = [s for s in schemas_mcp if s["function"]["name"] == "mcp_todo__add_item"][0]
    assert mcp_schema["function"]["description"] == "Add a todo item"
    assert mcp_schema["function"]["parameters"]["required"] == ["item"]


def test_parse_tool_calls():
    # Standard tool call
    tc_read = [
        {
            "id": "tc1",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"path": "src/main.py"})
            }
        }
    ]
    actions = parse_tool_calls(tc_read)
    assert len(actions) == 1
    assert actions[0].kind == "read_file"
    assert actions[0].path == "src/main.py"
    assert actions[0].tool_call_id == "tc1"

    # MCP tool call (legacy single-underscore wire name still parses)
    tc_mcp = [
        {
            "id": "tc2",
            "type": "function",
            "function": {
                "name": "mcp_weather_get_forecast",
                "arguments": json.dumps({"location": "New York"})
            }
        }
    ]
    actions_mcp = parse_tool_calls(tc_mcp)
    assert len(actions_mcp) == 1
    assert actions_mcp[0].kind == "call_mcp"
    assert actions_mcp[0].tool == "weather.get_forecast"
    assert actions_mcp[0].arguments == {"location": "New York"}
    assert actions_mcp[0].tool_call_id == "tc2"


def test_parse_mcp_wire_name_handles_underscores():
    from harness.pilot import _parse_mcp_wire_name

    # New unambiguous encoding
    assert _parse_mcp_wire_name("mcp_my_server__add_item") == "my_server.add_item"
    assert _parse_mcp_wire_name("mcp_todo__add_item") == "todo.add_item"
    # Legacy mcp_{server}_{tool} (server without underscores)
    assert _parse_mcp_wire_name("mcp_weather_get_forecast") == "weather.get_forecast"


def test_extract_reasoning():
    # Case 1: Direct reasoning field
    msg1 = {"reasoning": "I think this is step 1."}
    assert extract_reasoning(msg1) == "I think this is step 1."

    # Case 2: Direct reasoning_content field
    msg2 = {"reasoning_content": "I think this is step 2."}
    assert extract_reasoning(msg2) == "I think this is step 2."

    # Case 3: reasoning_details field (OpenRouter array of objects)
    msg3 = {
        "reasoning_details": [
            {"type": "thinking", "thinking": "Step 3 details."}
        ]
    }
    assert extract_reasoning(msg3) == "Step 3 details."

    # Case 4: Inline <think> tag fallback
    msg4 = {"content": "Hello! <think>Inside the think block.</think> Some prose."}
    assert extract_reasoning(msg4) == "Inside the think block."

    # None case
    msg_empty = {"content": "Hello! Just prose."}
    assert extract_reasoning(msg_empty) == ""


def test_stub_driver_chat():
    driver = StubDriver()
    
    # First turn: should emit a deterministic tool call
    messages1 = [{"role": "user", "content": "How are you?"}]
    resp1 = driver.chat(messages1)
    assert resp1.meta["tool_calls"] is not None
    assert len(resp1.meta["tool_calls"]) == 1
    assert resp1.meta["tool_calls"][0]["function"]["name"] == "read_file"
    assert resp1.text == ""

    # Subsequent turn: has tool call in history, should return prose content
    messages2 = [
        {"role": "user", "content": "How are you?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": resp1.meta["tool_calls"]
        },
        {"role": "tool", "tool_call_id": "call_stub_1", "content": "File content"}
    ]
    resp2 = driver.chat(messages2)
    assert not resp2.meta.get("tool_calls")
    assert "Based on the tool execution" in resp2.text


class _FakeNativePilot:
    name = "fake-native-pilot"
    
    def __init__(self):
        self.calls = 0

    def complete(self, task_prompt: str, *, system: Optional[str] = None) -> Any:
        # Dummy to satisfy Driver interface
        from pmharness.drivers.openai_compat import DriverResponse
        return DriverResponse(text="")

    def chat(self, messages: list, *, tools: list | None = None, system: str | None = None) -> Any:
        from pmharness.drivers.openai_compat import DriverResponse
        self.calls += 1
        if self.calls == 1:
            # Emit a tool call in turn 1
            tool_calls = [
                {
                    "id": "tc_smoke_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "AGENTS.md"})
                    }
                }
            ]
            return DriverResponse(
                text="",
                tokens_out=15,
                latency_ms=1.0,
                meta={
                    "tool_calls": tool_calls,
                    "reasoning": "Need to check AGENTS.md first.",
                    "finish_reason": "tool_calls"
                }
            )
        else:
            # Emit final answer in turn 2
            return DriverResponse(
                text="I have read AGENTS.md and verified everything is fine.",
                tokens_out=20,
                latency_ms=1.0,
                meta={
                    "tool_calls": [],
                    "reasoning": "Already read it, now answering.",
                    "finish_reason": "stop"
                }
            )


class _FakeInlinePilot:
    name = "fake-inline-pilot"
    
    def __init__(self):
        self.calls = 0

    def complete(self, task_prompt: str, *, system: Optional[str] = None) -> Any:
        from pmharness.drivers.openai_compat import DriverResponse
        return DriverResponse(text="")

    def chat(self, messages: list, *, tools: list | None = None, system: str | None = None) -> Any:
        from pmharness.drivers.openai_compat import DriverResponse
        self.calls += 1
        if self.calls == 1:
            # Emit an inline tool call with empty structured tool_calls
            return DriverResponse(
                text='<function=read_file>\n<parameter=path>\nAGENTS.md\n</parameter>\n</function>\n</tool_call>',
                tokens_out=15,
                latency_ms=1.0,
                meta={
                    "tool_calls": [],
                    "reasoning": "Checking AGENTS.md inline.",
                    "finish_reason": "stop"
                }
            )
        else:
            return DriverResponse(
                text="I have read AGENTS.md via inline fallback and it is clean.",
                tokens_out=20,
                latency_ms=1.0,
                meta={
                    "tool_calls": [],
                    "reasoning": "Now answering after inline execution.",
                    "finish_reason": "stop"
                }
            )


def test_conversation_smoke_native_turn(monkeypatch):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp(), repo=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s.pilot = _FakeNativePilot()

    events = list(s.send("Please read AGENTS.md for me."))
    kinds = [e.kind for e in events]
    
    # Assert events emitted (no post-answer thinking/reasoning ConvEvent)
    assert "thinking" not in kinds
    assert "action_start" in kinds
    assert "action_result" in kinds
    assert "message" in kinds
    assert kinds[-1] == "assistant_done"

    # Verify history structure: role: tool message was appended
    tool_msgs = [m for m in s._history if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "tc_smoke_1"
    # Since AGENTS.md won't exist in the temp repo, it should have a "File not found" error string
    assert "File not found" in tool_msgs[0]["content"]

    # Verify assistant message has native tool_calls
    assistant_with_tools = [m for m in s._history if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(assistant_with_tools) == 1
    assert assistant_with_tools[0]["tool_calls"][0]["id"] == "tc_smoke_1"


def test_conversation_inline_fallback_turn():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp(), repo=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s.pilot = _FakeInlinePilot()

    events = list(s.send("Please read AGENTS.md."))
    kinds = [e.kind for e in events]
    
    assert "thinking" not in kinds
    assert "action_start" in kinds
    assert "action_result" in kinds
    assert "message" in kinds
    assert kinds[-1] == "assistant_done"

    # Verify history structure: role: tool message was appended
    tool_msgs = [m for m in s._history if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    # Check that synthetic tool call ID is used
    assert tool_msgs[0]["tool_call_id"] == "call_inline_1"
    assert "File not found" in tool_msgs[0]["content"]

    # Verify assistant message has native tool_calls
    assistant_with_tools = [m for m in s._history if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(assistant_with_tools) == 1
    assert assistant_with_tools[0]["tool_calls"][0]["id"] == "call_inline_1"


def test_parse_inline_tool_calls_qwen_shape():
    from harness.pilot import parse_inline_tool_calls
    
    # (a) parse_inline_tool_calls parses the EXACT qwen shape seen live
    content = '<function=read_file>\n<parameter=path>\nREADME.md\n</parameter>\n</function>\n</tool_call>'
    actions = parse_inline_tool_calls(content)
    assert len(actions) == 1
    assert actions[0].kind == "read_file"
    assert actions[0].path == "README.md"
    assert actions[0].tool_call_id == "call_inline_1"


def test_parse_inline_tool_calls_shape_b():
    from harness.pilot import parse_inline_tool_calls
    
    # (b) parses Shape B
    content = '<tool_call>\n{"name":"run_command","arguments":{"command":"ls"}}\n</tool_call>'
    actions = parse_inline_tool_calls(content)
    assert len(actions) == 1
    assert actions[0].kind == "run_command"
    assert actions[0].command == "ls"
    assert actions[0].tool_call_id == "call_inline_1"


def test_parse_inline_tool_calls_mcp():
    from harness.pilot import parse_inline_tool_calls
    
    # (c) parses an mcp_<server>_<tool> inline call
    content = '<tool_call>\n{"name":"mcp_todo_add_item","arguments":{"item":"buy milk"}}\n</tool_call>'
    actions = parse_inline_tool_calls(content)
    assert len(actions) == 1
    assert actions[0].kind == "call_mcp"
    assert actions[0].tool == "todo.add_item"
    assert actions[0].arguments == {"item": "buy milk"}
    assert actions[0].tool_call_id == "call_inline_1"


def test_parse_inline_tool_calls_multiple():
    from harness.pilot import parse_inline_tool_calls
    
    # (d) multiple <function=...> blocks
    content = (
        'Let me run two tools:\n'
        '<function=read_file>\n<parameter=path>\nfoo.py\n</parameter>\n</function>\n'
        'and then list:\n'
        '<function=list_dir>\n<parameter=path>\nbar\n</parameter>\n</function>'
    )
    actions = parse_inline_tool_calls(content)
    assert len(actions) == 2
    assert actions[0].kind == "read_file"
    assert actions[0].path == "foo.py"
    assert actions[1].kind == "list_dir"
    assert actions[1].path == "bar"


def test_parse_inline_tool_calls_truncated():
    from harness.pilot import parse_inline_tool_calls
    
    # (e) a truncated <function=list_dir><parameter=path>.</parameter> with no closing </function>
    content = '<function=list_dir><parameter=path>.</parameter>'
    actions = parse_inline_tool_calls(content)
    assert len(actions) == 1
    assert actions[0].kind == "list_dir"
    assert actions[0].path == "."


def test_strip_inline_tool_calls():
    from harness.pilot import strip_inline_tool_calls, parse_inline_tool_calls
    
    # (f) strip_inline_tool_calls removes the blocks, leaving real prose
    content = 'Hello! Let me do this for you:\n<function=read_file>\n<parameter=path>README.md</parameter></function>\nHope you like it!'
    stripped = strip_inline_tool_calls(content)
    assert stripped == 'Hello! Let me do this for you:\n\nHope you like it!'
    
    # No tool calls
    normal = 'Just some standard prose saying <function> is cool but not a tool call.'
    assert strip_inline_tool_calls(normal) == normal
    assert parse_inline_tool_calls(normal) == []

