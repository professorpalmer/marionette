"""Hermetic tests for the NL memory/wiki query synthesis core.

The model call is injected as a fake `complete`, so these tests run with NO
network and NO API keys, per AGENTS.md (intent/pure layer stays hermetic).
"""
import pytest

from harness.nl_memory import (
    answer_from_memory,
    build_prompt,
    parse_citations,
    NOT_FOUND,
)


ENTRIES = [
    {
        "title": "Driver decision",
        "body": "We decided the default driver should be the local adapter for deterministic evals.",
        "source": "wiki/decisions",
    },
    {
        "title": "Auth notes",
        "body": "Auth uses JWT tokens verified in middleware.",
        "source": "wiki/auth",
    },
    {
        "title": "Scoring",
        "body": "Scoring is deterministic, no LLM-as-judge.",
        "source": "wiki/scoring",
    },
]


def test_grounded_answer_with_citations():
    def fake_complete(prompt):
        # The grounded fact lives in entry 1.
        assert "default driver" in prompt
        return "The default driver is the local adapter [1]."

    out = answer_from_memory(
        "what did we decide about the default driver?", ENTRIES, complete=fake_complete
    )
    assert "local adapter" in out["answer"]
    assert out["citations"] == [1]
    assert out["used_entry_ids"] == ["Driver decision"]


def test_not_found_when_unsupported():
    def fake_complete(prompt):
        return NOT_FOUND

    out = answer_from_memory(
        "what is the release schedule?", ENTRIES, complete=fake_complete
    )
    assert out["answer"] == NOT_FOUND
    assert out["citations"] == []
    assert out["used_entry_ids"] == []


def test_not_found_case_insensitive():
    def fake_complete(prompt):
        return "Not Found In Memory"

    out = answer_from_memory("q", ENTRIES, complete=fake_complete)
    assert out["answer"] == NOT_FOUND
    assert out["citations"] == []


def test_citations_map_back_to_right_entries():
    def fake_complete(prompt):
        # Cite entries 3 and 2, in that order, plus an out-of-range [9] that
        # must be dropped.
        return "Scoring is deterministic [3] and auth uses JWT [2] [9]."

    out = answer_from_memory("summarize", ENTRIES, complete=fake_complete)
    assert out["citations"] == [3, 2]
    assert out["used_entry_ids"] == ["Scoring", "Auth notes"]


def test_empty_entries_guard_does_not_call_model():
    calls = []

    def fake_complete(prompt):
        calls.append(prompt)
        return "should not happen"

    out = answer_from_memory("anything", [], complete=fake_complete)
    assert out == {"answer": NOT_FOUND, "citations": [], "used_entry_ids": []}
    assert calls == []


def test_empty_entries_guard_without_complete():
    # Empty entries short-circuit before the complete requirement.
    out = answer_from_memory("anything", [])
    assert out["answer"] == NOT_FOUND


def test_requires_complete_when_entries_present():
    with pytest.raises(ValueError):
        answer_from_memory("q", ENTRIES)


def test_empty_model_output_is_not_found():
    def fake_complete(prompt):
        return "   "

    out = answer_from_memory("q", ENTRIES, complete=fake_complete)
    assert out["answer"] == NOT_FOUND
    assert out["citations"] == []


def test_entry_id_falls_back_to_synthetic():
    entries = [{"body": "no title here", "source": "s"}]

    def fake_complete(prompt):
        return "Here it is [1]."

    out = answer_from_memory("q", entries, complete=fake_complete)
    assert out["citations"] == [1]
    assert out["used_entry_ids"] == ["entry-1"]


def test_explicit_id_preferred():
    entries = [{"id": "mem-42", "title": "T", "body": "b", "source": "s"}]

    def fake_complete(prompt):
        return "Answer [1]."

    out = answer_from_memory("q", entries, complete=fake_complete)
    assert out["used_entry_ids"] == ["mem-42"]


def test_build_prompt_numbers_and_instructs():
    prompt = build_prompt("what about driver?", ENTRIES)
    assert "[1] Driver decision" in prompt
    assert "[2] Auth notes" in prompt
    assert "[3] Scoring" in prompt
    assert "ONLY" in prompt
    assert NOT_FOUND in prompt
    assert "what about driver?" in prompt


def test_parse_citations_dedupes_and_ranges():
    assert parse_citations("[1] foo [1] bar [2]", 3) == [1, 2]
    assert parse_citations("[5] out of range", 3) == []
    assert parse_citations("no citations here", 3) == []
