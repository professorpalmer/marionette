from __future__ import annotations

from harness.mcp_oauth_lab import gate_mcp_oauth, mcp_oauth_lab_enabled, refuse_mcp_oauth


def test_mcp_oauth_lab_default_disabled(monkeypatch):
    monkeypatch.delenv("HARNESS_MCP_OAUTH_LAB", raising=False)
    assert mcp_oauth_lab_enabled() is False
    denied = gate_mcp_oauth("register")
    assert denied["allowed"] is False
    assert "default-disabled" in denied["error"]
    assert refuse_mcp_oauth()["allowed"] is False


def test_mcp_oauth_lab_opt_in(monkeypatch):
    monkeypatch.setenv("HARNESS_MCP_OAUTH_LAB", "1")
    assert mcp_oauth_lab_enabled() is True
    assert gate_mcp_oauth("register") == {"allowed": True, "action": "register"}
