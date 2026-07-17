"""MCP client + manager against an in-repo fake stdio server (zero external deps)."""
import json
import sys
from pathlib import Path

from harness.mcp_client import StdioMcpClient, McpError
from harness.mcp_manager import McpManager

FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


def _client():
    return StdioMcpClient(name="fake", command=sys.executable, args=[FAKE])


def test_client_handshake_and_list():
    c = _client()
    c.start()
    try:
        assert c.alive
        tools = c.list_tools()
        names = {t.name for t in tools}
        assert names == {"echo", "add"}
        assert tools[0].qualified.startswith("fake.")
    finally:
        c.stop()
    assert not c.alive


def test_client_call_tool():
    c = _client()
    c.start()
    try:
        r = c.call_tool("echo", {"text": "hello"})
        assert r["content"][0]["text"] == "hello"
        r2 = c.call_tool("add", {"a": 2, "b": 3})
        assert r2["content"][0]["text"] == "5"
    finally:
        c.stop()


def test_client_missing_command():
    c = StdioMcpClient(name="nope", command="definitely-not-a-real-cmd-xyz")
    try:
        c.start()
        assert False, "should have raised"
    except McpError as e:
        assert "not found" in str(e).lower() or "no such" in str(e).lower()


def test_manager_config_roundtrip(tmp_path):
    import os
    import stat
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    assert m.load_config() == {}
    m.save_server("fake", {"command": sys.executable, "args": [FAKE]})
    assert "fake" in m.load_config()

    if os.name == 'posix':
        mode = os.stat(str(cfgp)).st_mode
        assert stat.S_IMODE(mode) == 0o600

    m.remove_server("fake")
    assert "fake" not in m.load_config()


def test_manager_start_call_status(tmp_path):
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    m.save_server("fake", {"command": sys.executable, "args": [FAKE]})
    try:
        tools = m.start_server("fake")
        assert {t.name for t in tools} == {"echo", "add"}
        st = m.status()
        assert st[0]["running"] and st[0]["tools"] == 2
        out = m.call("fake.echo", {"text": "hi"})
        assert out["content"][0]["text"] == "hi"
    finally:
        m.stop_all()


def test_manager_start_all_reports_errors(tmp_path):
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    m.save_server("good", {"command": sys.executable, "args": [FAKE]})
    m.save_server("bad", {"command": "no-such-cmd-xyz"})
    try:
        report = m.start_all()
        assert report["good"] == 2
        assert isinstance(report["bad"], str) and "error" in report["bad"]
    finally:
        m.stop_all()

