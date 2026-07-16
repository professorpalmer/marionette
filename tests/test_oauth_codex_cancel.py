"""Codex device OAuth cancel clears pending sessions."""

from __future__ import annotations

from harness import oauth_codex as oc


def test_cancel_clears_pending(monkeypatch):
    oc.clear_pending_for_tests()

    def fake_http(method, url, **kwargs):
        return 200, {
            "user_code": "ABCD-EFGH",
            "device_auth_id": "dev-1",
            "interval": 5,
        }

    monkeypatch.setattr(oc, "_http_json", fake_http)
    start = oc.start_codex_device_login(label="t")
    sid = start["session_id"]
    assert sid in oc._pending
    res = oc.cancel_codex_device_login(sid)
    assert res["status"] == "cancelled"
    assert res["cleared"] is True
    assert sid not in oc._pending
    # Idempotent
    res2 = oc.cancel_codex_device_login(sid)
    assert res2["cleared"] is False
