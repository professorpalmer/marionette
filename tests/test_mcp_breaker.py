from __future__ import annotations

import pytest

from harness.conversation import _mcp_result_text
from harness.mcp_client import McpError, McpRemoteError, McpTool
from harness.mcp_manager import McpManager


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Client:
    alive = True

    def __init__(self):
        self.results = []
        self.calls = 0

    def call_tool(self, name, arguments, timeout=120.0):
        self.calls += 1
        r = self.results.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    clock = _Clock()
    mgr = McpManager(config_path=str(tmp_path / "mcp.json"), clock=clock)
    client = _Client()
    mgr._clients["srv"] = client
    mgr._tools["srv.t"] = McpTool(server="srv", name="t", description="", input_schema={})
    return mgr, client, clock


OK = {"content": [{"type": "text", "text": "fine"}]}


def _fail(mgr, client, n):
    for _ in range(n):
        client.results.append(McpError("MCP server 'srv': timeout waiting for tools/call"))
        with pytest.raises(McpError, match="timeout"):
            mgr.call("srv.t", {})


def test_opens_after_three_transport_failures_and_fails_fast(setup):
    mgr, client, clock = setup
    _fail(mgr, client, 3)
    with pytest.raises(McpError) as ei:
        mgr.call("srv.t", {})
    msg = str(ei.value)
    assert "failed 3 times in a row" in msg
    assert "pause until 00:17:40 UTC" in msg  # 1000 s + 60 s after the epoch
    assert "Do not retry this server now" in msg
    assert client.calls == 3
    assert mgr._last_invocations["srv"]["ok"] is False


def test_failures_spread_beyond_window_do_not_open(setup):
    mgr, client, clock = setup
    _fail(mgr, client, 2)
    clock.t += 61
    _fail(mgr, client, 2)
    client.results.append(OK)
    assert mgr.call("srv.t", {}) == OK


def test_half_open_probe_success_resets(setup):
    mgr, client, clock = setup
    _fail(mgr, client, 3)
    clock.t += 61
    client.results.append(OK)
    assert mgr.call("srv.t", {}) == OK
    _fail(mgr, client, 2)
    client.results.append(OK)
    assert mgr.call("srv.t", {}) == OK


def test_half_open_probe_failure_reopens_at_once(setup):
    mgr, client, clock = setup
    _fail(mgr, client, 3)
    clock.t += 61
    _fail(mgr, client, 1)
    with pytest.raises(McpError, match="failed 4 times in a row"):
        mgr.call("srv.t", {})
    assert client.calls == 4


def test_success_resets_count(setup):
    mgr, client, clock = setup
    _fail(mgr, client, 2)
    client.results.append(OK)
    mgr.call("srv.t", {})
    _fail(mgr, client, 2)
    client.results.append(OK)
    assert mgr.call("srv.t", {}) == OK


def test_application_errors_do_not_count(setup):
    mgr, client, clock = setup
    rejected = {"isError": True, "content": [{"type": "text", "text": "bad path"}]}
    for _ in range(4):
        client.results.append(dict(rejected))
        out = mgr.call("srv.t", {})
        text = _mcp_result_text(out)
        assert "answered and rejected this call" in text
        assert "check your arguments" in text
        assert "bad path" in text
    for _ in range(4):
        client.results.append(McpRemoteError("tools/call -> {'code': -32602}"))
        with pytest.raises(McpRemoteError):
            mgr.call("srv.t", {})
    client.results.append(OK)
    assert mgr.call("srv.t", {}) == OK
    assert client.calls == 9
