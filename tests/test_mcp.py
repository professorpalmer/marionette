"""MCP client + manager against an in-repo fake stdio server (zero external deps)."""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import harness.mcp_client as mcp_mod
from harness.mcp_client import StdioMcpClient, McpError
from harness.mcp_manager import CATALOG, McpManager
from harness.tool_discovery import mcp_tools_from_list_result

FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


def _client():
    return StdioMcpClient(name="fake", command=sys.executable, args=[FAKE])


@pytest.fixture
def fake_manager(tmp_path):
    """A manager with the fake stdio server saved but not started. Stops all servers after."""
    m = McpManager(config_path=str(tmp_path / "mcp.json"))
    m.save_server("fake", {"command": sys.executable, "args": [FAKE]})
    yield m
    m.stop_all()


def test_mixed_tools_list_payload_keeps_only_valid_rows():
    result = {
        "tools": [
            {
                "name": "good",
                "description": "ok",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "missing_type",
                "description": "ok",
                "inputSchema": {"properties": {}},
            },
            {"name": "", "description": "missing name", "inputSchema": {"type": "object"}},
            {"description": "no name key", "inputSchema": {"type": "object"}},
            {"name": "string_params", "inputSchema": {"type": "object", "parameters": "{}"}},
            {"name": "list_props", "inputSchema": {"type": "object", "properties": []}},
            {"name": "not_object", "inputSchema": {"type": "string"}},
            {"name": "schema_is_string", "inputSchema": "object"},
            "not-a-dict",
        ]
    }
    tools = mcp_tools_from_list_result("mixed", result)
    assert [t.name for t in tools] == ["good", "missing_type"]
    assert all(t.server == "mixed" for t in tools)


def test_list_tools_mixed_payload_skips_unusable():
    c = StdioMcpClient(name="mixed", command="x")

    def fake_request(method, params, timeout=60.0):
        assert method == "tools/list"
        return {
            "tools": [
                {"name": "good", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "bad", "inputSchema": {"type": "string"}},
                {"name": "also_good", "inputSchema": {}},
            ]
        }

    c._request = fake_request
    tools = c.list_tools()
    assert [t.name for t in tools] == ["good", "also_good"]


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


def test_manager_start_call_status(fake_manager):
    m = fake_manager
    tools = m.start_server("fake")
    assert {t.name for t in tools} == {"echo", "add"}
    st = m.status()
    assert st[0]["running"] and st[0]["tools"] == 2
    out = m.call("fake.echo", {"text": "hi"})
    assert out["content"][0]["text"] == "hi"


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


def test_lazy_boot_status_idle_and_discovered_empty(fake_manager):
    """Configured-but-never-started servers are idle, not errors; tools empty."""
    m = fake_manager
    st = m.status()
    assert len(st) == 1
    assert st[0]["name"] == "fake"
    assert st[0]["running"] is False
    assert st[0]["lifecycle"] == "idle"
    assert st[0]["tools"] == 0
    assert not st[0].get("error")
    assert m.discovered_tools() == []


def test_status_lifecycle_running_stopped_and_error(fake_manager):
    """lifecycle distinguishes idle, running, operator-stopped, and start errors."""
    m = fake_manager
    m.save_server("bad", {"command": "no-such-cmd-xyz-lifecycle"})
    idle = next(s for s in m.status() if s["name"] == "fake")
    assert idle["lifecycle"] == "idle"
    assert idle["running"] is False

    m.start_server("fake")
    running = next(s for s in m.status() if s["name"] == "fake")
    assert running["lifecycle"] == "running"
    assert running["running"] is True

    m.stop_server("fake")
    stopped = next(s for s in m.status() if s["name"] == "fake")
    assert stopped["lifecycle"] == "stopped"
    assert stopped["running"] is False
    assert not stopped["error"]

    try:
        m.start_server("bad")
    except McpError:
        pass
    errored = next(s for s in m.status() if s["name"] == "bad")
    assert errored["lifecycle"] == "error"
    assert errored["running"] is False
    assert errored["error"]


def test_ensure_server_starts_only_when_not_alive(fake_manager):
    m = fake_manager
    tools = m.ensure_server("fake")
    assert {t.name for t in tools} == {"echo", "add"}
    assert m.status()[0]["running"]
    # Second call must not restart (same tool set, still alive).
    again = m.ensure_server("fake")
    assert {t.name for t in again} == {"echo", "add"}
    assert m.status()[0]["running"]


