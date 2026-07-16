"""Anthropic OAuth helpers (no live network)."""

from __future__ import annotations

from harness.oauth_anthropic import (
    cancel_anthropic_pkce_login,
    clear_pending_for_tests,
    is_anthropic_oauth_token,
    start_anthropic_pkce_login,
)


def test_is_oauth_token_shapes():
    assert is_anthropic_oauth_token("sk-ant-oat-abc123")
    assert is_anthropic_oauth_token("eyJhbGciOiJIUzI1NiJ9.e30.sig")
    assert is_anthropic_oauth_token("cc-session-token")
    assert not is_anthropic_oauth_token("sk-ant-api03-regular-key")
    assert not is_anthropic_oauth_token("")


def test_start_pkce_returns_auth_url(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    clear_pending_for_tests()
    res = start_anthropic_pkce_login(label="max-1")
    assert res["provider"] == "anthropic"
    assert res["session_id"]
    assert "claude.ai/oauth/authorize" in res["auth_url"]
    assert "code_challenge" in res["auth_url"]
    assert res["flow"] == "pkce_paste"


def test_cancel_clears_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    clear_pending_for_tests()
    start = start_anthropic_pkce_login(label="max-cancel")
    sid = start["session_id"]
    from harness import oauth_anthropic as oa
    assert sid in oa._pending
    res = cancel_anthropic_pkce_login(sid)
    assert res["status"] == "cancelled"
    assert res["cleared"] is True
    assert sid not in oa._pending
    res2 = cancel_anthropic_pkce_login(sid)
    assert res2["cleared"] is False
