"""McpManager.manage() coverage (list/add/start) for peel verification."""
import sys
from pathlib import Path

from harness.mcp_manager import McpManager

FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


def test_manager_manage_list_add_stdio(tmp_path):
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    listed = m.manage("list")
    assert listed["ok"] and listed["servers"] == []
    try:
        out = m.manage(
            "add",
            name="fake",
            command=sys.executable,
            args=[FAKE],
            confirm=True,
        )
        assert out["ok"] is True
        assert out["tools"] == 2
        assert out["transport"] == "stdio"
        st = m.manage("list")
        assert any(s["name"] == "fake" and s["running"] for s in st["servers"])
    finally:
        m.stop_all()


def test_manager_manage_add_url_only(tmp_path, monkeypatch):
    """Docker-style: save URL even if start fails (server not listening)."""
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    out = m.manage("add", name="discord-mcp", url="http://127.0.0.1:1/mcp")
    # Port 1 almost never hosts MCP; expect saved + start error, not reject.
    assert out.get("saved") or out.get("ok")
    assert "discord-mcp" in m.load_config()
    assert m.load_config()["discord-mcp"]["url"] == "http://127.0.0.1:1/mcp"


def test_stdio_add_requires_explicit_confirmation(tmp_path):
    """Adding a stdio server spawns a local process, so it must be deliberate.

    Without this, an "add" performs a silent process spawn -- the path a
    prompt-injected pilot (or anything else that can reach the manager) would
    take. The refusal must also leave nothing persisted and nothing running.
    """
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    try:
        out = m.manage("add", name="evil", command=sys.executable, args=[FAKE])
        assert out["ok"] is False
        assert out["requires_confirmation"] is True
        assert "confirm=true" in out["error"]
        assert "evil" not in m.load_config()
        assert not any(s["name"] == "evil" for s in m.manage("list")["servers"])
    finally:
        m.stop_all()


def test_http_url_add_needs_no_confirmation(tmp_path):
    """url=... is not a local process spawn, so it stays zero-friction."""
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    out = m.manage("add", name="http-only", url="http://127.0.0.1:1/mcp")
    assert not out.get("requires_confirmation")
    assert "http-only" in m.load_config()


def test_http_route_refuses_stdio_without_confirmation(tmp_path):
    """The HTTP route is the second entry point and must enforce the same rule.

    post_mcp_add does not go through manage(), so a gate added only there would
    have left /api/mcp/add -- the path the MCP pane and any other local client
    uses -- as a free bypass.
    """
    from harness.api.mcp import post_mcp_add

    m = McpManager(config_path=str(tmp_path / "mcp.json"))

    class _Svc:
        def __init__(self, manager):
            self.mcp = manager

    status, body = post_mcp_add(
        {"name": "evil", "command": "touch", "args": ["/tmp/pwned_marionette"]},
        _Svc(m),
    )
    assert status == 200
    assert body["ok"] is False
    assert body["requires_confirmation"] is True
    assert "evil" not in m.load_config()
