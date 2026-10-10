"""OAuth pool entries renew an expired access token with their refresh token."""

from __future__ import annotations

import time

import pytest

from harness import credential_pool as cp
from harness import keys as keys_mod


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    stored_keys = {}
    monkeypatch.setattr(keys_mod, "_read_keys", lambda: dict(stored_keys))
    monkeypatch.setattr(keys_mod, "_write_keys", lambda k: stored_keys.update(k))
    calls = []

    def fake_refresh(refresh_token):
        calls.append(refresh_token)
        return {"access_token": "sk-ant-oat01-new", "expires_in": 28800, "refresh_token": "rt-2"}

    monkeypatch.setattr("harness.oauth_anthropic.refresh_anthropic_tokens", fake_refresh)
    cp.clear_pools_for_tests()
    yield {"calls": calls, "keys": stored_keys}
    cp.clear_pools_for_tests()


def _expired_entry(**kw):
    return cp.add_oauth_entry(
        "anthropic", access_token="sk-ant-oat01-old", refresh_token="rt-1",
        label="claude-max", expires_at_ms=int(time.time() * 1000) - 1000, **kw,
    )


def test_401_renews_the_entry_and_its_mirrors(pool, monkeypatch):
    entry = _expired_entry()
    pool["keys"]["anthropic"] = "sk-ant-oat01-old"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-oat01-old")

    nxt = cp.report_failure("anthropic", entry.id, status_code=401,
                            message="OAuth access token has expired.")

    assert nxt == "sk-ant-oat01-new"
    assert pool["calls"] == ["rt-1"]
    assert entry.refresh_token == "rt-2"
    assert cp.has_healthy_credential("anthropic")
    assert pool["keys"]["anthropic"] == "sk-ant-oat01-new"
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-oat01-new"


def test_failed_refresh_still_marks_the_entry_exhausted(pool, monkeypatch):
    monkeypatch.setattr("harness.oauth_anthropic.refresh_anthropic_tokens", lambda rt: None)
    entry = _expired_entry()
    assert cp.report_failure("anthropic", entry.id, status_code=401, message="revoked") is None
    assert not cp.has_healthy_credential("anthropic")


def test_select_renews_a_token_that_expires_soon(pool):
    _expired_entry()
    assert cp.resolve_entry("anthropic").runtime_token == "sk-ant-oat01-new"
    assert pool["calls"] == ["rt-1"]


def test_a_token_another_process_renewed_is_adopted(pool):
    entry = _expired_entry()
    store = cp._load_store()
    for stored in store["pools"]["anthropic"]:
        stored["access_token"] = "sk-ant-oat01-other-process"
        stored["refresh_token"] = "rt-other"
        stored["expires_at_ms"] = int(time.time() * 1000) + 3_600_000
    import json
    with open(cp._pool_path(), "w", encoding="utf-8") as f:
        json.dump(store, f)

    assert cp.refresh_oauth_entry(entry)
    assert entry.access_token == "sk-ant-oat01-other-process"
    assert entry.refresh_token == "rt-other"
    assert pool["calls"] == []


def test_api_key_401_is_not_refreshed(pool):
    entry = cp.add_api_key("anthropic", "sk-ant-api03-xxxxxxxxxx", label="key")
    assert cp.report_failure("anthropic", entry.id, status_code=401, message="bad key") is None
    assert pool["calls"] == []


def test_refresh_request_shape(monkeypatch):
    import io
    import json
    from harness import oauth_anthropic as oa

    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data))
        return io.BytesIO(json.dumps({"access_token": "a2", "expires_in": 60}).encode())

    monkeypatch.setattr(oa.urllib.request, "urlopen", fake_urlopen)
    out = oa.refresh_anthropic_tokens("rt-1")
    assert out == {"access_token": "a2", "expires_in": 60, "refresh_token": None}
    assert sent == [{"grant_type": "refresh_token", "client_id": oa._OAUTH_CLIENT_ID, "refresh_token": "rt-1"}]
