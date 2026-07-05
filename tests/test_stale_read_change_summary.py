"""Superseded reads collapse to a DELTA summary, not a content-losing pointer.

When an earlier whole-file read of a path is elided because a newer read of the
same path exists, the pointer is enriched with a one-line-per-region delta
(via harness.change_summary.summarize_change) describing WHAT changed between the
earlier and the newer, kept read -- so the model keeps that knowledge instead of
discarding it. Every enrichment is fully guarded: if the file text cannot be
extracted, summarize_change raises, or the two reads are identical, the pointer
falls back to EXACTLY today's bare pointer string so behavior never regresses.

Hermetic: exercises _elide_stale_reads directly.
"""
import tempfile

from harness.change_summary import summarize_change
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession

BARE = "elided to save tokens"


def _s():
    return ConversationalSession(HarnessConfig(state_dir=tempfile.mkdtemp()))


def test_changed_reads_produce_region_delta_pointer():
    old = "line1\nline2\nline3\n"
    new = "line1\nline2 CHANGED\nline3\nline4\n"
    hist = [
        {"role": "tool", "tool_call_id": "1", "content": old, "_read_path": "f.py"},
        {"role": "tool", "tool_call_id": "2", "content": new, "_read_path": "f.py"},
    ]
    out = _s()._elide_stale_reads(hist)
    ptr = out[0]["content"]
    # Pointer names the path and carries the region-delta summary...
    assert "f.py" in ptr
    assert "changed since:" in ptr
    assert summarize_change(old, new) in ptr
    assert BARE not in ptr
    # ...and the newer read is kept intact.
    assert out[1]["content"] == new
    # Internal tag never leaks.
    assert "_read_path" not in out[0]
    assert out[0]["tool_call_id"] == "1"


def test_identical_reads_fall_back_to_plain_pointer():
    same = "same\ncontent\nexactly\n"
    hist = [
        {"role": "tool", "tool_call_id": "1", "content": same, "_read_path": "g.py"},
        {"role": "tool", "tool_call_id": "2", "content": same, "_read_path": "g.py"},
    ]
    out = _s()._elide_stale_reads(hist)
    ptr = out[0]["content"]
    assert BARE in ptr
    assert "changed since:" not in ptr


def test_malformed_message_falls_back_without_raising():
    # Earlier read carries a non-extractable content (an int) -> bare pointer.
    hist = [
        {"role": "tool", "tool_call_id": "1", "content": 12345, "_read_path": "h.py"},
        {"role": "tool", "tool_call_id": "2", "content": "real text\n", "_read_path": "h.py"},
    ]
    out = _s()._elide_stale_reads(hist)
    ptr = out[0]["content"]
    assert BARE in ptr
    assert "changed since:" not in ptr
    # Newer read untouched, tag stripped.
    assert out[1]["content"] == "real text\n"
    assert "_read_path" not in out[1]


def test_unrelated_single_read_untouched():
    hist = [
        {"role": "tool", "tool_call_id": "x", "content": "solo file text\n", "_read_path": "solo.py"},
        {"role": "assistant", "content": "ok"},
    ]
    out = _s()._elide_stale_reads(hist)
    assert out[0]["content"] == "solo file text\n"
    assert "_read_path" not in out[0]
    assert out[1] == {"role": "assistant", "content": "ok"}


def test_content_block_list_is_extracted():
    # Content as a list of provider blocks is extracted and summarized.
    old_blocks = [{"type": "text", "text": "a\nb\nc\n"}]
    new_blocks = [{"type": "text", "text": "a\nb CHANGED\nc\n"}]
    hist = [
        {"role": "tool", "tool_call_id": "1", "content": old_blocks, "_read_path": "b.py"},
        {"role": "tool", "tool_call_id": "2", "content": new_blocks, "_read_path": "b.py"},
    ]
    out = _s()._elide_stale_reads(hist)
    ptr = out[0]["content"]
    assert "changed since:" in ptr
    assert summarize_change("a\nb\nc\n", "a\nb CHANGED\nc\n") in ptr
