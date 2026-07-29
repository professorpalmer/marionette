"""Direct Codex cache token stamping and pilot meter integration."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from harness.api.cost_accounting import _cache_savings
from harness.send_loop_phases import meter_pilot_step
from pmharness.drivers.codex_responses import CodexResponsesDriver


def test_response_from_raw_stamps_cache_read_tokens():
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5")
    raw = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "ok"}],
            }
        ],
        "usage": {
            "input_tokens": 10_000,
            "output_tokens": 200,
            "input_tokens_details": {"cached_tokens": 8_000},
        },
    }
    resp = driver._response_from_raw(raw, t0=time.time())
    assert resp.meta["cache_read_tokens"] == 8_000
    assert resp.meta["raw_usage"]["input_tokens_details"]["cached_tokens"] == 8_000
    assert resp.tokens_in == 10_000
    assert resp.tokens_out == 200


def test_meter_pilot_step_codex_cached_tokens_and_savings(monkeypatch):
    """Real ChatGPT Codex usage shape must increment _tokens_cached without double count."""
    meters = {}
    session = SimpleNamespace(
        _tokens_used=0,
        _tokens_out=0,
        _turn_output_tokens=0,
        _tokens_in=0,
        _last_prompt_tokens=0,
        _tokens_cached=0,
        _tokens_cache_write=0,
        _tokens_cache_write_5m=0,
        _tokens_cache_write_1h=0,
        _last_turn_cache_read_tokens=0,
        _last_prompt_cache_activity_at=0.0,
        _plan_billing=False,
        _price_source="",
        _provider_cost_usd=0.0,
        _provider_billed_tokens_in=0,
        _provider_billed_tokens_out=0,
        _provider_billed_tokens_cached=0,
        _provider_billed_tokens_cache_write=0,
        _provider_billed_tokens_cache_write_5m=0,
        _provider_billed_tokens_cache_write_1h=0,
        config=SimpleNamespace(driver="openai-codex/test"),
        _accumulate_session_meters=lambda **kw: meters.update(kw),
    )
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5")
    raw = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "ok"}],
            }
        ],
        "usage": {
            "input_tokens": 10_000,
            "output_tokens": 200,
            "input_tokens_details": {"cached_tokens": 8_000},
        },
    }
    resp = driver._response_from_raw(raw, t0=time.time())
    monkeypatch.setattr(
        "pmharness.registry.resolve_price_with_source",
        lambda _name: (3.0, 15.0, "catalog"),
        raising=False,
    )
    from harness.api.cost_accounting import _session_cost

    monkeypatch.setattr("harness.server._session_cost", _session_cost, raising=False)
    meter_pilot_step(session, resp, prompt="hello")

    assert session._tokens_cached == 8_000
    assert session._last_turn_cache_read_tokens == 8_000
    assert session._tokens_in == 10_000
    assert session._tokens_used == 10_000 + 200
    assert session._plan_billing is True
    assert resp.meta.get("provider_cost_usd") is None
    savings = _cache_savings(8_000, 3.0)
    assert savings > 0
    assert session._last_prompt_cache_activity_at > 0
