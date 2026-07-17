"""MCP client + manager against an in-repo fake stdio server (zero external deps)."""
import json
import sys
from pathlib import Path

from harness.mcp_client import StdioMcpClient, McpError
from harness.mcp_manager import CATALOG, McpManager

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


def test_catalog_includes_firecrawl():
    entry = CATALOG["firecrawl"]
    assert entry["command"] == "npx"
    assert "firecrawl-mcp" in entry["args"]
    assert entry["env_hint"] == ["FIRECRAWL_API_KEY"]
    assert "Firecrawl" in entry["desc"] or "firecrawl" in entry["desc"].lower()


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


def test_manager_refresh_reconnects(tmp_path):
    """Refresh must stop then start so a previously-failed server can recover."""
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    m.save_server("fake", {"command": sys.executable, "args": [FAKE]})
    try:
        m.start_server("fake")
        assert m.status()[0]["running"]
        tools = m.refresh_server("fake")
        assert {t.name for t in tools} == {"echo", "add"}
        st = m.status()[0]
        assert st["running"] and st["tools"] == 2 and not st["error"]
        # manage_mcp refresh action mirrors the HTTP endpoint.
        out = m.manage("refresh", name="fake")
        assert out["ok"] and out.get("refreshed") and out["tools"] == 2
    finally:
        m.stop_all()


def test_manager_refresh_clears_stale_error(tmp_path):
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    m.save_server("bad", {"command": "no-such-cmd-xyz-refresh"})
    m.save_server("fake", {"command": sys.executable, "args": [FAKE]})
    try:
        try:
            m.start_server("bad")
        except McpError:
            pass
        assert m.status()[0]["name"] == "bad" or any(s["error"] for s in m.status())
        # Bad stays errored; refresh on good still works independently.
        tools = m.refresh_server("fake")
        assert len(tools) == 2
        fake = next(s for s in m.status() if s["name"] == "fake")
        assert fake["running"] and not fake["error"]
    finally:
        m.stop_all()


def test_post_mcp_refresh_handler(tmp_path):
    from harness.api.mcp import McpServices, post_mcp_refresh

    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    m.save_server("fake", {"command": sys.executable, "args": [FAKE]})
    svc = McpServices(mcp=m)
    try:
        code, body = post_mcp_refresh({"name": "fake"}, svc)
        assert code == 200 and body["ok"] and body["tools"] == 2
        code, body = post_mcp_refresh({"name": "missing"}, svc)
        assert code == 200 and not body["ok"] and "error" in body
    finally:
        m.stop_all()

