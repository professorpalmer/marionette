"""Standing economics (HARNESS_STANDING_ECONOMICS) — estimated floor + TTL."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

# usage_meters ↔ cost is circular at import time; load server first.
import harness.server  # noqa: F401
from harness.api.usage_meters import (
    _standing_economics_fields,
    prompt_cache_ttl_ms_for_driver,
    standing_economics_enabled,
)


def test_standing_economics_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("HARNESS_STANDING_ECONOMICS", raising=False)
    assert standing_economics_enabled() is False


def test_standing_economics_flag_on(monkeypatch):
    monkeypatch.setenv("HARNESS_STANDING_ECONOMICS", "1")
    assert standing_economics_enabled() is True


def test_prompt_cache_ttl_claude_1h(monkeypatch):
    monkeypatch.delenv("HARNESS_ANTHROPIC_CACHE_TTL", raising=False)
    assert prompt_cache_ttl_ms_for_driver("anthropic/claude-sonnet-4") == 3_600_000


def test_prompt_cache_ttl_claude_5m_arm(monkeypatch):
    monkeypatch.setenv("HARNESS_ANTHROPIC_CACHE_TTL", "5m")
    assert prompt_cache_ttl_ms_for_driver("claude-opus-4") == 300_000


def test_prompt_cache_ttl_unknown_provider_refuses():
    assert prompt_cache_ttl_ms_for_driver("groq/llama") is None
    assert prompt_cache_ttl_ms_for_driver("") is None


def test_standing_fields_omitted_when_flag_off(monkeypatch):
    monkeypatch.delenv("HARNESS_STANDING_ECONOMICS", raising=False)
    assert _standing_economics_fields(3.0) == {}


def test_standing_fields_estimated_floor_and_warm_ttl(monkeypatch):
    monkeypatch.setenv("HARNESS_STANDING_ECONOMICS", "1")
    monkeypatch.delenv("HARNESS_ANTHROPIC_CACHE_TTL", raising=False)

    import time

    now = time.time()
    pilot = SimpleNamespace(
        config=SimpleNamespace(driver="anthropic/claude-sonnet-4"),
        _last_prompt_cache_activity_at=now - 600,  # 10 minutes ago
        _last_turn_cache_read_tokens=12_000,
        get_context_usage=lambda: {
            "categories": [
                {"name": "System prompt", "tokens": 10_000},
                {"name": "Tool definitions", "tokens": 20_000},
                {"name": "Rules", "tokens": 1_000},
                {"name": "Conversation", "tokens": 50_000},
            ]
        },
    )

    import harness.api.usage_meters as um

    monkeypatch.setattr(um, "_pilot", lambda: pilot)
    monkeypatch.setattr(um, "_cfg", lambda: SimpleNamespace(driver="anthropic/claude-sonnet-4"))

    payload = _standing_economics_fields(3.0)
    assert payload["standing_economics_basis"] == "estimated"
    assert payload["standing_floor_tokens"] == 31_000
    assert payload["standing_system_tokens"] == 10_000
    assert payload["standing_tool_tokens"] == 20_000
    # 31000/1e6 * 3.0
    assert payload["standing_floor_cost_usd"] == pytest.approx(0.093)
    # Cached floor at 0.1x — only while warm.
    assert payload["standing_floor_cost_cached_usd"] == pytest.approx(0.0093)
    assert payload["prompt_cache_ttl_ms"] == 3_600_000
    assert payload["prompt_cache_state"] == "warm"
    assert payload["prompt_cache_expires_in_ms"] > 0


def test_standing_fields_refuse_cached_value_after_expiry(monkeypatch):
    monkeypatch.setenv("HARNESS_STANDING_ECONOMICS", "1")
    monkeypatch.delenv("HARNESS_ANTHROPIC_CACHE_TTL", raising=False)

    import time

    now = time.time()
    pilot = SimpleNamespace(
        config=SimpleNamespace(driver="anthropic/claude-sonnet-4"),
        # Older than 1h TTL.
        _last_prompt_cache_activity_at=now - 4_000,
        _last_turn_cache_read_tokens=12_000,
        get_context_usage=lambda: {
            "categories": [
                {"name": "System prompt", "tokens": 5_000},
                {"name": "Tool definitions", "tokens": 5_000},
            ]
        },
    )

    import harness.api.usage_meters as um

    monkeypatch.setattr(um, "_pilot", lambda: pilot)
    monkeypatch.setattr(um, "_cfg", lambda: SimpleNamespace(driver="anthropic/claude-sonnet-4"))

    payload = _standing_economics_fields(3.0)
    assert payload["prompt_cache_state"] == "expired"
    assert payload["prompt_cache_expires_in_ms"] == 0
    assert "standing_floor_cost_cached_usd" not in payload
    # Uncached floor still shown as estimated.
    assert payload["standing_floor_cost_usd"] == pytest.approx(0.03)
    assert payload["standing_economics_basis"] == "estimated"


def test_standing_fields_omit_cache_when_prompt_cache_disabled(monkeypatch):
    monkeypatch.setenv("HARNESS_STANDING_ECONOMICS", "1")
    monkeypatch.setenv("HARNESS_PROMPT_CACHE", "0")
    monkeypatch.delenv("HARNESS_ANTHROPIC_CACHE_TTL", raising=False)

    import time

    now = time.time()
    pilot = SimpleNamespace(
        config=SimpleNamespace(driver="anthropic/claude-sonnet-4"),
        _last_prompt_cache_activity_at=now - 600,
        _last_turn_cache_read_tokens=12_000,
        get_context_usage=lambda: {
            "categories": [
                {"name": "System prompt", "tokens": 5_000},
                {"name": "Tool definitions", "tokens": 5_000},
            ]
        },
    )

    import harness.api.usage_meters as um

    monkeypatch.setattr(um, "_pilot", lambda: pilot)
    monkeypatch.setattr(um, "_cfg", lambda: SimpleNamespace(driver="anthropic/claude-sonnet-4"))

    payload = _standing_economics_fields(3.0)
    assert payload["standing_floor_cost_usd"] == pytest.approx(0.03)
    assert "standing_floor_cost_cached_usd" not in payload
    assert "prompt_cache_ttl_ms" not in payload
    assert "prompt_cache_state" not in payload


def test_standing_fields_refuse_cached_floor_without_activity(monkeypatch):
    monkeypatch.setenv("HARNESS_STANDING_ECONOMICS", "1")
    monkeypatch.delenv("HARNESS_PROMPT_CACHE", raising=False)
    monkeypatch.delenv("HARNESS_ANTHROPIC_CACHE_TTL", raising=False)

    pilot = SimpleNamespace(
        config=SimpleNamespace(driver="anthropic/claude-sonnet-4"),
        get_context_usage=lambda: {
            "categories": [
                {"name": "System prompt", "tokens": 5_000},
                {"name": "Tool definitions", "tokens": 5_000},
            ]
        },
    )

    import harness.api.usage_meters as um

    monkeypatch.setattr(um, "_pilot", lambda: pilot)
    monkeypatch.setattr(um, "_cfg", lambda: SimpleNamespace(driver="anthropic/claude-sonnet-4"))

    payload = _standing_economics_fields(3.0)
    assert payload["standing_floor_cost_usd"] == pytest.approx(0.03)
    assert "standing_floor_cost_cached_usd" not in payload
    assert "prompt_cache_state" not in payload


def test_standing_fields_refuse_cached_floor_on_prompt_miss_without_cache_read(monkeypatch):
    """Recent prompt activity without cache read must not claim cached-floor value."""
    monkeypatch.setenv("HARNESS_STANDING_ECONOMICS", "1")
    monkeypatch.delenv("HARNESS_ANTHROPIC_CACHE_TTL", raising=False)

    import time

    now = time.time()
    pilot = SimpleNamespace(
        config=SimpleNamespace(driver="anthropic/claude-sonnet-4"),
        _last_prompt_cache_activity_at=now - 60,
        _last_turn_cache_read_tokens=0,
        get_context_usage=lambda: {
            "categories": [
                {"name": "System prompt", "tokens": 5_000},
                {"name": "Tool definitions", "tokens": 5_000},
            ]
        },
    )

    import harness.api.usage_meters as um

    monkeypatch.setattr(um, "_pilot", lambda: pilot)
    monkeypatch.setattr(um, "_cfg", lambda: SimpleNamespace(driver="anthropic/claude-sonnet-4"))

    payload = _standing_economics_fields(3.0)
    assert payload["standing_floor_cost_usd"] == pytest.approx(0.03)
    assert "standing_floor_cost_cached_usd" not in payload
    assert "prompt_cache_state" not in payload


def test_tool_output_savings_omits_standing_for_process_wide(monkeypatch):
    monkeypatch.setenv("HARNESS_STANDING_ECONOMICS", "1")

    import harness.api.usage_meters as um

    pilot = SimpleNamespace(
        state_dir="/tmp/unused",
        harness_session_id="sess-1",
        config=SimpleNamespace(max_context_tokens=96000),
    )
    monkeypatch.setattr(um, "_pilot", lambda: pilot)
    monkeypatch.setattr(
        um,
        "_standing_economics_fields",
        lambda _pin: {"standing_economics_basis": "estimated"},
    )
    monkeypatch.setattr(
        "harness.tool_output_savings.session_savings_payload",
        lambda *a, **k: {
            "tool_output_tokens_saved": 0,
            "tool_output_savings_usd": 0.0,
            "tool_output_compactions": 0,
        },
    )
    monkeypatch.setattr(
        "harness.history_compaction_journal.history_compaction_payload",
        lambda *a, **k: {},
    )
    monkeypatch.setattr("harness.spill_registry.spill_usage_payload", lambda *a, **k: {})
    monkeypatch.setattr("harness.eval_history.eval_history_payload", lambda *a, **k: {})
    monkeypatch.setattr(
        "harness.memory_layers.latest_layer_snapshot",
        lambda *a, **k: {},
    )
    monkeypatch.setattr("harness.compaction_advisor.advice_payload", lambda *a, **k: {})

    session_payload = um._tool_output_savings_fields(3.0, process_wide=False)
    boot_payload = um._tool_output_savings_fields(3.0, process_wide=True)
    assert session_payload.get("standing_economics_basis") == "estimated"
    assert "standing_economics_basis" not in boot_payload

