"""Hermetic tests for lightweight task transactions."""
from __future__ import annotations

from dataclasses import replace

from harness.task_transaction import (
    TaskTransaction,
    as_dict,
    context_block,
    new_transaction,
    note_files,
    note_verification,
)


def test_new_transaction_strips_and_truncates():
    tx = new_transaction("  ship the gate  ")
    assert tx.goal == "ship the gate"
    assert tx.phase == "intake"
    assert tx.files == []
    assert tx.constraints == []
    assert tx.plan == ""
    assert tx.invariants == []
    assert tx.verification == ""

    long_msg = "x" * 600
    tx2 = new_transaction(long_msg)
    assert len(tx2.goal) == 500
    assert tx2.goal == "x" * 500


def test_new_transaction_never_raises_on_none():
    tx = new_transaction(None)  # type: ignore[arg-type]
    assert tx.goal == ""
    assert tx.phase == "intake"


def test_note_files_unique_ordered_and_phase_acting():
    tx = new_transaction("edit helpers")
    assert tx.phase == "intake"

    tx2 = note_files(tx, ["a.py", "b.py", "a.py", "  ", None, "b.py"])
    assert tx2.files == ["a.py", "b.py"]
    assert tx2.phase == "acting"
    # Immutable: original unchanged.
    assert tx.files == []
    assert tx.phase == "intake"

    tx3 = note_files(tx2, ["c.py", "a.py"])
    assert tx3.files == ["a.py", "b.py", "c.py"]
    assert tx3.phase == "acting"


def test_note_files_does_not_demote_later_phase():
    tx = replace(new_transaction("done-ish"), phase="verifying")
    tx2 = note_files(tx, ["z.py"])
    assert tx2.files == ["z.py"]
    assert tx2.phase == "verifying"


def test_note_verification_pass_ok_skipped_done():
    tx = note_files(new_transaction("verify me"), ["t.py"])
    for result in ("pass", "OK", " skipped ", "Pass"):
        out = note_verification(tx, result)
        assert out.phase == "done"
        assert out.verification == result.strip()


def test_note_verification_non_pass_is_verifying():
    tx = new_transaction("still checking")
    out = note_verification(tx, "fail: 1 test")
    assert out.phase == "verifying"
    assert out.verification == "fail: 1 test"

    empty = note_verification(tx, "  ")
    assert empty.phase == "verifying"
    assert empty.verification == ""


def test_as_dict_omits_empty_lists_and_strings():
    bare = new_transaction("")
    assert as_dict(bare) == {"phase": "intake"}

    tx = note_files(new_transaction("goal text"), ["a.py"])
    tx = replace(tx, plan="do the thing", constraints=["no bump"], invariants=[])
    d = as_dict(tx)
    assert d["goal"] == "goal text"
    assert d["files"] == ["a.py"]
    assert d["plan"] == "do the thing"
    assert d["constraints"] == ["no bump"]
    assert "invariants" not in d
    assert "verification" not in d
    assert d["phase"] == "acting"


def test_context_block_empty_when_only_goal():
    tx = new_transaction("only a goal")
    assert context_block(tx) == ""

    empty = new_transaction("")
    assert context_block(empty) == ""


def test_context_block_compact_markdown_with_extras():
    tx = note_files(new_transaction("ship it"), ["harness/task_transaction.py"])
    tx = replace(tx, plan="add module + tests", constraints=["no version bump"])
    tx = note_verification(tx, "pass")
    block = context_block(tx)
    assert block.startswith("## Task transaction")
    assert "goal: ship it" in block
    assert "phase: done" in block
    assert "plan: add module + tests" in block
    assert "- harness/task_transaction.py" in block
    assert "- no version bump" in block
    assert "verification: pass" in block


def test_helpers_tolerate_bad_tx():
    assert as_dict(None) == {}  # type: ignore[arg-type]
    assert context_block(None) == ""  # type: ignore[arg-type]
    recovered = note_files(None, ["a.py"])  # type: ignore[arg-type]
    assert isinstance(recovered, TaskTransaction)
    assert recovered.files == ["a.py"]
    assert recovered.phase == "acting"
