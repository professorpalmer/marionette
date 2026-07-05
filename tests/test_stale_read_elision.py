"""Superseded whole-file reads are elided from the outgoing prompt.

Token minimization: when the model reads the same file more than once, the older
full copies keep costing input tokens every turn. The pre-send pass replaces
every earlier read of a path with a one-line pointer and keeps only the latest,
without mutating stored history. Ranged reads and single reads are untouched, and
the internal _read_path tag never leaks to the provider.

Hermetic: exercises _elide_stale_reads directly.
"""
import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def _s():
    return ConversationalSession(HarnessConfig(state_dir=tempfile.mkdtemp()))


def _hist():
    return [
        {"role": "tool", "tool_call_id": "1", "content": "FOO V1" * 100, "_read_path": "foo.py"},
        {"role": "assistant", "content": "ok"},
        {"role": "tool", "tool_call_id": "2", "content": "BAR" * 100, "_read_path": "bar.py"},
        {"role": "tool", "tool_call_id": "3", "content": "FOO V2 LATEST" * 100, "_read_path": "foo.py"},
    ]


def test_earlier_read_is_elided_latest_kept():
    out = _s()._elide_stale_reads(_hist())
    # first foo read -> pointer; latest foo read -> full; bar -> untouched
    assert "elided to save tokens" in out[0]["content"]
    assert "FOO V2 LATEST" in out[3]["content"]
    assert out[2]["content"].startswith("BAR")


def test_pairing_keys_preserved_and_tag_stripped():
    out = _s()._elide_stale_reads(_hist())
    for m in out:
        assert "_read_path" not in m, "internal tag must not reach the provider"
    # tool_call_id preserved so tool-result pairing stays valid
    assert out[0]["tool_call_id"] == "1"
    assert out[3]["tool_call_id"] == "3"


def test_single_read_untouched():
    hist = [{"role": "tool", "tool_call_id": "x", "content": "ONLY ONCE" * 50, "_read_path": "solo.py"}]
    out = _s()._elide_stale_reads(hist)
    assert "ONLY ONCE" in out[0]["content"]  # not elided
    assert "_read_path" not in out[0]


def test_no_tagged_reads_is_noop():
    hist = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    out = _s()._elide_stale_reads(hist)
    assert out == hist


def test_bytes_saved_on_repeated_reads():
    before = sum(len(m["content"]) for m in _hist())
    out = _s()._elide_stale_reads(_hist())
    after = sum(len(m["content"]) for m in out)
    assert after < before  # the elision actually reduced payload size
