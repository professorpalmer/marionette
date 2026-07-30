"""Per-server MCP last_invocation receipts (separate from lifecycle health)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harness.mcp_client import McpError
from harness.mcp_manager import McpManager

FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


def test_call_records_success_and_failure_separately_from_health(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    m.save_server("fake", {"command": sys.executable, "args": [FAKE]})
    try:
        m.start_server("fake")
        st = m.status()[0]
        assert st["running"] is True
        assert "last_invocation" not in st

        out = m.call("fake.echo", {"text": "hi"})
        assert out["content"][0]["text"] == "hi"
        st = m.status()[0]
        inv = st["last_invocation"]
        assert inv["tool"] == "echo"
        assert inv["ok"] is True
        assert inv["error"] == ""
        assert inv["at"]

        # Lifecycle health stays healthy even after a failed call.
        with pytest.raises(McpError):
            m.call("fake.nope", {})
        st = m.status()[0]
        assert st["running"] is True
        assert st["error"] == ""
        inv = st["last_invocation"]
        assert inv["tool"] == "nope"
        assert inv["ok"] is False
        assert inv["error"]
        assert "arguments" not in inv
        assert "result" not in inv
    finally:
        m.stop_all()


def test_invocation_persists_best_effort_under_state_dir(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    m.save_server("fake", {"command": sys.executable, "args": [FAKE]})
    try:
        m.start_server("fake")
        m.call("fake.add", {"a": 1, "b": 2})
    finally:
        m.stop_all()

    path = state / "mcp_invocations.json"
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    row = data["servers"]["fake"]
    assert row == {
        "tool": "add",
        "ok": True,
        "error": "",
        "at": row["at"],
    }
    raw = path.read_text(encoding="utf-8")
    assert "arguments" not in raw
    assert "\"a\"" not in raw

    m2 = McpManager(config_path=str(cfgp))
    st = next(s for s in m2.status() if s["name"] == "fake")
    assert st["last_invocation"]["tool"] == "add"
    assert st["last_invocation"]["ok"] is True


def test_get_mcp_exposes_last_invocation(tmp_path, monkeypatch):
    from harness.api.mcp import McpServices, get_mcp

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    m.save_server("fake", {"command": sys.executable, "args": [FAKE]})
    try:
        m.start_server("fake")
        m.call("fake.echo", {"text": "x"})
        code, body = get_mcp(McpServices(mcp=m))
        assert code == 200
        server = next(s for s in body["servers"] if s["name"] == "fake")
        assert server["running"] is True
        assert server["last_invocation"]["tool"] == "echo"
        assert server["last_invocation"]["ok"] is True
    finally:
        m.stop_all()


def test_post_mcp_call_ok_false_still_records_failure(tmp_path, monkeypatch):
    from harness.api.mcp import McpServices, post_mcp_call

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    m.save_server("fake", {"command": sys.executable, "args": [FAKE]})
    svc = McpServices(mcp=m)
    try:
        m.start_server("fake")
        code, body = post_mcp_call(
            {"tool": "fake.missing", "arguments": {}}, svc,
        )
        assert code == 200
        assert body["ok"] is False
        assert "error" in body
        st = m.status()[0]
        assert st["last_invocation"]["ok"] is False
        assert st["last_invocation"]["tool"] == "missing"
    finally:
        m.stop_all()


_SECRET_SAMPLES = (
    "Bearer sk-or-v1-deadbeefcafe0123456789",
    "Basic dXNlcjpwYXNzd29yZA==",
    "ghp_abcdefghijklmnopqrstuv",
    "github_pat_11AAAAAAAAabcdefghijklmnopqrstuvwxyz",
    "api_key=super-secret-value-99",
    "token: leaked-token-value",
)


def test_invocation_error_redacts_secrets_on_disk_and_api(tmp_path, monkeypatch):
    """Receipt errors are secret-redacted before memory, disk, and API surfaces."""
    from harness.api.mcp import McpServices, get_mcp, post_mcp_call

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    m.save_server("fake", {"command": sys.executable, "args": [FAKE]})
    secret_blob = "auth failed: " + " | ".join(_SECRET_SAMPLES)

    try:
        m.start_server("fake")
        client = m._clients["fake"]
        monkeypatch.setattr(
            client,
            "call_tool",
            lambda *_a, **_k: (_ for _ in ()).throw(McpError(secret_blob)),
        )
        with pytest.raises(McpError):
            m.call("fake.echo", {"text": "x"})

        st = m.status()[0]
        inv = st["last_invocation"]
        assert inv["ok"] is False
        assert inv["tool"] == "echo"
        assert "REDACTED" in inv["error"]
        for needle in (
            "sk-or-v1-deadbeefcafe0123456789",
            "ghp_abcdefghijklmnopqrstuv",
            "github_pat_11AAAAAAAAabcdefghijklmnopqrstuvwxyz",
            "super-secret-value-99",
            "leaked-token-value",
            "dXNlcjpwYXNzd29yZA==",
        ):
            assert needle not in inv["error"]

        raw_disk = (state / "mcp_invocations.json").read_text(encoding="utf-8")
        for needle in (
            "sk-or-v1-deadbeefcafe0123456789",
            "ghp_abcdefghijklmnopqrstuv",
            "github_pat_11AAAAAAAAabcdefghijklmnopqrstuvwxyz",
            "super-secret-value-99",
            "leaked-token-value",
            "dXNlcjpwYXNzd29yZA==",
            "Bearer sk-",
        ):
            assert needle not in raw_disk
        assert "REDACTED" in raw_disk

        code, body = get_mcp(McpServices(mcp=m))
        assert code == 200
        api_inv = next(s for s in body["servers"] if s["name"] == "fake")["last_invocation"]
        assert "sk-or-v1-deadbeefcafe0123456789" not in api_inv["error"]
        assert "super-secret-value-99" not in api_inv["error"]

        code, call_body = post_mcp_call(
            {"tool": "fake.echo", "arguments": {"text": "y"}},
            McpServices(mcp=m),
        )
        assert code == 200
        assert call_body["ok"] is False
        assert "sk-or-v1-deadbeefcafe0123456789" not in call_body["error"]
        assert "super-secret-value-99" not in call_body["error"]
        assert "REDACTED" in call_body["error"]
    finally:
        m.stop_all()


_SECRET_NEEDLES = (
    "sk-or-v1-deadbeefcafe0123456789",
    "ghp_abcdefghijklmnopqrstuv",
    "github_pat_11AAAAAAAAabcdefghijklmnopqrstuvwxyz",
    "super-secret-value-99",
    "leaked-token-value",
    "dXNlcjpwYXNzd29yZA==",
)


def _assert_no_secret_leak(text: str) -> None:
    assert "REDACTED" in text
    for needle in _SECRET_NEEDLES:
        assert needle not in text


def test_lifecycle_handler_errors_redact_secrets(tmp_path, monkeypatch):
    """post_mcp_start/add/refresh {ok:false}.error never returns raw secrets."""
    from harness.api.mcp import (
        McpServices,
        post_mcp_add,
        post_mcp_refresh,
        post_mcp_start,
    )

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    secret_blob = "handshake failed: " + " | ".join(_SECRET_SAMPLES)
    svc = McpServices(mcp=m)

    def boom(*_a, **_k):
        raise McpError(secret_blob)

    monkeypatch.setattr(m, "start_server", boom)
    monkeypatch.setattr(m, "refresh_server", boom)

    m.save_server("leaky", {"command": "false"})
    for handler, body in (
        (post_mcp_start, {"name": "leaky"}),
        (post_mcp_refresh, {"name": "leaky"}),
        (post_mcp_add, {"name": "leaky-add", "command": "false"}),
    ):
        code, resp = handler(body, svc)
        assert code == 200
        assert resp["ok"] is False
        _assert_no_secret_leak(resp["error"])
        _assert_no_secret_leak(json.dumps(resp))


def test_lifecycle_manager_errors_redacted_before_status(tmp_path, monkeypatch):
    """McpManager stores redacted lifecycle errors; status/manage never leak secrets."""
    from harness import mcp_manager as mm
    from harness.api.mcp import McpServices, get_mcp

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    cfgp = tmp_path / "mcp.json"
    m = McpManager(config_path=str(cfgp))
    secret_blob = "handshake failed: " + " | ".join(_SECRET_SAMPLES)
    svc = McpServices(mcp=m)

    class BoomClient:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise McpError(secret_blob)

        def stop(self):
            return None

        def list_tools(self):
            return []

        alive = False

    monkeypatch.setattr(mm, "StdioMcpClient", BoomClient)
    m.save_server("broken", {"command": "false"})
    with pytest.raises(McpError):
        m.start_server("broken")

    st = next(s for s in m.status() if s["name"] == "broken")
    assert st["running"] is False
    _assert_no_secret_leak(st["error"])
    _assert_no_secret_leak(m._errors["broken"])

    code, listing = get_mcp(svc)
    assert code == 200
    api_err = next(s for s in listing["servers"] if s["name"] == "broken")["error"]
    _assert_no_secret_leak(api_err)

    managed = m.manage("start", name="broken")
    assert managed["ok"] is False
    _assert_no_secret_leak(managed["error"])

    managed_refresh = m.manage("refresh", name="broken")
    assert managed_refresh["ok"] is False
    _assert_no_secret_leak(managed_refresh["error"])
