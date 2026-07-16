"""xAI / Nous device OAuth cancel clears pending sessions (no live network)."""

from __future__ import annotations

from harness import oauth_nous as on
from harness import oauth_xai as ox


def test_xai_cancel_clears_pending(monkeypatch):
    ox.clear_pending_for_tests()

    def fake_http(url, form, *, timeout=20.0):
        return 200, {
            "device_code": "dev-xai",
            "user_code": "XAI-CODE",
            "verification_uri": "https://auth.x.ai/device",
            "verification_uri_complete": "https://auth.x.ai/device?user_code=XAI-CODE",
            "expires_in": 900,
            "interval": 5,
        }

    monkeypatch.setattr(ox, "_http_form", fake_http)
    monkeypatch.setattr(ox, "_token_endpoint", lambda: "https://auth.x.ai/oauth2/token")
    start = ox.start_xai_device_login(label="x")
    sid = start["session_id"]
    assert sid in ox._pending
    res = ox.cancel_xai_device_login(sid)
    assert res["status"] == "cancelled"
    assert res["cleared"] is True
    assert sid not in ox._pending
    assert ox.cancel_xai_device_login(sid)["cleared"] is False


def test_nous_cancel_clears_pending(monkeypatch):
    on.clear_pending_for_tests()

    def fake_http(url, form, *, timeout=20.0):
        return 200, {
            "device_code": "dev-nous",
            "user_code": "NOUS-CODE",
            "verification_uri": "https://portal.nousresearch.com/device",
            "expires_in": 900,
            "interval": 1,
        }

    monkeypatch.setattr(on, "_http_form", fake_http)
    start = on.start_nous_device_login(label="n")
    sid = start["session_id"]
    assert sid in on._pending
    res = on.cancel_nous_device_login(sid)
    assert res["status"] == "cancelled"
    assert res["cleared"] is True
    assert sid not in on._pending
    assert on.cancel_nous_device_login(sid)["cleared"] is False


def test_nous_poll_mirrors_env(tmp_path, monkeypatch):
    """Successful Nous device poll exports NOUS_API_KEY for classic pilots."""
    import os

    from harness import credential_pool as cp

    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("NOUS_API_KEY", raising=False)
    cp.clear_pools_for_tests()
    on.clear_pending_for_tests()

    def fake_http(url, form, *, timeout=20.0):
        if "device/code" in url:
            return 200, {
                "device_code": "dev-nous",
                "user_code": "NOUS-CODE",
                "verification_uri": "https://portal.nousresearch.com/device",
                "expires_in": 900,
                "interval": 1,
            }
        return 200, {
            "access_token": "nous-access-token-xyz",
            "refresh_token": "nous-refresh",
            "expires_in": 3600,
        }

    monkeypatch.setattr(on, "_http_form", fake_http)
    start = on.start_nous_device_login(label="n1")
    res = on.poll_nous_device_login(start["session_id"])
    assert res["status"] == "done"
    assert os.environ.get("NOUS_API_KEY") == "nous-access-token-xyz"


def test_xai_poll_mirrors_env(tmp_path, monkeypatch):
    """Successful xAI device poll exports XAI_API_KEY for Grok pilots."""
    import os

    from harness import credential_pool as cp

    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_OAUTH_TOKEN", raising=False)
    cp.clear_pools_for_tests()
    ox.clear_pending_for_tests()

    def fake_http(url, form, *, timeout=20.0):
        if "device/code" in url:
            return 200, {
                "device_code": "dev-xai",
                "user_code": "XAI-CODE",
                "verification_uri": "https://auth.x.ai/device",
                "verification_uri_complete": "https://auth.x.ai/device?user_code=XAI-CODE",
                "expires_in": 900,
                "interval": 5,
            }
        return 200, {
            "access_token": "xai-access-token-xyz",
            "refresh_token": "xai-refresh",
            "expires_in": 3600,
        }

    monkeypatch.setattr(ox, "_http_form", fake_http)
    monkeypatch.setattr(ox, "_token_endpoint", lambda: "https://auth.x.ai/oauth2/token")
    start = ox.start_xai_device_login(label="x1")
    res = ox.poll_xai_device_login(start["session_id"])
    assert res["status"] == "done"
    assert os.environ.get("XAI_API_KEY") == "xai-access-token-xyz"
    assert os.environ.get("XAI_OAUTH_TOKEN") == "xai-access-token-xyz"
