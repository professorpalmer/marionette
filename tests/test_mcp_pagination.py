from __future__ import annotations

import pytest

from harness.mcp_client import McpError, StdioMcpClient
from harness.mcp_http_client import HttpMcpClient
from harness.tool_discovery import mcp_list_all_tools


def _tool(name):
    return {"name": name, "inputSchema": {"type": "object"}}


class _Pages:
    """Fake tools/list: one result per call, recording params."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def __call__(self, params, timeout):
        self.calls.append(dict(params))
        return self.pages[len(self.calls) - 1]


def test_single_page_without_cursor_sends_one_empty_request():
    req = _Pages([{"tools": [_tool("a")]}])
    out = mcp_list_all_tools("s", req)
    assert [t["name"] for t in out["tools"]] == ["a"]
    assert req.calls == [{}]


def test_follows_next_cursor_until_absent():
    req = _Pages([
        {"tools": [_tool("a")], "nextCursor": "c1"},
        {"tools": [_tool("b")], "nextCursor": "c2"},
        {"tools": [_tool("c")]},
    ])
    out = mcp_list_all_tools("s", req)
    assert [t["name"] for t in out["tools"]] == ["a", "b", "c"]
    assert req.calls == [{}, {"cursor": "c1"}, {"cursor": "c2"}]


def test_repeated_cursor_is_an_error():
    req = _Pages([
        {"tools": [_tool("a")], "nextCursor": "c1"},
        {"tools": [_tool("b")], "nextCursor": "c2"},
        {"tools": [_tool("c")], "nextCursor": "c1"},
    ])
    with pytest.raises(McpError, match="repeated_cursor"):
        mcp_list_all_tools("s", req)


def test_oversized_cursor_is_an_error():
    req = _Pages([{"tools": [], "nextCursor": "x" * (64 * 1024 + 1)}])
    with pytest.raises(McpError, match="cursor_size"):
        mcp_list_all_tools("s", req)


def test_page_limit():
    counter = {"n": 0}

    def endless(params, timeout):
        counter["n"] += 1
        return {"tools": [], "nextCursor": "c%d" % counter["n"]}

    with pytest.raises(McpError, match="max_pages"):
        mcp_list_all_tools("s", endless)
    assert counter["n"] == 100


def test_tool_limit():
    counter = {"n": 0}

    def big(params, timeout):
        counter["n"] += 1
        return {"tools": [_tool("t") for _ in range(1000)], "nextCursor": "c%d" % counter["n"]}

    with pytest.raises(McpError, match="max_tools"):
        mcp_list_all_tools("s", big)
    assert counter["n"] == 3


def test_one_deadline_for_the_whole_listing():
    now = {"t": 0.0}
    timeouts = []

    def slow(params, timeout):
        timeouts.append(timeout)
        now["t"] += 25.0
        return {"tools": [_tool("t")], "nextCursor": "c%d" % len(timeouts)}

    with pytest.raises(McpError, match="deadline"):
        mcp_list_all_tools("s", slow, deadline_s=60.0, clock=lambda: now["t"])
    assert timeouts == [60.0, 35.0, 10.0]


def test_stdio_client_list_tools_paginates():
    client = StdioMcpClient("s", "true")
    sent = []
    pages = {None: {"tools": [_tool("a")], "nextCursor": "n"}, "n": {"tools": [_tool("b")]}}

    def fake_request(method, params, timeout=60.0):
        sent.append((method, dict(params)))
        return pages[params.get("cursor")]

    client._request = fake_request
    assert [t.name for t in client.list_tools()] == ["a", "b"]
    assert sent == [("tools/list", {}), ("tools/list", {"cursor": "n"})]


def test_http_client_list_tools_paginates():
    client = HttpMcpClient("s", "http://127.0.0.1:9/mcp")
    pages = {None: {"tools": [_tool("a")], "nextCursor": "n"}, "n": {"tools": [_tool("b")]}}
    client._request = lambda method, params, timeout=60.0: pages[params.get("cursor")]
    assert [t.name for t in client.list_tools()] == ["a", "b"]
