from __future__ import annotations

"""Hermetic compaction-residual laboratory tests.

Runs through the real ConversationalSession compaction seam and the Wave 1
archive-backed peek_history path. No API keys. No fake string-only runner.
"""

from harness.compaction_archive import load_compaction_archive_messages
from harness.compaction_residual import RESIDUAL_CATALOG, compaction_residual_mode
from pmharness.compaction_residual_battery import RESIDUAL_CASES, ResidualCase
from pmharness.compaction_residual_bench import (
    ARM_A,
    ARM_B,
    ARM_C,
    ARM_D,
    SCRIPTED_SUMMARY,
    run_compaction_residual_bench,
    run_residual_arm,
    score_residual_text,
)


REQUIRED_TEMPLATES = {
    "early_constraint",
    "mid_session_file_path",
    "reversed_decision",
    "error_tail_fact",
    "spill_artifact_handle",
    "distractor_twin",
    "catalog_miss_plain_fact",
}


def test_battery_schema_covers_six_templates():
    templates = {case.template for case in RESIDUAL_CASES}
    assert REQUIRED_TEMPLATES <= templates
    assert len(RESIDUAL_CASES) >= 6
    for case in RESIDUAL_CASES:
        assert isinstance(case, ResidualCase)
        assert case.id
        assert case.transcript
        assert case.transcript[0]["role"] == "system"
        assert case.probe_prompt
        assert case.must_contain
        assert isinstance(case.must_not_contain, tuple)
        assert set(case.expected_arms) >= {"A", "B", "C", "D"}
        assert case.expected_arms["A"]["residual"] == "summary"
        assert case.expected_arms["B"]["residual"] == "catalog"
        assert case.expected_arms["C"]["peek"] is True
        assert case.expected_arms["D"]["compact"] is False
    distractor = next(c for c in RESIDUAL_CASES if c.id == "distractor_twin")
    assert distractor.must_not_contain == ("auth_legacy_v1.py",)
    nonce_facts = next(c for c in RESIDUAL_CASES if c.id == "catalog_miss_plain_fact")
    assert nonce_facts.catalog_recalls_fact is True
    assert len(nonce_facts.must_contain) >= 2


def test_scoring_is_deterministic_substring_oracle():
    case = ResidualCase(
        id="unit",
        template="early_constraint",
        transcript=({"role": "system", "content": "s"},),
        probe_prompt="p",
        must_contain=("alpha-fact", "gamma-fact"),
        must_not_contain=("beta-fab",),
    )
    # must_contain is all-of: one token is not enough.
    partial = score_residual_text(case, "the ALPHA-FACT is present")
    assert partial["buried_fact_recall"] is False
    assert partial["false_recall"] is False
    assert partial["end_task_success"] is False
    hit = score_residual_text(case, "the ALPHA-FACT and GAMMA-FACT are present")
    assert hit["buried_fact_recall"] is True
    assert hit["false_recall"] is False
    assert hit["end_task_success"] is True
    fab = score_residual_text(case, "beta-fab only")
    assert fab["buried_fact_recall"] is False
    assert fab["false_recall"] is True
    assert fab["end_task_success"] is False
    silent = score_residual_text(case, "nothing relevant")
    assert silent["buried_fact_recall"] is False
    assert silent["false_recall"] is False
    assert silent["end_task_success"] is False


def test_default_catalog_mode_untouched_by_import(monkeypatch):
    monkeypatch.delenv("HARNESS_COMPACTION_RESIDUAL", raising=False)
    assert compaction_residual_mode() == RESIDUAL_CATALOG