def test_call_starts_missing_client_without_prior_start(fake_manager):
    """Lazy connect: call() starts a configured idle server on first use."""
    m = fake_manager
    assert m.discovered_tools() == []
    out = m.call("fake.echo", {"text": "hi"})
    assert out["content"][0]["text"] == "hi"
    assert m.status()[0]["running"]
    assert any(t.qualified == "fake.echo" for t in m.discovered_tools())


def test_manager_refresh_reconnects(fake_manager):
    """Refresh must stop then start so a previously-failed server can recover."""
    m = fake_manager
    m.start_server("fake")
    assert m.status()[0]["running"]
    tools = m.refresh_server("fake")
    assert {t.name for t in tools} == {"echo", "add"}
    st = m.status()[0]
    assert st["running"] and st["tools"] == 2 and not st["error"]
    # manage_mcp refresh action mirrors the HTTP endpoint.
    out = m.manage("refresh", name="fake")
    assert out["ok"] and out.get("refreshed") and out["tools"] == 2


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


def test_post_mcp_refresh_handler(fake_manager):
    from harness.api.mcp import McpServices, post_mcp_refresh

    hatches: list = []
    svc = McpServices(mcp=fake_manager, on_tools_changed=lambda: hatches.append(True))
    code, body = post_mcp_refresh({"name": "fake"}, svc)
    assert code == 200 and body["ok"] and body["tools"] == 2
    assert hatches, "refresh must hatch the frozen tools[] snapshot"
    code, body = post_mcp_refresh({"name": "missing"}, svc)
    assert code == 200 and not body["ok"] and "error" in body


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


def test_allowed_tools_absent_means_all_tools(fake_manager):
    m = fake_manager
    tools = m.start_server("fake")
    assert {t.name for t in tools} == {"echo", "add"}
    st = next(s for s in m.status() if s["name"] == "fake")
    assert "allowed_tools" not in st


def test_refresh_interrupted_by_concurrent_stop(fake_manager, monkeypatch):
    """A stop mid-refresh must not leave the server running afterward."""
    import threading
    import time

    m = fake_manager
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
                c.call_tool("slow", {"seconds": 5}, timeout=10.0)
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


def test_start_reaps_child_when_handshake_times_out(tmp_path):
    """start() must stop() the process group if initialize never answers."""
    script = tmp_path / "mute_mcp.py"
    script.write_text(
        "import time\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    spawned = []
    real_popen = subprocess.Popen

    def _capture_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    orig = mcp_mod.subprocess.Popen
    mcp_mod.subprocess.Popen = _capture_popen
    c = StdioMcpClient(
        name="mute",
        command=sys.executable,
        args=[str(script)],
        startup_timeout=0.4,
    )
    try:
        with pytest.raises(McpError, match="timeout waiting for initialize"):
            c.start()
        assert spawned, "start() never spawned a child"
        child = spawned[0]
        deadline = time.time() + 3
        while child.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert child.poll() is not None, "handshake timeout left the MCP child running"
        assert c._proc is None
    finally:
        mcp_mod.subprocess.Popen = orig
        try:
            c.stop()
        except Exception:
            pass
        for proc in spawned:
            if proc.poll() is None:
                proc.kill()


class _McpPilot:
    """Says it'll use a tool, fires one call_mcp, then finishes."""
    name = "mcp-pilot"
    def __init__(self): self.n = 0
    def complete(self, prompt, *, system=None):
        from pmharness.drivers.openai_compat import DriverResponse

        self.n += 1
        if self.n == 1:
            t = '{"say":"calling echo","actions":[{"kind":"call_mcp","tool":"fake.echo","arguments":{"text":"pong"}}]}'
        else:
            t = '{"say":"Done.","actions":[]}'
        return DriverResponse(text=t, tokens_out=10, latency_ms=1.0)


@pytest.mark.swarm  # constructs a ConversationalSession (pilot build)
def test_pilot_calls_mcp_tool(tmp_path):
    """Pilot -> call_mcp -> MCP manager end-to-end (fake stdio server, no network)."""
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path))
    s = ConversationalSession(cfg)
    s.pilot = _McpPilot()
    m = McpManager(config_path=str(tmp_path / "mcp.json"))
    m.save_server("fake", {"command": sys.executable, "args": [FAKE]})
    m.start_server("fake")
    s._mcp = m
    try:
        events = list(s.send("use the echo tool"))
        results = [e for e in events if e.kind == "action_result"]
        assert results, "expected an action_result from the mcp call"
        assert results[0].data.get("adapter") == "mcp"
        assert "pong" in results[0].data["artifacts"][0]["headline"]
    finally:
        m.stop_all()

