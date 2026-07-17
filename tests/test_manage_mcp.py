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
