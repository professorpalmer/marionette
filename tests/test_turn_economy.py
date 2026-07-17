"""Identity + wiring tests for the TurnEconomy facade (PR1 facade, PR2 session wire)."""
from __future__ import annotations

import ast
import os
import tempfile

import pytest

from harness.append_only_context import append_only_setting, should_enable_append_only
from harness.compaction_advisor import (
    _HOT_NOW_RATIO,
    advice_payload,
    assess_layer_pressure,
)
from harness.context_budget import BudgetConfig, PERSISTED_OUTPUT_TAG, enforce_turn_budget, maybe_persist_result
from harness.spill_registry import spill_usage_payload
from harness.tool_output_savings import make_compaction_callback, session_savings_payload
from harness.turn_budget import parse_turn_budget
from harness.turn_economy import TurnEconomy
from harness.wiki_grounding_savings import (
    JSONL_FILENAME,
    parse_jsonl_records,
    session_grounding_payload,
    try_record_grounding,
)

# Offload gate floor: 3000 tokens ~= 12000 chars (same as test_context_budget).
_GATE_FLOOR_CHARS = 12_500


def _economy(tmpdir: str, *, session_id: str = "sess-te", job_id: str | None = "job-te") -> TurnEconomy:
    return TurnEconomy(
        state_dir=tmpdir,
        session_id=session_id,
        job_id=job_id,
        config=BudgetConfig(max_result_chars=10, turn_budget_chars=3000, preview_chars=1500),
    )


def test_module_keeps_autobudget_out():
    path = os.path.join(os.path.dirname(__file__), "..", "harness", "turn_economy.py")
    source = open(path, encoding="utf-8").read()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
                for alias in node.names:
                    imported.add(alias.name)
    assert "autobudget" not in imported
    assert "AutoBudget" not in imported
    # Docstring may mention the exclusion; ensure no import/attribute usage.
    assert not any(
        isinstance(node, (ast.Name, ast.Attribute))
        and (
            (isinstance(node, ast.Name) and node.id == "AutoBudget")
            or (isinstance(node, ast.Attribute) and node.attr == "AutoBudget")
        )
        for node in ast.walk(tree)
    )


def test_parse_output_directive_matches_parse_turn_budget():
    economy = TurnEconomy(state_dir=".", session_id="s")
    samples = [
        "+50k",
        "+1.5m!",
        "Please keep it short +5k thanks",
        "$+5k",
        "",
        None,
    ]
    for text in samples:
        assert economy.parse_output_directive(text) == parse_turn_budget(text)  # type: ignore[arg-type]


def test_resolve_append_only_matches_helpers(monkeypatch):
    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", "auto")
    economy = TurnEconomy(state_dir=".", session_id="s")
    cases = [
        ("http://localhost:11434/v1", "ollama"),
        ("https://api.openai.com/v1", "gpt-4"),
        ("https://openrouter.ai/api/v1", "openrouter"),
    ]
    for base_url, driver_name in cases:
        expected = should_enable_append_only(
            append_only_setting(), base_url, driver_name
        )
        assert economy.resolve_append_only(base_url, driver_name) is expected

    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", "off")
    assert economy.resolve_append_only("http://localhost:11434", "ollama") is False
    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", "on")
    assert economy.resolve_append_only("https://api.openai.com/v1", "gpt-4") is True


def test_persist_tool_result_matches_maybe_persist_result():
    with tempfile.TemporaryDirectory() as tmpdir:
        economy = _economy(tmpdir)
        small = "small"
        assert economy.persist_tool_result(small, "id-small") == maybe_persist_result(
            small,
            "id-small",
            tmpdir,
            economy.config,
            on_compaction=make_compaction_callback(
                state_dir=tmpdir,
                session_id="sess-te",
                tool_call_id="id-small",
                job_id="job-te",
            ),
            spill_session_id="sess-te",
        )

        large = "x" * _GATE_FLOOR_CHARS
        via_facade = economy.persist_tool_result(large, "id-large")
        via_direct = maybe_persist_result(
            large,
            "id-large-direct",
            tmpdir,
            economy.config,
            on_compaction=make_compaction_callback(
                state_dir=tmpdir,
                session_id="sess-te",
                tool_call_id="id-large-direct",
                job_id="job-te",
            ),
            spill_session_id="sess-te",
        )
        assert PERSISTED_OUTPUT_TAG in via_facade
        assert PERSISTED_OUTPUT_TAG in via_direct
        assert "spill://" in via_facade or "pmharness-results/id-large.txt" in via_facade
        assert os.path.exists(os.path.join(tmpdir, "pmharness-results", "id-large.txt"))


