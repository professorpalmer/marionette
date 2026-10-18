from __future__ import annotations

"""MCP notifications/tools/list_changed: refresh once at the next step."""

import json
import sys
import textwrap

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.mcp_http_client import HttpMcpClient
from harness.mcp_manager import McpManager

# Stdio server. Tool "grow" adds tool "extra" and sends `n` list_changed
# notifications before its response. Tool "break_list" makes tools/list fail.
# Tool "stats" reports how many tools/list calls the server saw.
_SERVER = textwrap.dedent('''
    import json, sys
    tools = [{"name": "base", "description": "b", "inputSchema": {"type": "object"}}]
    state = {"lists": 0, "broken": False}
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()
    for line in sys.stdin:
        msg = json.loads(line)
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {"tools": {"listChanged": True}}}})
        elif method == "tools/list":
            state["lists"] += 1
            if state["broken"]:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": "boom"}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
        elif method == "tools/call":
            p = msg["params"]
            name, args = p["name"], p.get("arguments") or {}
            if name == "grow":
                tools.append({"name": "extra", "description": "e", "inputSchema": {"type": "object"}})
                for _ in range(int(args.get("n", 1))):
                    send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
                text = "grew"
            elif name == "break_list":
                state["broken"] = True
                tools.append({"name": "extra", "description": "e", "inputSchema": {"type": "object"}})
                send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
                text = "broken"
            elif name == "log":
                send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}})
                text = "logged"
            else:
                text = json.dumps({"lists": state["lists"]})
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}})
''')


@pytest.fixture
def mgr(tmp_path):
    script = tmp_path / "list_changed_server.py"
    script.write_text(_SERVER, encoding="utf-8")
    m = McpManager(config_path=str(tmp_path / "mcp.json"))
    m.save_server("fake", {"command": sys.executable, "args": [str(script)]})
    m.start_server("fake")
    yield m
    m.stop_all()


def _names(m):
    return sorted(t.qualified for t in m.discovered_tools())


def _server_lists(m):
    out = m.call("fake.stats", {})
    return json.loads(out["content"][0]["text"])["lists"]


def test_notification_then_next_step_sees_new_tool(mgr):
    mgr.call("fake.grow", {"n": 1})
    assert _names(mgr) == ["fake.base"]
    assert mgr.refresh_changed_tools() is True
    assert _names(mgr) == ["fake.base", "fake.extra"]


def test_two_notifications_give_one_refresh(mgr):
    assert _server_lists(mgr) == 1
    mgr.call("fake.grow", {"n": 2})
    assert mgr.refresh_changed_tools() is True
    assert mgr.refresh_changed_tools() is False
    assert _server_lists(mgr) == 2


def test_no_notification_gives_no_refresh(mgr):
    mgr.call("fake.log", {})
    for _ in range(3):
        assert mgr.refresh_changed_tools() is False
    assert _server_lists(mgr) == 1


def test_refresh_failure_keeps_old_tools(mgr):
    mgr.call("fake.break_list", {})
    assert mgr.refresh_changed_tools() is False
    assert _names(mgr) == ["fake.base"]
    assert _server_lists(mgr) == 2
    # A remote error waits for the next notification. No retry storm.
    assert mgr.refresh_changed_tools() is False
    assert _server_lists(mgr) == 2


def test_step_schema_picks_up_new_tool(mgr):
    s = ConversationalSession(HarnessConfig())
    s._mcp = mgr

    def catalog_ids():
        s._build_visible_tools_schema()
        return {k for k in s._tool_catalog._entries if k.startswith("mcp:")}

    assert catalog_ids() == {"mcp:fake.base"}
    mgr.call("fake.grow", {"n": 1})
    assert catalog_ids() == {"mcp:fake.base", "mcp:fake.extra"}


def test_http_sse_notification_bumps_revision():
    client = HttpMcpClient("h", "http://127.0.0.1:1/rpc")
    raw = (
        'data: {"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n\n'
        'data: {"jsonrpc":"2.0","method":"notifications/message","params":{}}\n\n'
        'data: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
    )

    class _Resp:
        headers = {"Content-Type": "text/event-stream"}

        def read(self, n):
            return raw.encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    client._opener = type("O", (), {"open": lambda self, req, timeout: _Resp()})()
    msg = client._post({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}, 1.0)
    assert msg == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert client.tools_revision.value == 1
