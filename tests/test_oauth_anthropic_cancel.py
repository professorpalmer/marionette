"""Anthropic PKCE cancel clears pending sessions."""

from __future__ import annotations

from harness import oauth_anthropic as oa


def test_cancel_clears_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    oa.clear_pending_for_tests()
    start = oa.start_anthropic_pkce_login(label="t")
    sid = start["session_id"]
    assert sid in oa._pending
    res = oa.cancel_anthropic_pkce_login(sid)
    assert res["status"] == "cancelled"
    assert res["cleared"] is True
    assert sid not in oa._pending
    assert oa.cancel_anthropic_pkce_login(sid)["cleared"] is False
