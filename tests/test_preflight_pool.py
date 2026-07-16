"""Session.preflight accepts pooled OAuth tokens (no env required)."""

from __future__ import annotations

import os
import tempfile

import pytest

from harness import credential_pool as cp
from harness.config import HarnessConfig
from harness.session import Session


@pytest.fixture
def pool_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_CODEX_TOKEN", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cp.clear_pools_for_tests()
    yield tmp_path
    cp.clear_pools_for_tests()


def test_preflight_ok_with_codex_pool_only(pool_dir, monkeypatch):
    cp.add_oauth_entry(
        "openai-codex",
        access_token="eyJhbGciOiJub25lIn0.e30.sig",
        label="chatgpt-codex",
    )
    # Env may be mirrored by add_oauth_entry; clear it to prove preflight
    # accepts the pool itself.
    monkeypatch.delenv("OPENAI_CODEX_TOKEN", raising=False)
    assert not os.environ.get("OPENAI_CODEX_TOKEN")

    cfg = HarnessConfig(
        driver="openai-codex:gpt-5.5",
        state_dir=str(tempfile.mkdtemp()),
    )
    s = Session(cfg)
    assert s.preflight() is None
    # Preflight mirrors for follow-on env checks.
    assert os.environ.get("OPENAI_CODEX_TOKEN", "").startswith("eyJ")


def test_credential_satisfied_helper(pool_dir, monkeypatch):
    monkeypatch.delenv("OPENAI_CODEX_TOKEN", raising=False)
    assert cp.credential_satisfied("OPENAI_CODEX_TOKEN") is False
    cp.add_oauth_entry(
        "openai-codex",
        access_token="tok-abc",
        label="c",
    )
    monkeypatch.delenv("OPENAI_CODEX_TOKEN", raising=False)
    assert cp.credential_satisfied("OPENAI_CODEX_TOKEN") is True


def test_preflight_ok_with_anthropic_pool_only(pool_dir, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cp.add_oauth_entry(
        "anthropic",
        access_token="sk-ant-oat-preflight-test-token",
        label="claude-max",
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert not os.environ.get("ANTHROPIC_API_KEY")

    cfg = HarnessConfig(
        driver="anthropic:claude-sonnet-4-5",
        state_dir=str(tempfile.mkdtemp()),
    )
    s = Session(cfg)
    assert s.preflight() is None
    assert os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-ant-oat")


def test_preflight_ok_with_xai_oauth_pool_only(pool_dir, monkeypatch):
    """SuperGrok OAuth is stored as xai-oauth but the pilot uses XAI_API_KEY."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_OAUTH_TOKEN", raising=False)
    cp.add_oauth_entry(
        "xai-oauth",
        access_token="xai-oauth-preflight-token-xyz",
        label="supergrok",
    )
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_OAUTH_TOKEN", raising=False)
    # add_oauth_entry mirrors both; clear again to prove preflight peeks the pool.
    assert cp.credential_satisfied("XAI_API_KEY") is True

    cfg = HarnessConfig(
        driver="xai:grok-4",
        state_dir=str(tempfile.mkdtemp()),
    )
    s = Session(cfg)
    # Clear env after Session init may have loaded pools.
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    assert s.preflight() is None
    assert os.environ.get("XAI_API_KEY", "") == "xai-oauth-preflight-token-xyz"


def test_preflight_ok_with_nous_pool_only(pool_dir, monkeypatch):
    monkeypatch.delenv("NOUS_API_KEY", raising=False)
    cp.add_oauth_entry(
        "nous",
        access_token="nous-preflight-token-abc",
        label="nous",
    )
    monkeypatch.delenv("NOUS_API_KEY", raising=False)

    cfg = HarnessConfig(
        driver="nous:Hermes-4-70B",
        state_dir=str(tempfile.mkdtemp()),
    )
    s = Session(cfg)
    monkeypatch.delenv("NOUS_API_KEY", raising=False)
    assert s.preflight() is None
    assert os.environ.get("NOUS_API_KEY", "") == "nous-preflight-token-abc"
