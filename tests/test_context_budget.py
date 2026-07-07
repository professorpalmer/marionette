import os
import tempfile
import pytest
from dataclasses import dataclass
from harness.context_budget import (
    BudgetConfig,
    generate_preview,
    spill_to_disk,
    maybe_persist_result,
    enforce_turn_budget,
    PERSISTED_OUTPUT_TAG,
)
from harness.conversation import ConversationalSession

# Offload gate floor: 3000 tokens ~= 12000 chars.
_GATE_FLOOR_CHARS = 12_500


@dataclass
class DummyConfig:
    repo: str = ""
    driver: str = "stub-oracle-v2"
    reach: str = "local"
    state_dir: str = ""
    swarm_adapter: str = ""


@dataclass
class DummyAction:
    kind: str
    path: str
    start_line: int = None
    limit: int = None


def test_generate_preview():
    content = "line1\nline2\nline3\nline4\nline5\n"
    preview, has_more = generate_preview(content, max_chars=15)
    assert preview == "line1\nline2\n"
    assert has_more is True

    preview, has_more = generate_preview("hello", max_chars=10)
    assert preview == "hello"
    assert has_more is False


def test_spill_to_disk():
    with tempfile.TemporaryDirectory() as tmpdir:
        content = "full content details go here"
        path = spill_to_disk(content, "res123", tmpdir)
        assert os.path.exists(path)
        assert path.endswith("pmharness-results/res123.txt")
        with open(path, "r", encoding="utf-8") as f:
            assert f.read() == content


def test_maybe_persist_result():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = BudgetConfig(max_result_chars=10, turn_budget_chars=50)

        # Small content
        small = "small"
        res = maybe_persist_result(small, "id1", tmpdir, config)
        assert res == small

        # Large content (above offload gate floor)
        large = "x" * _GATE_FLOOR_CHARS
        res = maybe_persist_result(large, "id2", tmpdir, config)
        assert PERSISTED_OUTPUT_TAG in res
        assert "pmharness-results/id2.txt" in res
        assert "Use read_file with start_line and limit to read specific sections" in res

        # File contents should match
        file_path = os.path.join(tmpdir, "pmharness-results", "id2.txt")
        with open(file_path, "r", encoding="utf-8") as f:
            assert f.read() == large


def test_maybe_persist_result_exception_fallback():
    config = BudgetConfig(max_result_chars=10, turn_budget_chars=50)
    content = "x" * _GATE_FLOOR_CHARS
    # Embedded NUL makes the path unwritable on every platform (Windows happily
    # creates "/nonexistent_directory_cannot_write/!" at the drive root).
    res = maybe_persist_result(content, "id123", "/nonexistent\0directory", config)
    assert "[Truncated: tool response was" in res
    assert "Full output could not be saved" in res


def test_enforce_turn_budget():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Use realistic larger budget sizes so the replacement messages (approx 250 chars)
        # do not trigger cascade spilling of everything.
        config = BudgetConfig(max_result_chars=200, turn_budget_chars=3000)
        messages = [
            {"role": "tool", "tool_call_id": "tc1", "content": "small content"},  # 13 chars
            {"role": "tool", "tool_call_id": "tc2", "content": "a" * 150},  # 150 chars
            {"role": "tool", "tool_call_id": "tc3", "content": "b" * _GATE_FLOOR_CHARS},
        ]
        # Total: 5163 chars (> 3000 turn_budget_chars)
        # tc3 is the largest and gets persisted first.
        enforce_turn_budget(messages, tmpdir, config)

        assert PERSISTED_OUTPUT_TAG in messages[2]["content"]
        assert "tc3" in messages[2]["content"]
        assert messages[0]["content"] == "small content"  # small was untouched


def test_enforce_turn_budget_under_budget():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = BudgetConfig(max_result_chars=20, turn_budget_chars=100)
        messages = [
            {"role": "tool", "tool_call_id": "tc1", "content": "small"},
            {"role": "tool", "tool_call_id": "tc2", "content": "medium"},
        ]
        original = [dict(m) for m in messages]
        enforce_turn_budget(messages, tmpdir, config)
        assert messages == original