def test_enforce_tool_batch_matches_enforce_turn_budget():
    with tempfile.TemporaryDirectory() as tmpdir:
        economy = _economy(tmpdir)
        messages_a = [
            {"role": "tool", "tool_call_id": "tc1", "content": "small content"},
            {"role": "tool", "tool_call_id": "tc2", "content": "b" * _GATE_FLOOR_CHARS},
        ]
        messages_b = [dict(m) for m in messages_a]

        out_facade = economy.enforce_tool_batch(messages_a)
        out_direct = enforce_turn_budget(
            messages_b,
            tmpdir,
            economy.config,
            savings_session_id="sess-te",
            savings_job_id="job-te",
        )
        assert out_facade is messages_a
        assert out_direct is messages_b
        assert PERSISTED_OUTPUT_TAG in messages_a[1]["content"]
        assert PERSISTED_OUTPUT_TAG in messages_b[1]["content"]
        assert messages_a[0]["content"] == messages_b[0]["content"] == "small content"


def test_record_wiki_grounding_matches_try_record_grounding():
    with tempfile.TemporaryDirectory() as tmpdir:
        economy = _economy(tmpdir, session_id="wiki-sess")
        economy.record_wiki_grounding(120, 2, price_in=1.0)
        try_record_grounding(
            state_dir=tmpdir,
            session_id="wiki-sess-direct",
            chars=120,
            pages=2,
            price_in=1.0,
        )
        path = os.path.join(tmpdir, JSONL_FILENAME)
        records = parse_jsonl_records(path)
        assert len(records) == 2
        by_session = {r["session_id"]: r for r in records}
        assert by_session["wiki-sess"]["chars"] == 120
        assert by_session["wiki-sess"]["pages"] == 2
        assert by_session["wiki-sess-direct"]["chars"] == 120


def test_advise_compaction_with_snapshot_matches_assess():
    economy = TurnEconomy(state_dir=".", session_id="s")
    budget = 1000
    snapshot = {
        "L0": {"bytes": int(budget * 4 * _HOT_NOW_RATIO), "entries": 1},
        "L1": {"bytes": 0, "entries": 0, "components": {}},
        "L2": {"bytes": 0, "entries": 0, "components": {}},
        "L3": {"bytes": 0, "entries": 0, "components": {}},
    }
    assert economy.advise_compaction(budget, snapshot=snapshot) == assess_layer_pressure(
        snapshot, budget
    )


def test_advise_compaction_without_snapshot_matches_advice_payload(tmp_path):
    economy = TurnEconomy(state_dir=str(tmp_path), session_id="advise-sess")
    assert economy.advise_compaction(96_000) == advice_payload(
        str(tmp_path), "advise-sess", 96_000
    )


def test_default_config_when_omitted(tmp_path):
    economy = TurnEconomy(state_dir=str(tmp_path), session_id="s")
    assert isinstance(economy.config, BudgetConfig)
    assert economy.session_id == "s"
    assert economy.job_id is None


def test_usage_fields_match_payload_helpers(tmp_path):
    economy = _economy(str(tmp_path), session_id="usage-sess")
    assert economy.spill_usage_fields() == spill_usage_payload(
        str(tmp_path), "usage-sess"
    )
    assert economy.tool_output_savings_fields(1.5) == session_savings_payload(
        str(tmp_path), "usage-sess", 1.5
    )
    assert economy.tool_output_savings_fields(1.5, session_id="") == session_savings_payload(
        str(tmp_path), "", 1.5
    )
    assert economy.wiki_grounding_fields(2.0) == session_grounding_payload(
        str(tmp_path), "usage-sess", 2.0
    )


def test_conversation_hot_path_routes_through_turn_economy():
    """PR2: session hot path must call TurnEconomy methods, not raw helpers.

    After the audit peel, call sites live across conversation.py and the
    mixins it composes (send_loop, wiki_distill, compaction_mixin).
    """
    harness_dir = os.path.join(os.path.dirname(__file__), "..", "harness")
    scan_files = (
        "conversation.py",
        "send_loop.py",
        "wiki_distill.py",
        "compaction_mixin.py",
    )

    forbidden_direct = {
        "maybe_persist_result",
        "enforce_turn_budget",
        "parse_turn_budget",
        "try_record_grounding",
        "make_compaction_callback",
        "assess_layer_pressure",
        "spill_usage_payload",
        "session_grounding_payload",
        "session_savings_payload",
        "should_enable_append_only",
    }
    imported_names: set[str] = set()
    called_attrs: set[str] = set()
    for name in scan_files:
        path = os.path.join(harness_dir, name)
        source = open(path, encoding="utf-8").read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported_names.add(alias.name)
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "_turn_economy"
            ):
                called_attrs.add(node.attr)

    leaked = forbidden_direct & imported_names
    assert not leaked, f"session modules still import raw helpers: {sorted(leaked)}"

    for required in (
        "persist_tool_result",
        "enforce_tool_batch",
        "parse_output_directive",
        "record_wiki_grounding",
        "resolve_append_only",
        "spill_usage_fields",
        "tool_output_savings_fields",
        "wiki_grounding_fields",
        "advise_compaction",
    ):
        assert required in called_attrs, f"missing _turn_economy.{required} call site"

    # AutoBudget stays on ConversationalSession (run_auto) but must not be
    # folded into TurnEconomy — covered by test_module_keeps_autobudget_out.
