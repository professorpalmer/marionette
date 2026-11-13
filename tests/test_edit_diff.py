from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from harness import edit_diff as ed
from harness.edit_diff import edit_diff, read_text_for_diff
from harness.pilot import PilotAction
from harness.send_loop_phases import dispatch_local_action


def test_counts_and_patch_for_an_edit(tmp_path):
    path = tmp_path / "calc.py"
    path.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    before = read_text_for_diff(str(path))
    path.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    diff = edit_diff(before, str(path), "src/calc.py")
    assert diff["added"] == 1 and diff["removed"] == 1
    assert "-    return a - b\n" in diff["patch"] and "+    return a + b\n" in diff["patch"]
    assert diff["patch"].startswith("--- a/src/calc.py\n+++ b/src/calc.py\n")
    assert diff["truncated"] is False


def test_a_crlf_file_diffs_as_lf_lines(tmp_path):
    path = tmp_path / "calc.py"
    path.write_bytes(b"def add(a, b):\r\n    return a - b\r\n")
    before = read_text_for_diff(str(path))
    path.write_bytes(b"def add(a, b):\r\n    return a + b\r\n")
    diff = edit_diff(before, str(path), "calc.py")
    assert (diff["added"], diff["removed"]) == (1, 1)
    assert "\r" not in diff["patch"]
    assert "+    return a + b\n" in diff["patch"]


def test_a_new_file_is_all_added(tmp_path):
    path = tmp_path / "new.py"
    before = read_text_for_diff(str(path))
    assert before == ""
    path.write_text("a\nb\n", encoding="utf-8")
    diff = edit_diff(before, str(path), "new.py")
    assert (diff["added"], diff["removed"]) == (2, 0)
    assert diff["patch"].startswith("--- /dev/null\n")


def test_binary_and_large_files_get_no_diff(tmp_path, monkeypatch):
    blob = tmp_path / "x.bin"
    blob.write_bytes(b"\0\1\2")
    assert read_text_for_diff(str(blob)) is None
    assert edit_diff(None, str(blob), "x.bin") is None
    monkeypatch.setattr(ed, "MAX_DIFF_SOURCE_BYTES", 4)
    big = tmp_path / "big.txt"
    big.write_text("0123456789", encoding="utf-8")
    assert read_text_for_diff(str(big)) is None


def test_long_patch_is_cut_but_counts_stay_exact(tmp_path, monkeypatch):
    monkeypatch.setattr(ed, "MAX_PATCH_LINES", 10)
    path = tmp_path / "f.txt"
    path.write_text("", encoding="utf-8")
    path.write_text("".join(f"{i}\n" for i in range(50)), encoding="utf-8")
    diff = edit_diff("", str(path), "f.txt")
    assert diff["added"] == 50 and diff["truncated"] is True
    assert len(diff["patch"].splitlines()) == 10


def test_edit_file_action_result_carries_the_diff(tmp_path):
    target = tmp_path / "b.py"
    target.write_text("x = 1\n", encoding="utf-8")

    def do_edit(act, write=True):
        if write:
            target.write_text("x = 2\n", encoding="utf-8")
        return True, "ok", "1 replacement"

    session = SimpleNamespace(
        config=SimpleNamespace(repo=str(tmp_path)),
        harness_session_id="sess",
        _checkpoints=SimpleNamespace(snapshot=MagicMock(return_value=None)),
        _do_edit_file=do_edit,
        _append_action_result=MagicMock(),
    )
    act = PilotAction(kind="edit_file", path="b.py", old_str="1", new_str="2")
    events = list(dispatch_local_action(session, act, "e1", True, []))
    diff = events[0].data["diff"]
    assert (diff["added"], diff["removed"]) == (1, 1)
    assert "+x = 2\n" in diff["patch"]
