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
    # first foo read -> pointer; latest foo read -> full; bar -> untouched.
    # The two foo reads differ, so the pointer is enriched with a delta summary;
    # either way it names the elided path and is no longer the full stale copy.
    assert "earlier read of foo.py elided" in out[0]["content"]
    assert "FOO V1" not in out[0]["content"]
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


def _read(path, start=None, limit=None):
    from harness.pilot import PilotAction
    return PilotAction(kind="read_file", path=path, start_line=start, limit=limit)


def test_read_key_normalizes_aliases_and_keys_ranges(tmp_path):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path / "s"), repo=str(tmp_path))
    s = ConversationalSession(cfg)
    abs_path = str(tmp_path / "harness" / "x.py")
    # One file, three spellings: the workspace-relative label.
    assert s._read_elision_key(_read("harness/x.py")) == "harness/x.py"
    assert s._read_elision_key(_read("./harness/x.py")) == "harness/x.py"
    assert s._read_elision_key(_read(abs_path)) == "harness/x.py"
    # The ~150-line slices read_file recommends are keys of their own.
    assert s._read_elision_key(_read("harness/x.py", 1, 150)) == "harness/x.py:1+150"
    assert s._read_elision_key(_read(abs_path, 1, 150)) == "harness/x.py:1+150"
    assert s._read_elision_key(_read("harness/x.py", 151, 150)) == "harness/x.py:151+150"
    assert s._read_elision_key(_read("spill://abc")) == "spill://abc"


def test_repeated_identical_slice_is_elided_other_slices_kept(tmp_path):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path / "s"), repo=str(tmp_path))
    s = ConversationalSession(cfg)
    msgs = [
        {"role": "tool", "tool_call_id": "a", "content": "slice one v1", "_read_path": s._read_elision_key(_read("harness/x.py", 1, 150))},
        {"role": "tool", "tool_call_id": "b", "content": "slice two", "_read_path": s._read_elision_key(_read("harness/x.py", 151, 150))},
        {"role": "tool", "tool_call_id": "c", "content": "slice one v2", "_read_path": s._read_elision_key(_read(str(tmp_path / "harness/x.py"), 1, 150))},
    ]
    out = s._elide_stale_reads(msgs)
    assert out[0]["content"].startswith("[earlier read of harness/x.py:1+150")
    assert out[1]["content"] == "slice two"
    assert out[2]["content"] == "slice one v2"


def test_path_spelling_alone_is_not_reported_as_a_change(tmp_path):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path / "s"), repo=str(tmp_path))
    s = ConversationalSession(cfg)
    body = "\n".join(f"line_{i}" for i in range(20))
    msgs = [
        {"role": "tool", "tool_call_id": "a", "content": f"(read_file ./x.py returned)\n{body}", "_read_path": "x.py"},
        {"role": "tool", "tool_call_id": "b", "content": f"(read_file {tmp_path}/x.py returned)\n{body}", "_read_path": "x.py"},
    ]
    pointer = s._elide_stale_reads(msgs)[0]["content"]
    assert pointer.startswith("[earlier read of x.py")
    assert "changed since" not in pointer
