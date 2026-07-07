"""Tests for shared tool-output offload savings gate."""
from __future__ import annotations

import os
import tempfile

import pytest

from harness.context_budget import BudgetConfig, maybe_persist_result
from harness.offload_policy import (
    MIN_TOOL_RESULT_TOKENS,
    SAVINGS_MARGIN,
    gate_decision,
    should_offload,
)
from harness.tool_output_savings import (
    CHARS_PER_TOKEN,
    ToolOutputSavingsLedger,
    make_compaction_callback,
    tokens_avoided,
)


def _above_floor_chars(extra_tokens: int = 500) -> int:
    return (MIN_TOOL_RESULT_TOKENS + extra_tokens) * CHARS_PER_TOKEN


def test_below_floor_never_offloaded():
    original = _above_floor_chars(-1)
    replacement = 100
    assert not should_offload(original, replacement)
    decision = gate_decision(original, replacement)
    assert not decision["offload"]
    assert "below floor" in decision["reason"]


def test_replacement_above_margin_rejected():
    original = _above_floor_chars(500)
    replacement = int(original * SAVINGS_MARGIN) + 1
    assert not should_offload(original, replacement)
    decision = gate_decision(original, replacement)
    assert not decision["offload"]
    assert "margin" in decision["reason"]


def test_big_result_small_stub_accepted():
    original = _above_floor_chars(1000)
    replacement = 500
    assert should_offload(original, replacement)
    decision = gate_decision(original, replacement)
    assert decision["offload"]
    assert decision["estimated_tokens_saved"] == tokens_avoided(original, replacement)


def test_maybe_persist_result_respects_gate(tmp_path):
    config = BudgetConfig(max_result_chars=10, turn_budget_chars=50)
    small = "x" * _above_floor_chars(-1)
    result = maybe_persist_result(small, "tc-small", str(tmp_path), config)
    assert result == small
    assert not (tmp_path / "pmharness-results").exists()


def test_ledger_records_once_per_session_tool_call_through_gate(tmp_path):
    original = _above_floor_chars(2000)
    replacement = 800
    assert should_offload(original, replacement)

    state_dir = str(tmp_path)
    session_id = "sess-gate"
    tool_call_id = "tc-gate"
    callback = make_compaction_callback(
        state_dir=state_dir,
        session_id=session_id,
        tool_call_id=tool_call_id,
    )
    callback(original, replacement, "persist")
    callback(original, replacement - 100, "persist")

    ledger = ToolOutputSavingsLedger(state_dir)
    summary = ledger.summarize(session_id=session_id)
    assert summary.record_count == 1
    assert summary.tokens_saved == tokens_avoided(original, replacement)


def test_env_overrides(monkeypatch):
    original = _above_floor_chars(1000)
    replacement = 500
    monkeypatch.setenv("HARNESS_OFFLOAD_MIN_TOKENS", "5000")
    assert not should_offload(original, replacement)

    monkeypatch.delenv("HARNESS_OFFLOAD_MIN_TOKENS", raising=False)
    monkeypatch.setenv("HARNESS_OFFLOAD_MARGIN", "0.5")
    replacement = int(original * 0.6)
    assert not should_offload(original, replacement)


def test_gate_never_raises_on_garbage_inputs():
    for original, replacement in [
        (-1, 10),
        (0, 0),
        (10**12, 1),
        ("bad", "worse"),
        (None, None),
    ]:
        decision = gate_decision(original, replacement)  # type: ignore[arg-type]
        assert isinstance(decision, dict)
        assert "offload" in decision
        assert "reason" in decision
        assert "estimated_tokens_saved" in decision


def test_maybe_persist_large_result_passes_gate(tmp_path):
    config = BudgetConfig(max_result_chars=100, turn_budget_chars=50000)
    large = "a" * _above_floor_chars(5000)
    result = maybe_persist_result(large, "tc-big", str(tmp_path), config)
    assert "<persisted-output>" in result
    file_path = tmp_path / "pmharness-results" / "tc-big.txt"
    assert file_path.exists()