def test_all_four_arms_on_representative_cases(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_COMPACTION_RESIDUAL", raising=False)
    representative = ("early_constraint", "spill_artifact_handle", "distractor_twin")
    result = run_compaction_residual_bench(
        case_ids=representative,
        state_dir=str(tmp_path),
    )
    assert result["n"] == 12
    by_key = {(row["case_id"], row["arm"]): row for row in result["rows"]}

    for case_id in representative:
        arm_a = by_key[(case_id, ARM_A)]
        arm_b = by_key[(case_id, ARM_B)]
        arm_c = by_key[(case_id, ARM_C)]
        arm_d = by_key[(case_id, ARM_D)]

        assert arm_a["compacted"] is True
        assert arm_a["mode"] == "llm"
        assert arm_a["residual_mode"] == "summary"
        assert arm_a["peek_calls"] == 0
        # Scripted paragraph still omits tokens; last-wins story can restore
        # distinctive last-N facts after filler skip.
        assert arm_a["peek_buried_fact_recall"] is False

        assert arm_b["compacted"] is True
        assert arm_b["mode"] == "extractive"
        assert arm_b["residual_mode"] == "catalog"
        assert arm_b["buried_fact_recall"] is True
        assert arm_b["residual_buried_fact_recall"] is True
        assert arm_b["peek_buried_fact_recall"] is False
        assert arm_b["false_recall"] is False
        assert arm_b["end_task_success"] is True
        assert arm_b["peek_calls"] == 0

        assert arm_c["compacted"] is True
        assert arm_c["mode"] == "extractive"
        assert arm_c["peek_calls"] >= 1
        assert arm_c["peek_tokens"] > 0
        assert arm_c["buried_fact_recall"] is True
        assert arm_c["residual_buried_fact_recall"] is True
        assert arm_c["peek_buried_fact_recall"] is True
        assert arm_c["end_task_success"] is True
        archive = load_compaction_archive_messages(
            str(tmp_path),
            f"residual-{case_id}-C",
        )
        assert archive, "arm C must use the Wave 1 archive, not fake recall"

        assert arm_d["compacted"] is False
        assert arm_d["residual_mode"] == "off"
        assert arm_d["event_kinds"] == []
        assert arm_d["buried_fact_recall"] is True
        assert arm_d["residual_tokens"] > arm_b["residual_tokens"]


def test_full_battery_no_keys_and_tail_integrity(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("HARNESS_COMPACTION_RESIDUAL", raising=False)
    result = run_compaction_residual_bench(state_dir=str(tmp_path))
    assert result["n"] == len(RESIDUAL_CASES) * 4
    by_id = {case.id: case for case in RESIDUAL_CASES}
    for row in result["rows"]:
        assert row["arm"] in (ARM_A, ARM_B, ARM_C, ARM_D)
        assert row["case_id"]
        assert "buried_fact_recall" in row
        assert "false_recall" in row
        assert "residual_buried_fact_recall" in row
        assert "peek_buried_fact_recall" in row
        assert "peek_false_recall" in row
        assert "peek_task_success" in row
        assert "residual_tokens" in row
        assert "peek_calls" in row
        assert "peek_tokens" in row
        assert "end_task_success" in row
        case = by_id[row["case_id"]]
        if row["arm"] == ARM_B:
            if case.catalog_recalls_fact:
                assert row["end_task_success"] is True
                assert row["residual_buried_fact_recall"] is True
            else:
                assert row["residual_buried_fact_recall"] is False
                assert row["end_task_success"] is False
        if row["arm"] == ARM_C:
            assert row["end_task_success"] is True
            if not case.catalog_recalls_fact:
                assert row["residual_buried_fact_recall"] is False
                assert row["peek_buried_fact_recall"] is True
                assert row["peek_task_success"] is True
        if row["arm"] == ARM_D:
            assert row["compacted"] is False
            assert row["buried_fact_recall"] is True


def test_arm_c_peek_reads_elided_middle(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_COMPACTION_RESIDUAL", raising=False)
    case = next(c for c in RESIDUAL_CASES if c.id == "early_constraint")
    receipt = run_residual_arm(case, ARM_C, state_dir=str(tmp_path))
    assert receipt["peek_calls"] >= 1
    archive = load_compaction_archive_messages(
        str(tmp_path),
        f"residual-{case.id}-C",
    )
    archive_text = " ".join(str(m.get("content") or "") for m in archive)
    assert "never write to production.db" in archive_text
    assert receipt["buried_fact_recall"] is True
    assert receipt["peek_buried_fact_recall"] is True


def test_catalog_miss_plain_fact_is_now_residual_recall(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_COMPACTION_RESIDUAL", raising=False)
    case = next(c for c in RESIDUAL_CASES if c.id == "catalog_miss_plain_fact")
    assert case.catalog_recalls_fact is True
    arm_b = run_residual_arm(case, ARM_B, state_dir=str(tmp_path))
    arm_c = run_residual_arm(case, ARM_C, state_dir=str(tmp_path))
    assert arm_b["residual_buried_fact_recall"] is True
    assert arm_b["buried_fact_recall"] is True
    assert arm_b["end_task_success"] is True
    assert arm_b["peek_calls"] == 0
    assert arm_c["residual_buried_fact_recall"] is True
    assert arm_c["buried_fact_recall"] is True
    assert arm_c["end_task_success"] is True
    archive = load_compaction_archive_messages(
        str(tmp_path),
        f"residual-{case.id}-C",
    )
    archive_text = " ".join(str(m.get("content") or "") for m in archive)
    assert "omega-cache-token-9f3a" in archive_text
    assert "shard-omega-p95" in archive_text


def test_scripted_summary_omits_buried_tokens():
    blob = SCRIPTED_SUMMARY.lower()
    for case in RESIDUAL_CASES:
        assert not any(token.lower() in blob for token in case.must_contain)
