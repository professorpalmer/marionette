"""Tests for the deterministic large-output limiting helpers added to
harness/context_budget.py: head+tail preview, byte-aware sizing, dedupe
hashing, and multibyte safety. Import-pure, stdlib-only, deterministic."""

import os

from harness.context_budget import (
    BudgetConfig,
    byte_size,
    truncate_bytes,
    content_hash,
    generate_preview,
    spill_to_disk,
    maybe_persist_result,
    build_persisted_message,
    PERSISTED_OUTPUT_TAG,
)


def test_head_tail_preview_shows_beginning_and_end():
    lines = [f"line {i}" for i in range(1000)]
    content = "\n".join(lines) + "\nFINAL ERROR AT END"
    preview, has_more = generate_preview(content, max_chars=300, head_tail=True)
    assert has_more is True
    assert "line 0" in preview
    assert "FINAL ERROR AT END" in preview
    assert "omitted" in preview
    assert len(preview) < len(content)


def test_head_only_preview_is_default_and_unchanged():
    content = "x" * 5000
    preview, has_more = generate_preview(content, max_chars=1000)
    assert has_more is True
    # Default head-only mode keeps the leading window, not the tail.
    assert preview == content[:1000]


def test_short_content_returns_unchanged_in_both_modes():
    content = "short output"
    assert generate_preview(content, max_chars=1000) == (content, False)
    assert generate_preview(content, max_chars=1000, head_tail=True) == (content, False)


def test_byte_size_multibyte():
    assert byte_size("abc") == 3
    # Each of these characters is multibyte in UTF-8.
    assert byte_size("\u00e9") == 2       # e with acute accent
    assert byte_size("\u4e2d") == 3       # CJK char
    assert byte_size("\U0001f600") == 4   # emoji-range codepoint (not printed)


def test_truncate_bytes_never_splits_multibyte():
    content = "\u4e2d" * 100  # 300 bytes total, 3 bytes each
    out = truncate_bytes(content, 7)  # 7 bytes -> 2 whole chars (6 bytes)
    assert out == "\u4e2d\u4e2d"
    assert byte_size(out) <= 7
    # Must remain valid decodable text (no partial character).
    out.encode("utf-8").decode("utf-8")


def test_truncate_bytes_passthrough_and_zero():
    assert truncate_bytes("hello", 100) == "hello"
    assert truncate_bytes("hello", 0) == ""


def test_content_hash_is_stable_and_dedupes():
    a = content_hash("identical large output")
    b = content_hash("identical large output")
    c = content_hash("different output")
    assert a == b
    assert a != c
    assert len(a) == 12


def test_spill_dedupe_writes_single_file(tmp_path):
    content = "big content " * 500
    p1 = spill_to_disk(content, "result_a", str(tmp_path), dedupe=True)
    p2 = spill_to_disk(content, "result_a", str(tmp_path), dedupe=True)
    assert p1 == p2
    assert content_hash(content) in os.path.basename(p1)
    results_dir = os.path.join(str(tmp_path), "pmharness-results")
    assert len(os.listdir(results_dir)) == 1


def test_spill_no_dedupe_backward_compatible(tmp_path):
    content = "big content " * 500
    p = spill_to_disk(content, "result_b", str(tmp_path), dedupe=False)
    assert os.path.basename(p) == "result_b.txt"


def test_maybe_persist_smart_default_for_command_results(tmp_path):
    content = "\n".join(f"log {i}" for i in range(2000)) + "\nboom: failure"
    config = BudgetConfig(max_result_chars=100, preview_chars=200)
    msg = maybe_persist_result(
        content=content,
        result_id="command_run_1",
        state_dir=str(tmp_path),
        config=config,
    )
    assert PERSISTED_OUTPUT_TAG in msg
    # Command-like id -> head+tail default -> trailing error visible.
    assert "boom: failure" in msg
    assert "omitted" in msg


def test_maybe_persist_default_head_only_for_generic_results(tmp_path):
    content = "\n".join(f"log {i}" for i in range(2000)) + "\ntail_marker"
    config = BudgetConfig(max_result_chars=100, preview_chars=200)
    msg = maybe_persist_result(
        content=content,
        result_id="read_file_1",
        state_dir=str(tmp_path),
        config=config,
    )
    assert PERSISTED_OUTPUT_TAG in msg
    assert "tail_marker" not in msg


def test_maybe_persist_multibyte_preview_is_valid(tmp_path):
    content = "\u4e2d\u6587\u5185\u5bb9 " * 3000
    config = BudgetConfig(max_result_chars=50, preview_chars=101)
    msg = maybe_persist_result(
        content=content,
        result_id="cmd_multibyte",
        state_dir=str(tmp_path),
        config=config,
        head_tail=True,
    )
    # The message must round-trip through UTF-8 with no partial characters.
    msg.encode("utf-8").decode("utf-8")
    assert PERSISTED_OUTPUT_TAG in msg


def test_build_persisted_message_labels_head_tail():
    preview_ht = "start\n... [omitted 100 characters] ...\nend"
    msg = build_persisted_message(preview_ht, True, 5000, "/tmp/x.txt")
    assert "head and tail" in msg
    preview_head = "just the head"
    msg2 = build_persisted_message(preview_head, True, 5000, "/tmp/x.txt")
    assert "first" in msg2
