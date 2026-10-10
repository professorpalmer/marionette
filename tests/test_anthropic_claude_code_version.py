"""OAuth requests send a Claude Code version the API accepts."""

from __future__ import annotations

import pmharness.drivers.anthropic as anth

TOO_OLD = ('{"type":"error","error":{"type":"invalid_request_error","message":"Claude Code 2.1.200 '
           'does not support this model; version 2.1.280 or newer is required. Run \'claude update\', '
           'or update the Claude desktop app, then try again.","details":{"error_code":'
           '"claude_code_version_too_old"}}}')


class _Run:
    def __init__(self, out):
        self.stdout = out


def test_installed_cli_version_wins_over_the_floor(monkeypatch):
    monkeypatch.setattr(anth, "_claude_code_version", None)
    monkeypatch.setattr("harness.claude_cli_auth.resolve_claude_binary", lambda: "/bin/claude")
    monkeypatch.setattr(anth.subprocess, "run", lambda *a, **k: _Run("2.1.400 (Claude Code)\n"))
    assert anth.claude_code_version() == "2.1.400"


def test_missing_or_older_cli_uses_the_floor(monkeypatch):
    monkeypatch.setattr(anth, "_claude_code_version", None)
    monkeypatch.setattr("harness.claude_cli_auth.resolve_claude_binary", lambda: None)
    assert anth.claude_code_version() == anth._CLAUDE_CODE_VERSION_FLOOR
    monkeypatch.setattr(anth, "_claude_code_version", None)
    monkeypatch.setattr("harness.claude_cli_auth.resolve_claude_binary", lambda: "/bin/claude")
    monkeypatch.setattr(anth.subprocess, "run", lambda *a, **k: _Run("2.1.100 (Claude Code)\n"))
    assert anth.claude_code_version() == anth._CLAUDE_CODE_VERSION_FLOOR


def test_too_old_error_is_retried_once_with_the_required_version(monkeypatch):
    monkeypatch.setattr(anth, "_claude_code_version", "2.1.200")
    driver = anth.AnthropicDriver("anthropic", "claude-opus-5-5")
    assert driver._retry_after_http_error(400, TOO_OLD) is True
    assert anth.claude_code_version() == "2.1.280"
    # The same error again does not loop: the version is not newer now.
    assert driver._retry_after_http_error(400, TOO_OLD) is False
    assert driver._retry_after_http_error(400, "some other bad request") is False


def test_oauth_headers_carry_the_version(monkeypatch):
    monkeypatch.setattr(anth, "_claude_code_version", "2.1.283")
    driver = anth.AnthropicDriver("anthropic", "claude-opus-5-5")
    monkeypatch.setattr(type(driver), "_key", lambda self: "sk-ant-oat01-test")
    assert driver._headers()["User-Agent"] == "claude-code/2.1.283"


def test_oauth_system_starts_with_the_claude_code_identity(monkeypatch):
    # Without this first block, the API answers a Claude Max token with 429.
    driver = anth.AnthropicDriver("anthropic", "claude-opus-5-5")
    monkeypatch.setattr(type(driver), "_prompt_cache_on", lambda self: False)
    monkeypatch.setattr(type(driver), "_uses_oauth", lambda self: True)
    identity = {"type": "text", "text": anth._CLAUDE_CODE_IDENTITY}
    assert driver._system_field("Be brief.") == [identity, {"type": "text", "text": "Be brief."}]
    assert driver._system_field(None) == [identity]


def test_api_key_system_is_unchanged(monkeypatch):
    driver = anth.AnthropicDriver("anthropic", "claude-opus-5-5")
    monkeypatch.setattr(type(driver), "_prompt_cache_on", lambda self: False)
    monkeypatch.setattr(type(driver), "_uses_oauth", lambda self: False)
    assert driver._system_field("Be brief.") == "Be brief."
    assert driver._system_field(None) is None
