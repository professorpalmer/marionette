from __future__ import annotations

"""A stdio MCP server that stops answering is restarted, not probed for ever."""

import sys
import textwrap

import pytest

from harness.mcp_client import McpTimeoutError, StdioMcpClient
from harness.mcp_manager import McpManager

_SERVER = textwrap.dedent('''
    import json, os, sys
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()
    for line in sys.stdin:
        msg = json.loads(line)
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {"tools": {}}}})
        elif method == "tools/list":
            tools = [{"name": n, "inputSchema": {"type": "object"}} for n in ("who", "hang")]
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
        elif method == "tools/call" and msg["params"]["name"] == "who":
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"content": [{"type": "text", "text": str(os.getpid())}]}})
''')


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    script = tmp_path / "stuck_server.py"
    script.write_text(_SERVER, encoding="utf-8")
    original = StdioMcpClient.call_tool
    monkeypatch.setattr(
        StdioMcpClient, "call_tool",
        lambda self, name, arguments, timeout=120.0: original(self, name, arguments, timeout=0.5),
    )
    m = McpManager(config_path=str(tmp_path / "mcp.json"))
    m.save_server("stuck", {"command": sys.executable, "args": [str(script)]})
    m.start_server("stuck")
    yield m
    m.stop_server("stuck")


def _pid(m):
    return int(m.call("stuck.who", {})["content"][0]["text"])


def test_two_timeouts_in_a_row_restart_the_server(mgr):
    first = _pid(mgr)
    old_client = mgr._clients["stuck"]
    with pytest.raises(McpTimeoutError) as one:
        mgr.call("stuck.hang", {})
    assert "was stopped" not in str(one.value)
    assert _pid(mgr) == first  # an answer in between resets the count
    with pytest.raises(McpTimeoutError):
        mgr.call("stuck.hang", {})
    with pytest.raises(McpTimeoutError) as two:
        mgr.call("stuck.hang", {})
    assert "was stopped. The next call starts it again." in str(two.value)
    second = _pid(mgr)
    assert second != first
    assert not old_client.alive
