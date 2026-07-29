"""Hermetic tests for HARNESS_SCHEMA_TOKEN_CALIBRATION (S07)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.schema_token_calibration import (
    SchemaTokenCalibrator,
    cold_start_tool_tokens,
    estimate_tools_from_schema,
    legacy_whole_schema_tokens,
    per_tool_cap_tokens,
    schema_token_calibration_enabled,
)
from harness.send_loop_phases import meter_pilot_step


def _sample_tools() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write a file",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


def _session(budget: int = 100_000) -> ConversationalSession:
    cfg = HarnessConfig(max_context_tokens=budget)
    s = ConversationalSession(cfg)
    s._history[0]["content"] = "sys"
    return s


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("HARNESS_SCHEMA_TOKEN_CALIBRATION", raising=False)
    assert schema_token_calibration_enabled() is False


def test_flag_on(monkeypatch):
    monkeypatch.setenv("HARNESS_SCHEMA_TOKEN_CALIBRATION", "1")
    assert schema_token_calibration_enabled() is True


def test_cold_start_chars_div_four():
    assert cold_start_tool_tokens(400) == 100
    assert cold_start_tool_tokens(401) == 100


def test_per_tool_cap(monkeypatch):
    monkeypatch.setenv("HARNESS_SCHEMA_TOKEN_PER_TOOL_CAP", "50")
    assert per_tool_cap_tokens() == 50
    assert cold_start_tool_tokens(400) == 50


def test_estimate_tools_from_schema_deterministic():
    tools = _sample_tools()
    total_a, rows_a = estimate_tools_from_schema(tools)
    total_b, rows_b = estimate_tools_from_schema(tools)
    assert total_a == total_b
    assert len(rows_a) == 2
    assert rows_a[0]["name"] == "read_file"
    assert rows_a[0]["estimated_tokens"] == rows_a[0]["schema_chars"] // 4


def test_flag_off_context_usage_parity(monkeypatch):
    monkeypatch.delenv("HARNESS_SCHEMA_TOKEN_CALIBRATION", raising=False)
    s = _session()
    usage = s.get_context_usage()
    tools = s._build_visible_tools_schema()
    legacy = len(json.dumps(tools)) // 4
    tool_cat = next(c for c in usage["categories"] if c["name"] == "Tool definitions")
    assert tool_cat["tokens"] == legacy
    assert "schema_token_calibration_basis" not in usage


def test_flag_on_exposes_estimated_telemetry(monkeypatch):
    monkeypatch.setenv("HARNESS_SCHEMA_TOKEN_CALIBRATION", "1")
    s = _session()
    usage = s.get_context_usage()
    assert usage["schema_token_calibration_basis"] == "estimated"
    assert usage["schema_token_cold_start"] >= 0
    assert usage["schema_token_legacy_floor"] >= 0


def test_ema_updates_from_provider_floor():
    cal = SchemaTokenCalibrator()
    fp = "abc123"
    cold = 100
    non_tool = 500

    cal.maybe_update(
        provider_floor=900,
        non_tool_heuristic=non_tool,
        cold_start_tools=cold,
        fingerprint=fp,
    )
    assert cal.observation_count == 1
    assert cal.ema_factor != 1.0
    assert cal.ema_factor == pytest.approx(0.2 * 4.0 + 0.8 * 1.0)


def test_fuse_never_undercuts_legacy_floor():
    tools = _sample_tools()
    legacy = legacy_whole_schema_tokens(tools)
    cal = SchemaTokenCalibrator()
    cal.ema_factor = 0.1
    billed, cold, _rows = cal.fuse_tool_tokens(tools, cold_start_total=10)
    assert billed >= legacy
    assert billed >= cold


def test_drift_reset_disables_calibration():
    cal = SchemaTokenCalibrator()
    cal.ema_factor = 3.0
    cal.observation_count = 5
    cal._recent_drifts = [0.3, 0.3, 0.3]
    cal.reset()
    assert cal.disabled is True
    assert cal.ema_factor == 1.0
    assert cal.observation_count == 0


def test_sustained_drift_triggers_reset():
    cal = SchemaTokenCalibrator()
    fp = "stable"
    for _ in range(3):
        cal.maybe_update(
            provider_floor=1000,
            non_tool_heuristic=100,
            cold_start_tools=100,
            fingerprint=fp,
        )
    assert cal.disabled is True
    assert cal.ema_factor == 1.0
    assert cal.observation_count == 0


def test_calibration_success_telemetry():
    cal = SchemaTokenCalibrator()
    cal.observation_count = 20
    cal.residual_sum = 100.0
    cal.provider_floor_sum = 20_000.0
    assert cal.mean_residual_pct() == pytest.approx(0.005)
    assert cal.calibration_success() is True


def test_provider_floor_non_undercut_in_context_usage(monkeypatch):
    monkeypatch.setenv("HARNESS_SCHEMA_TOKEN_CALIBRATION", "1")
    s = _session()
    s._history.append({"role": "user", "content": "hi"})

    usage_before = s.get_context_usage()
    heuristic_total = sum(c["tokens"] for c in usage_before["categories"])

    s._last_prompt_tokens = heuristic_total + 5000
    usage_after = s.get_context_usage()
    assert usage_after["total"] == heuristic_total + 5000
    assert usage_after["total"] >= heuristic_total


def test_meter_pilot_step_triggers_calibration_update(monkeypatch):
    monkeypatch.setenv("HARNESS_SCHEMA_TOKEN_CALIBRATION", "1")
    s = _session()
    s._history.append({"role": "user", "content": "hello"})

    prefix = s._context_usage_prefix_tokens()
    tools = s._build_visible_tools_schema()
    cold, _ = estimate_tools_from_schema(tools)
    non_tool = sum(prefix.values())
    provider_floor = non_tool + cold * 2

    resp = SimpleNamespace(tokens_out=5, tokens_in=provider_floor, meta={})
    meter_pilot_step(s, resp, prompt="hello")

    cal = s._schema_token_calibrator
    assert cal.observation_count == 1
    assert s._last_prompt_tokens == provider_floor


def test_no_cost_invention_from_calibration_fields(monkeypatch):
    monkeypatch.setenv("HARNESS_SCHEMA_TOKEN_CALIBRATION", "1")
    s = _session()
    s._tokens_in = 100
    s._tokens_out = 50
    s._tokens_used = 150
    s._provider_cost_usd = 0.42

    usage = s.get_context_usage()
    assert s._tokens_in == 100
    assert s._tokens_out == 50
    assert s._tokens_used == 150
    assert s._provider_cost_usd == 0.42
    assert "schema_token_calibration_basis" in usage
