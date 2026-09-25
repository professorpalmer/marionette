"""Center pilot uses Claude Code CLI login, not ANTHROPIC_API_KEY."""

from __future__ import annotations

import json

from harness import providers as prov
from harness.claude_cli_auth import login_token_if_ready, read_oauth_account, reset_for_tests
from harness.keys import mark_disconnected, unmark_disconnected
from pmharness.drivers.claude_cli import (
    ClaudeCliDriver,
    consume_stream_json,
    subscription_child_env,
)


def _write_oauth(home, *, email="user@example.com", uuid="acct-1"):
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(
        json.dumps({
            "oauthAccount": {
                "emailAddress": email,
                "accountUuid": uuid,
                "seatTier": "max",
            },
        }),
        encoding="utf-8",
    )


def test_oauth_account_not_file_presence(tmp_path):
    home = tmp_path / "empty"
    home.mkdir()
    (home / ".claude.json").write_text("{\"theme\":\"dark\"}", encoding="utf-8")
    assert read_oauth_account(home) is None
    _write_oauth(home)
    oauth = read_oauth_account(home)
    assert oauth is not None
    assert oauth["emailAddress"] == "user@example.com"


def test_subscription_child_env_drops_api_key():
    env = subscription_child_env({
        "ANTHROPIC_API_KEY": "sk-ant-payg",
        "ANTHROPIC_AUTH_TOKEN": "token",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "PATH": "/usr/bin",
        "HOME": "/tmp",
    })
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/tmp"


def test_api_key_alone_does_not_enable_claude_code(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_HOME", str(tmp_path / "none"))
    monkeypatch.delenv("CLAUDE_CODE_LOGIN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    reset_for_tests()
    assert login_token_if_ready() is None
    assert prov.get_provider("claude-code").available is False
    assert prov.get_provider("anthropic").available is True


def test_oauth_enables_claude_code_pilot(monkeypatch, tmp_path):
    home = tmp_path / "claude"
    _write_oauth(home)
    monkeypatch.setenv("CLAUDE_CONFIG_HOME", str(home))
    monkeypatch.delenv("CLAUDE_CODE_LOGIN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    reset_for_tests()
    provider = prov.get_provider("claude-code")
    assert provider.available
    assert provider.key() == "1"
    assert provider.key_env() == "CLAUDE_CODE_LOGIN"
    driver = prov.build_pilot("claude-code:claude-opus-4-8")
    assert isinstance(driver, ClaudeCliDriver)
    assert driver.model == "claude-opus-4-8"
    driver.claude_binary = "/usr/bin/claude"
    cmd = driver._build_cmd()
    assert cmd[0] == "/usr/bin/claude"
    assert cmd[1:6] == ["--print", "--output-format", "stream-json", "--verbose", "--permission-mode"]
    assert "sk-ant-xxx" not in cmd


def test_disconnect_hides_claude_code(monkeypatch, tmp_path):
    home = tmp_path / "claude"
    _write_oauth(home)
    monkeypatch.setenv("CLAUDE_CONFIG_HOME", str(home))
    reset_for_tests()
    mark_disconnected("claude-code")
    try:
        assert prov.get_provider("claude-code").available is False
    finally:
        unmark_disconnected("claude-code")


def test_consume_stream_json_result_and_deltas():
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-opus-4-8"}),
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "pong"}]},
        }),
        json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "pong",
            "session_id": "s1",
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }),
    ]
    deltas = []
    parsed = consume_stream_json(lines, on_delta=deltas.append)
    assert parsed["text"] == "pong"
    assert parsed["session_id"] == "s1"
    assert parsed["tokens_in"] == 3
    assert parsed["tokens_out"] == 1
    assert parsed["error"] is None
    assert deltas == ["pong"]


def test_plan_billing_includes_claude_code():
    from harness.conversation import _driver_is_plan_billing
    assert _driver_is_plan_billing("claude-code:claude-opus-4-8")
    assert not _driver_is_plan_billing("anthropic:claude-opus-4-8")


def test_model_fetch_returns_curated_claude_cli():
    from harness.model_fetch import _fetch_provider_models
    from harness.providers import get_provider
    models = _fetch_provider_models(get_provider("claude-code"), key="")
    assert "claude-opus-4-8" in models


def test_status_api_reports_unauthenticated(monkeypatch, tmp_path):
    from harness.api.providers import post_auth_claude_cli_status
    monkeypatch.setenv("CLAUDE_CONFIG_HOME", str(tmp_path / "none"))
    reset_for_tests()
    code, body = post_auth_claude_cli_status({"refresh": True})
    assert code == 200
    assert body["authenticated"] is False
    assert body["auth_kind"] == "claude_account"


def test_chat_spawn_strips_api_key(monkeypatch):
    captured = {}

    class _Proc:
        returncode = 0

        def communicate(self, _prompt, timeout=None):
            result = json.dumps({
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "ok",
                "session_id": "s1",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            })
            return result + "\n", ""

        def kill(self):
            return None

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env") or {}
        return _Proc()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-not-leak")
    monkeypatch.setattr("pmharness.drivers.claude_cli.subprocess.Popen", fake_popen)
    driver = ClaudeCliDriver(
        name="claude-code:claude-haiku-4-5",
        model="claude-haiku-4-5",
        claude_binary="/usr/bin/claude",
    )
    resp = driver.chat([{"role": "user", "content": "ping"}])
    assert resp.error is None
    assert resp.text == "ok"
    assert resp.meta["billing"] == "plan"
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert captured["cmd"][0] == "/usr/bin/claude"
    assert "--print" in captured["cmd"]
