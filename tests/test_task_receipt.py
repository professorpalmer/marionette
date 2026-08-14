"""Hermetic tests for compact MICRO/STANDARD task receipts."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from harness.task_receipt import (
    TaskReceipt,
    append_receipt,
    build_receipt,
    compute_patch_hash,
    git_branch,
    load_receipts,
    prompt_hash,
)


def test_prompt_hash_stable():
    assert prompt_hash("hello") == prompt_hash("hello")
    expected = hashlib.sha256(b"hello").hexdigest()[:16]
    assert prompt_hash("hello") == expected
    assert len(prompt_hash("hello")) == 16


def test_empty_message_hash_still_hex():
    digest = prompt_hash("")
    assert len(digest) == 16
    assert all(c in "0123456789abcdef" for c in digest)
    assert digest == hashlib.sha256(b"").hexdigest()[:16]


def test_git_branch_on_non_repo_returns_empty(tmp_path: Path):
    assert git_branch(str(tmp_path)) == ""
    assert git_branch(str(tmp_path / "missing")) == ""
    assert git_branch("") == ""


def test_build_receipt_omits_empty_values():
    rec = build_receipt(
        task_id="t1",
        profile="MICRO",
        model="",
        adapter=None,
        changed_files=[],
        verification="pass",
        created_at="2026-08-13T00:00:00+00:00",
    )
    assert rec["task_id"] == "t1"
    assert rec["profile"] == "MICRO"
    assert rec["verification"] == "pass"
    assert "model" not in rec
    assert "adapter" not in rec
    assert "changed_files" not in rec
    assert rec["created_at"] == "2026-08-13T00:00:00+00:00"


def test_append_load_roundtrip(tmp_path: Path):
    state = tmp_path / "state"
    first = build_receipt(
        task_id="a",
        profile="MICRO",
        prompt_hash=prompt_hash("msg-a"),
        verification="pass",
        created_at="2026-08-13T01:00:00+00:00",
    )
    second = build_receipt(
        task_id="b",
        profile="STANDARD",
        prompt_hash=prompt_hash("msg-b"),
        verification="skipped",
        created_at="2026-08-13T02:00:00+00:00",
    )
    append_receipt(str(state), first)
    append_receipt(str(state), second)

    loaded = load_receipts(str(state), limit=20)
    assert len(loaded) == 2
    assert loaded[0]["task_id"] == "a"
    assert loaded[1]["task_id"] == "b"
    assert loaded[0]["prompt_hash"] == prompt_hash("msg-a")

    # Dataclass path also appends.
    append_receipt(
        str(state),
        TaskReceipt(
            task_id="c",
            profile="MICRO",
            created_at="2026-08-13T03:00:00+00:00",
        ),
    )
    loaded3 = load_receipts(str(state), limit=2)
    assert len(loaded3) == 2
    assert loaded3[0]["task_id"] == "b"
    assert loaded3[1]["task_id"] == "c"


def test_append_receipt_never_raises(tmp_path: Path):
    # Invalid state_dir (file, not directory) should not raise.
    bad = tmp_path / "not-a-dir"
    bad.write_text("x", encoding="utf-8")
    append_receipt(str(bad), {"task_id": "x"})
    assert load_receipts(str(tmp_path / "missing"), limit=5) == []


def test_compute_patch_hash_empty_and_stable(tmp_path: Path):
    assert compute_patch_hash(None) == ""
    assert compute_patch_hash([]) == ""
    f = tmp_path / "a.py"
    f.write_text("print(1)\n", encoding="utf-8")
    h1 = compute_patch_hash([str(f)])
    h2 = compute_patch_hash([str(f)])
    assert h1 == h2
    assert len(h1) == 64
    assert all(c in "0123456789abcdef" for c in h1)
