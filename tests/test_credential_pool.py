"""Credential pool: select, rotate, plan-limit immediate rotate."""

from __future__ import annotations

import json
import os

import pytest

from harness import credential_pool as cp


@pytest.fixture
def pool_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    cp.clear_pools_for_tests()
    yield tmp_path
    cp.clear_pools_for_tests()


def test_add_and_select_fill_first(pool_dir):
    a = cp.add_api_key("openrouter", "sk-aaa-1111111111", label="a")
    b = cp.add_api_key("openrouter", "sk-bbb-2222222222", label="b")
    tok = cp.resolve_token("openrouter")
    assert tok == a.access_token
    # fill_first keeps using first until exhausted
    assert cp.resolve_token("openrouter") == a.access_token
    assert a.id != b.id


def test_rotate_on_plan_limit(pool_dir):
    a = cp.add_api_key("cursor", "key-aaaa-11111111", label="cursor-1")
    b = cp.add_api_key("cursor", "key-bbbb-22222222", label="cursor-2")
    assert cp.resolve_token("cursor") == a.access_token
    nxt = cp.report_failure(
        "cursor",
        a.id,
        status_code=429,
        message="usage limit reached for your plan",
    )
    assert nxt == b.access_token


def test_persist_roundtrip(pool_dir):
    cp.add_api_key("openai", "sk-persist-abcdefgh", label="p")
    path = os.path.join(str(pool_dir), "auth_pool.json")
    assert os.path.isfile(path)
    cp.clear_pools_for_tests()
    tok = cp.resolve_token("openai")
    assert tok == "sk-persist-abcdefgh"
    data = json.loads(open(path, encoding="utf-8").read())
    assert "openai" in data["pools"]


def test_public_list_masks_secret(pool_dir):
    cp.add_api_key("openrouter", "sk-or-v1-secrettoken99", label="or1")
    pub = cp.list_pool_public("openrouter")
    assert pub["entries"]
    assert "secrettoken99" not in json.dumps(pub)
    assert pub["entries"][0]["masked"]


def test_is_plan_limit_message():
    assert cp.is_plan_limit_message("ChatGPT usage limit reached")
    assert not cp.is_plan_limit_message("connection reset")


def test_xai_oauth_mirrors_xai_api_key_env(pool_dir, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_OAUTH_TOKEN", raising=False)
    cp.add_oauth_entry(
        "xai-oauth",
        access_token="xai-oauth-mirror-token-99",
        label="sg",
    )
    assert os.environ.get("XAI_OAUTH_TOKEN") == "xai-oauth-mirror-token-99"
    assert os.environ.get("XAI_API_KEY") == "xai-oauth-mirror-token-99"
    assert cp.credential_satisfied("XAI_API_KEY") is True
    assert cp.peek_token_for_env("XAI_API_KEY") == "xai-oauth-mirror-token-99"
