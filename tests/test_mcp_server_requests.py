from __future__ import annotations

"""MCP stdio: answer server requests and cancel a timed-out call."""

import json
import sys
import textwrap

import pytest

from harness.mcp_client import McpError, StdioMcpClient

# Tool "ping_first" sends a server ping that reuses the id of the client's
# own tools/call before it answers. Tool "ask" sends a request the client
# does not support. Tool "hang" never answers. Tool "seen" reports the
# client replies and cancel notices the server got.
_SERVER = textwrap.dedent('''
    import json, sys
    seen = {"replies": [], "cancelled": []}
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()
    def result(mid, text):
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}})
    for line in sys.stdin:
        msg = json.loads(line)
        mid, method = msg.get("id"), msg.get("method")
        if method is None:
            seen["replies"].append(msg)
            continue
        if mid is None:
            if method == "notifications/cancelled":
                seen["cancelled"].append(msg["params"])
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {"tools": {}}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": []}})
        elif method == "tools/call":
            name = msg["params"]["name"]
            if name == "ping_first":
                send({"jsonrpc": "2.0", "id": mid, "method": "ping"})
                reply = json.loads(sys.stdin.readline())
                result(mid, "pong=" + json.dumps(reply.get("result")))
            elif name == "ask":
                send({"jsonrpc": "2.0", "id": "srv-1", "method": "sampling/createMessage", "params": {}})
                reply = json.loads(sys.stdin.readline())
                result(mid, "code=" + str(reply.get("error", {}).get("code")))
            elif name == "hang":
                continue
            else:
                result(mid, json.dumps(seen["cancelled"]))
''')


@pytest.fixture
def client(tmp_path):
    script = tmp_path / "server_requests.py"
    script.write_text(_SERVER, encoding="utf-8")
    c = StdioMcpClient("fake", sys.executable, [str(script)])
    c.start()
    yield c
    c.stop()


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def test_server_ping_with_a_colliding_id_is_answered_not_taken_as_the_reply(client):
    out = client.call_tool("ping_first", {}, timeout=10)
    assert _text(out) == "pong={}"


def test_unsupported_server_request_gets_method_not_found(client):
    out = client.call_tool("ask", {}, timeout=10)
    assert _text(out) == "code=-32601"


def test_timed_out_call_sends_a_cancel_notice(client):
    with pytest.raises(McpError):
        client.call_tool("hang", {}, timeout=0.5)
    cancelled = json.loads(_text(client.call_tool("seen", {}, timeout=10)))
    assert len(cancelled) == 1
    assert isinstance(cancelled[0]["requestId"], int)
    assert "tools/call" in cancelled[0]["reason"]
