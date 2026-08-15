"""Hermetic tests for compact MICRO/STANDARD task receipts."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from harness.task_receipt import (
    JSONL_FILENAME,
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


def test_concurrent_append_receipts_are_intact_jsonl(tmp_path: Path):
    """Barrier-synced threads must each land as one intact JSON object.

    Unlocked Windows ``open(..., "a")`` can drop whole lines (CI 3.11 lost
    2/8). Each line is larger than a typical 8KiB stdio buffer so a torn
    write would fail json.loads or drop a task_id. load_receipts skips
    malformed lines, so the raw JSONL is checked as well.
    """
    n = 8
    # Optional field only — receipt schema is unchanged.
    oversized_repo = "R" * 9000
    state = tmp_path / "state"
    barrier = threading.Barrier(n)
    errors = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            append_receipt(
                str(state),
                {
                    "task_id": "concurrent-{0}".format(index),
                    "profile": "MICRO",
                    "repo": oversized_repo,
                    "created_at": "2026-08-15T00:00:00+00:00",
                },
            )
        except Exception as exc:  # pragma: no cover - failure surfaces below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    loaded = load_receipts(str(state), limit=n)
    expected_ids = {"concurrent-{0}".format(i) for i in range(n)}
    assert {row["task_id"] for row in loaded} == expected_ids
    assert len(loaded) == n
    for row in loaded:
        assert isinstance(row, dict)
        assert row["repo"] == oversized_repo

    raw_path = state / JSONL_FILENAME
    raw_lines = [
        line for line in raw_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(raw_lines) == n
    parsed_ids = []
    for line in raw_lines:
        rec = json.loads(line)
        assert isinstance(rec, dict)
        parsed_ids.append(rec["task_id"])
    assert set(parsed_ids) == expected_ids


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
