"""OpenAI-compat driver resolves and rotates via credential pool."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from harness import credential_pool as cp
from pmharness.drivers.openai_compat import OpenAICompatDriver


@pytest.fixture
def pool_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cp.clear_pools_for_tests()
    yield tmp_path
    cp.clear_pools_for_tests()


class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code: int, body: str):
        super().__init__(
            "https://example.test/v1/chat/completions",
            code,
            "err",
            hdrs=None,
            fp=io.BytesIO(body.encode("utf-8")),
        )


def test_driver_key_from_pool(pool_dir, monkeypatch):
    cp.add_api_key("openrouter", "sk-pool-aaaaaaaaaa", label="a")
    d = OpenAICompatDriver(
        name="or",
        model="x",
        base_url="https://example.test/v1",
        api_key_env="OPENROUTER_API_KEY",
    )
    assert d._key() == "sk-pool-aaaaaaaaaa"
    assert d._pool_provider == "openrouter"
    assert d._pool_entry_id


def test_driver_key_from_xai_oauth_pool(pool_dir, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    cp.add_oauth_entry(
        "xai-oauth",
        access_token="xai-oauth-driver-token",
        label="sg",
    )
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    d = OpenAICompatDriver(
        name="xai",
        model="grok-4",
        base_url="https://api.x.ai/v1",
        api_key_env="XAI_API_KEY",
    )
    assert d._key() == "xai-oauth-driver-token"
    assert d._pool_provider == "xai-oauth"


def test_driver_rotate_on_plan_limit_429(pool_dir, monkeypatch):
    a = cp.add_api_key("openrouter", "sk-pool-aaaaaaaaaa", label="a")
    b = cp.add_api_key("openrouter", "sk-pool-bbbbbbbbbb", label="b")
    d = OpenAICompatDriver(
        name="or",
        model="x",
        base_url="https://example.test/v1",
        api_key_env="OPENROUTER_API_KEY",
    )
    assert d._key() == a.access_token

    calls = {"n": 0}
    ok_body = json.dumps({
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }).encode("utf-8")

    class _OkResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return ok_body

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        auth = req.get_header("Authorization") or ""
        if calls["n"] == 1:
            raise _FakeHTTPError(429, "usage limit reached for your plan")
        assert "sk-pool-bbbbbbbbbb" in auth
        return _OkResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    resp = d.complete("ping")
    assert resp.error is None
    assert resp.text == "hi"
    assert calls["n"] == 2
    assert d._pool_entry_id == b.id