def test_enforce_turn_budget_already_persisted_skipped():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = BudgetConfig(max_result_chars=200, turn_budget_chars=3000)
        # Total size exceeds budget, but msg0 is already persisted.
        messages = [
            {"role": "tool", "tool_call_id": "tc1", "content": f"{PERSISTED_OUTPUT_TAG} already saved"},
            {"role": "tool", "tool_call_id": "tc2", "content": "b" * _GATE_FLOOR_CHARS},
        ]
        enforce_turn_budget(messages, tmpdir, config)
        assert PERSISTED_OUTPUT_TAG in messages[0]["content"]
        assert "already saved" in messages[0]["content"]
        assert PERSISTED_OUTPUT_TAG in messages[1]["content"]


def test_read_file_offset_limit(tmp_path):
    # Create dummy session
    conf = DummyConfig(repo=str(tmp_path))
    session = ConversationalSession(conf)

    # Create dummy file with 10 lines
    file_content = "\n".join(f"Line {i}" for i in range(1, 11))
    fpath = tmp_path / "test.txt"
    fpath.write_text(file_content, encoding="utf-8")

    # Read small file, no offset/limit
    act1 = DummyAction(kind="read_file", path="test.txt")
    ok, status, val = session._do_read_file(act1)
    assert ok is True
    assert status == "success"
    assert val == file_content

    # Read with start_line and limit
    act2 = DummyAction(kind="read_file", path="test.txt", start_line=3, limit=4)
    ok, status, val = session._do_read_file(act2)
    assert ok is True
    assert status == "success"
    # Lines 3, 4, 5, 6:
    expected = "[lines 3-6 of 10]\nLine 3\nLine 4\nLine 5\nLine 6\n"
    assert val == expected

    # Read with start_line only (no limit)
    act3 = DummyAction(kind="read_file", path="test.txt", start_line=8)
    ok, status, val = session._do_read_file(act3)
    assert ok is True
    assert status == "success"
    assert val == "[lines 8-10 of 10]\nLine 8\nLine 9\nLine 10"


def test_read_file_large_file_guard(tmp_path):
    conf = DummyConfig(repo=str(tmp_path))
    session = ConversationalSession(conf)

    # Create large file: 2100 lines (exceeds 2000 lines guard)
    large_lines = [f"This is line {i}" for i in range(1, 2101)]
    file_content = "\n".join(large_lines)
    fpath = tmp_path / "large.txt"
    fpath.write_text(file_content, encoding="utf-8")

    # Read large file, no range specified -> guard triggers
    act = DummyAction(kind="read_file", path="large.txt")
    ok, status, val = session._do_read_file(act)
    assert ok is True
    assert status == "success"
    assert "[file is large (2100 lines); re-read with start_line and limit to see specific sections]" in val
    # Check that it returns a head slice
    assert "This is line 1\n" in val
    assert "This is line 100\n" in val
    assert "This is line 101\n" not in val

    # Read large file WITH range specified -> guard does NOT trigger
    act_ranged = DummyAction(kind="read_file", path="large.txt", start_line=105, limit=5)
    ok, status, val = session._do_read_file(act_ranged)
    assert ok is True
    assert status == "success"
    assert "This is line 105" in val
    assert "[lines 105-109 of 2100]" in val


def test_regression_simulated_bug(tmp_path):
    conf = DummyConfig(repo=str(tmp_path))
    session = ConversationalSession(conf)

    assert session.context_budget_config.turn_budget_chars == 48000

    large_block = "x" * 33000
    for i in range(6):
        act = DummyAction(kind="read_file", path=f"file_{i}.txt")
        session._append_action_result(act, f"aid_{i}", large_block, is_native=True)

    tool_msgs = [m for m in session._history if m.get("role") == "tool"]
    assert len(tool_msgs) == 6

    from harness.context_budget import enforce_turn_budget
    enforce_turn_budget(
        tool_messages=tool_msgs,
        state_dir=session._state_dir_or_tempdir,
        config=session.context_budget_config,
    )

    total_chars = sum(len(m["content"]) for m in tool_msgs)
    assert total_chars <= session.context_budget_config.turn_budget_chars
