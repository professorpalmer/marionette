"""A used-up quota is not a rate limit: no retry, and the whole key rests."""

from __future__ import annotations

import tempfile

import pytest

from harness import credential_pool as cp
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from pmharness.drivers.base import DriverResponse
from pmharness.drivers.error_classifier import classify
from pmharness.drivers.retry import with_retry

OPENAI_QUOTA = ('{"error":{"message":"You exceeded your current quota, please check your '
                'plan and billing details.","type":"insufficient_quota"}}')
OPENAI_RPM = '{"error":{"code":"rate_limit_exceeded","message":"Rate limit reached for gpt-x"}}'
ANTHROPIC_CREDIT = "Your credit balance is too low to access the Anthropic API."


@pytest.mark.parametrize("status,body,want", [
    (429, OPENAI_QUOTA, "quota"),
    (429, ANTHROPIC_CREDIT, "quota"),
    (402, "Insufficient credits", "quota"),
    (429, "ChatGPT usage limit reached", "quota"),
    (429, OPENAI_RPM, "rate_limit"),
    (429, "Too Many Requests", "rate_limit"),
    (402, "payment required", "fatal"),
])
def test_classify_separates_quota_from_rate_limit(status, body, want):
    assert classify(status, body).value == want


def test_with_retry_does_not_retry_a_used_up_quota():
    calls, sleeps = [], []

    def fn():
        calls.append(1)
        return DriverResponse(text="", error=f"HTTP 429: {OPENAI_QUOTA} retry-after: 3600")

    resp = with_retry(fn, max_attempts=4, sleep=sleeps.append)
    assert calls == [1]
    assert sleeps == []
    assert resp.meta["error_class"] == "quota"
    assert resp.meta["retry_after_seconds"] == 3600.0


@pytest.fixture
def pool_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cp.clear_pools_for_tests()
    yield tmp_path
    cp.clear_pools_for_tests()


def _two_keys():
    a = cp.add_api_key("openrouter", "sk-aaa-1111111111", label="a")
    b = cp.add_api_key("openrouter", "sk-bbb-2222222222", label="b")
    return a, b


def _for(model):
    return cp.resolve_entry("openrouter", model).access_token


def test_plain_rate_limit_cools_only_the_model(pool_dir):
    a, b = _two_keys()
    cp.report_failure("openrouter", a.id, status_code=429, message=OPENAI_RPM, model="kimi-k3")
    assert _for("kimi-k3") == b.access_token
    assert _for("deepseek-v4-pro") == a.access_token


@pytest.mark.parametrize("body", [OPENAI_QUOTA, ANTHROPIC_CREDIT])
def test_used_up_quota_benches_the_whole_key(pool_dir, body):
    a, b = _two_keys()
    cp.report_failure("openrouter", a.id, status_code=429, message=body, model="kimi-k3")
    assert _for("deepseek-v4-pro") == b.access_token


def test_quota_error_text_names_credit_not_rate_limit():
    s = ConversationalSession(HarnessConfig(state_dir=tempfile.mkdtemp()))
    out = s._humanize_pilot_error(f"HTTP 429: {ANTHROPIC_CREDIT}")
    assert "credit/quota" in out
    assert "rate-limiting" not in out
