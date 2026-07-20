"""MCP client + manager against an in-repo fake stdio server (zero external deps)."""
import json
import sys
from pathlib import Path

import pytest

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


def test_get_mcp_uses_discovered_tools_alive_only():
    """GET /api/mcp must mirror pilot: alive-only tools, not stale cache."""
    from unittest.mock import MagicMock

    from harness.api.mcp import McpServices, get_mcp
    from harness.mcp_client import McpTool

    alive = McpTool(server="a", name="echo", description="alive")
    dead = McpTool(server="b", name="gone", description="stale")
    mcp = MagicMock()
    mcp.status.return_value = [{"name": "a", "running": True}]
    mcp.discovered_tools.return_value = [alive]
    mcp.tools.return_value = [alive, dead]
    code, body = get_mcp(McpServices(mcp=mcp))
    assert code == 200
    assert [t["qualified"] for t in body["tools"]] == [alive.qualified]
    mcp.discovered_tools.assert_called_once()
    mcp.tools.assert_not_called()


def test_status_tools_count_zero_when_client_dead_but_cached(tmp_path):
    """Dead-but-not-stopped clients must not report cached tool rows as live."""
    from harness.mcp_client import McpTool

    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    m.save_server("zombie", {"command": "x", "args": []})

    class DeadClient:
        alive = False

        def stop(self):
            pass

    m._clients["zombie"] = DeadClient()
    m._tools["zombie.echo"] = McpTool(server="zombie", name="echo", description="stale")
    m._tools["zombie.add"] = McpTool(server="zombie", name="add", description="stale")

    st = next(s for s in m.status() if s["name"] == "zombie")
    assert st["running"] is False
    assert st["tools"] == 0
    # Cache may still hold rows until stop/start; discovered_tools stays empty.
    assert m.tools()  # raw cache still has the stale rows
    assert m.discovered_tools() == []


def test_start_server_does_not_hold_lock_across_handshake(tmp_path, monkeypatch):
    """Manager lock HOL: stop/status must proceed while start() handshake runs."""
    import threading

    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    m.save_server("slow", {"command": "x", "args": []})

    entered = threading.Event()
    release = threading.Event()

    class SlowClient:
        def __init__(self, *a, **k):
            pass

        @property
        def alive(self):
            return False

        def start(self):
            entered.set()
            assert release.wait(5), "test timed out waiting for release"

        def list_tools(self):
            return []

        def stop(self):
            pass

    monkeypatch.setattr("harness.mcp_manager.StdioMcpClient", SlowClient)

    def run_start():
        try:
            m.start_server("slow")
        except Exception:
            pass

    t = threading.Thread(target=run_start, daemon=True)
    t.start()
    assert entered.wait(2), "slow start never entered handshake"
    # If start_server still held _lock across client.start(), this deadlocks.
    acquired = m._lock.acquire(timeout=1.0)
    assert acquired, "start_server held _lock across handshake (HOL)"
    m._lock.release()
    release.set()
    t.join(3)


def test_redact_mcp_secrets_masks_env_and_headers():
    from harness.mcp_manager import redact_mcp_secrets

    out = redact_mcp_secrets({
        "ok": True,
        "env": {"TOKEN": "secret"},
        "headers": {"Authorization": "Bearer x"},
        "nested": {"env": {"KEY": "v"}},
    })
    assert out["ok"] is True
    assert out["env"]["TOKEN"] == "REDACTED"
    assert out["headers"]["Authorization"] == "REDACTED"
    assert out["nested"]["env"]["KEY"] == "REDACTED"


def test_allowed_tools_filters_discovery_and_blocks_call(tmp_path):
    """Optional allowed_tools: discovery hides others; call rejects off-list."""
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    m.save_server(
        "fake",
        {
            "command": sys.executable,
            "args": [FAKE],
            "allowed_tools": ["echo"],
        },
    )
    try:
        tools = m.start_server("fake")
        assert {t.name for t in tools} == {"echo"}
        st = next(s for s in m.status() if s["name"] == "fake")
        assert st["allowed_tools"] == ["echo"]
        assert st["tools"] == 1
        out = m.call("fake.echo", {"text": "hi"})
        assert "hi" in json.dumps(out)
        with pytest.raises(McpError, match="allowlist"):
            m.call("fake.add", {"a": 1, "b": 2})
    finally:
        m.stop_all()


def test_allowed_tools_absent_means_all_tools(tmp_path):
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    m.save_server("fake", {"command": sys.executable, "args": [FAKE]})
    try:
        tools = m.start_server("fake")
        assert {t.name for t in tools} == {"echo", "add"}
        st = next(s for s in m.status() if s["name"] == "fake")
        assert "allowed_tools" not in st
    finally:
        m.stop_all()


def test_refresh_interrupted_by_concurrent_stop(tmp_path, monkeypatch):
    """A stop mid-refresh must not leave the server running afterward."""
    import threading
    import time

    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    m.save_server("fake", {"command": sys.executable, "args": [FAKE]})
    m.start_server("fake")
    assert m.status()[0]["running"]

    started = threading.Event()
    release = threading.Event()
    real_start = StdioMcpClient.start

    def slow_start(self):
        started.set()
        release.wait(timeout=5)
        return real_start(self)

    monkeypatch.setattr(StdioMcpClient, "start", slow_start)
    err = []

    def _refresh():
        try:
            m.refresh_server("fake")
        except McpError as e:
            err.append(str(e))

    t = threading.Thread(target=_refresh, daemon=True)
    t.start()
    assert started.wait(timeout=3)
    m.stop_server("fake")
    release.set()
    t.join(timeout=6)
    assert err, "refresh should fail when stop supersedes it"
    assert "superseded" in err[0] or "interrupted" in err[0]
    # Give the superseded start a moment to tear down its client.
    time.sleep(0.2)
    assert not m.status()[0]["running"]
    m.stop_all()


def test_stdio_cancel_unblocks_without_killing_server():
    """cancel() must wake waiters and leave the process alive for reuse."""
    import threading
    import time
    from pathlib import Path

    slow = str(Path(__file__).parent / "fixtures" / "fake_mcp_server_slow.py")
    c = StdioMcpClient(name="slow", command=sys.executable, args=[slow])
    c.start()
    try:
        errors = []

        def _call():
            try:
                c.call_tool("slow", {"seconds": 30}, timeout=10.0)
            except McpError as e:
                errors.append(str(e))

        t = threading.Thread(target=_call, daemon=True)
        t.start()
        time.sleep(0.3)
        assert c.alive
        n = c.cancel()
        assert n >= 1
        t.join(timeout=3)
        assert errors and "cancelled" in errors[0]
        assert c.alive
        # Server still usable after cancel.
        tools = c.list_tools()
        assert any(t.name == "slow" for t in tools)
    finally:
        c.stop()

