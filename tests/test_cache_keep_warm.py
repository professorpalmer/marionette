from __future__ import annotations

import io
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from harness.cache_keep_warm import CacheKeepWarm, KeepWarmLimits
from pmharness.drivers.cache_refresh import (
    ANTHROPIC_MODELS,
    CacheRefreshDriver,
    RefreshCancelled,
)
from pmharness.drivers.reasoning_envelope import (
    capture_reasoning,
    replay_reasoning,
    retain_reasoning,
)


class DummyDriver(CacheRefreshDriver):
    def __init__(
        self,
        model="gpt-5.6",
        base_url="https://api.openai.com/v1",
        chatgpt_backend=False,
    ):
        self.name = "dummy"
        self.model = model
        self.base_url = base_url
        self.chatgpt_backend = chatgpt_backend
        self.init_cache_refresh()


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Runner:
    def __init__(self, pilot, clock):
        self.pilot = pilot
        self.clock = clock
        self._input_admissions = 0
        self._replacement_retired = False
        self._busy = False

    def is_turn_busy(self):
        return self._busy


def _openai_body():
    return {"model": "gpt-5.6", "input": "hi", "max_output_tokens": 16, "stream": True}


def _anthropic_body(ttl="5m", thinking=None):
    body = {
        "model": "claude-sonnet-4-5",
        "messages": [{"role": "user", "content": "hi"}],
        "system": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral", "ttl": ttl}}],
    }
    if thinking is not None:
        body["thinking"] = thinking
    return body


def test_keep_warm_limits_are_bounded():
    with pytest.raises(ValueError):
        KeepWarmLimits(max_refreshes=0)
    with pytest.raises(ValueError):
        KeepWarmLimits(idle_seconds=30)
    with pytest.raises(ValueError):
        KeepWarmLimits(max_spend_usd=0)


def test_unknown_model_and_chatgpt_oauth_are_unsupported():
    unknown = DummyDriver(model="mystery-model")
    unknown.remember_cache_request("responses", _openai_body(), {"Authorization": "Bearer x"}, 1.0, {})
    assert unknown.cache_snapshot().reason

    oauth = DummyDriver(chatgpt_backend=True)
    oauth.remember_cache_request("responses", _openai_body(), {}, 1.0, {})
    assert "ChatGPT" in oauth.cache_snapshot().reason

    uncapped = DummyDriver()
    body = _openai_body()
    del body["max_output_tokens"]
    uncapped.remember_cache_request("responses", body, {}, 1.0, {})
    assert "output cap" in uncapped.cache_snapshot().reason


def test_anthropic_thinking_and_unknown_ttl_are_refused():
    driver = DummyDriver(model="claude-sonnet-4-5", base_url="https://api.anthropic.com/v1")
    driver.remember_cache_request(
        "anthropic", _anthropic_body(thinking={"type": "enabled"}), {}, 1.0, {},
    )
    assert "Thinking" in driver.cache_snapshot().reason

    other = DummyDriver(model="claude-sonnet-4-5", base_url="https://api.anthropic.com/v1")
    body = _anthropic_body()
    body["system"][0]["cache_control"] = {"type": "ephemeral", "ttl": "2h"}
    other.remember_cache_request("anthropic", body, {}, 1.0, {})
    assert "lifetime" in other.cache_snapshot().reason
    assert "claude-sonnet-4-5" in ANTHROPIC_MODELS


def test_refresh_replays_the_accepted_snapshot_with_one_output_token():
    driver = DummyDriver()
    driver.remember_cache_request(
        "responses",
        _openai_body(),
        {"Authorization": "Bearer x"},
        10.0,
        {"input_tokens_details": {"cached_tokens": 12}},
    )
    snapshot = driver.cache_snapshot()
    captured = {}

    class FakeResp:
        status = 200

        def read(self, _n):
            return json.dumps({"usage": {"input_tokens_details": {"cached_tokens": 20}}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        fp = SimpleNamespace(raw=SimpleNamespace(_sock=None))

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode())
        return FakeResp()

    from pmharness.drivers.cache_refresh import RefreshAttempt
    with mock.patch("pmharness.drivers.cache_refresh.urllib.request.urlopen", fake_urlopen):
        result = driver.refresh_cache(snapshot, RefreshAttempt())
    assert captured["url"].endswith("/responses")
    assert captured["body"]["max_output_tokens"] == 1
    assert captured["body"]["stream"] is False
    assert captured["body"]["input"] == "hi"
    assert result.cached_tokens == 20


def test_keep_warm_tick_respects_fake_clock_and_idle_limit():
    clock = Clock(1000.0)
    driver = DummyDriver()
    driver.remember_cache_request(
        "responses",
        _openai_body(),
        {},
        clock.now,
        {"input_tokens_details": {"cached_tokens": 4}},
    )
    controller = CacheKeepWarm(Runner(driver, clock), clock=clock, schedule=False)
    controller.start(KeepWarmLimits(max_refreshes=2, idle_seconds=60, max_spend_usd=5.0))
    assert controller.status()["state"] in {"warm", "cold"}
    clock.now += 61
    controller.tick()
    assert controller.enabled is False
    assert "Idle" in controller.reason


def test_keep_warm_cancels_refresh_when_a_turn_starts():
    clock = Clock(1000.0)
    driver = DummyDriver()
    driver.remember_cache_request(
        "responses",
        _openai_body(),
        {},
        clock.now,
        {"input_tokens_details": {"cached_tokens": 4}},
    )
    controller = CacheKeepWarm(Runner(driver, clock), clock=clock, schedule=False)
    controller.start()
    attempt = type("A", (), {"cancelled": type("E", (), {"is_set": lambda self: False})(), "cancel": mock.Mock()})()
    controller.attempt = attempt
    controller.foreground_start()
    attempt.cancel.assert_called_once()


def test_reasoning_envelope_is_identity_bound_and_not_spliced():
    envelope = capture_reasoning(
        "anthropic",
        "https://api.anthropic.com/v1",
        "claude-sonnet-4-5",
        [{"type": "thinking", "thinking": "secret"}, {"type": "text", "text": "hi"}],
    )
    message = {"role": "assistant", "content": "hi"}
    retain_reasoning(message, envelope)
    assert replay_reasoning(
        message, "anthropic", "https://api.anthropic.com/v1", "claude-sonnet-4-5",
    )[0]["type"] == "thinking"
    assert replay_reasoning(
        message, "responses", "https://api.openai.com/v1", "gpt-5.6",
    ) is None
    message["content"] = "edited"
    assert replay_reasoning(
        message, "anthropic", "https://api.anthropic.com/v1", "claude-sonnet-4-5",
    ) is None
    assert capture_reasoning("anthropic", "https://api.anthropic.com/v1", "m", [{"type": "text"}]) is None
