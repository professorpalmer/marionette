"""Tests for the turn-context journal (harness/turn_context.py)."""
import json
import os

from harness.turn_context import (
    JOURNAL_FILENAME,
    context_at,
    record_turn_context,
    turn_context_enabled,
)


def test_record_and_read_back_across_turns(tmp_path):
    state = str(tmp_path)
    for turn in (1, 2, 3):
        record_turn_context(state, "s1", turn, repo=str(tmp_path))

    for turn in (1, 2, 3):
        record = context_at(state, "s1", turn)
        assert record is not None
        assert record["turn"] == turn
        assert record["session_id"] == "s1"
        assert "env" in record and "check_specs_hash" in record


def test_newest_record_wins_per_turn(tmp_path):
    state = str(tmp_path)
    record_turn_context(state, "s1", 2, repo=str(tmp_path))
    # Re-record the same turn with a toggle flipped; newest must win.
    os.environ["HARNESS_HASH_EDIT"] = "1"
    try:
        record_turn_context(state, "s1", 2, repo=str(tmp_path))
    finally:
        os.environ.pop("HARNESS_HASH_EDIT", None)
    record = context_at(state, "s1", 2)
    assert record is not None
    assert record["env"]["HARNESS_HASH_EDIT"] == "1"


def test_missing_journal_yields_none(tmp_path):
    assert context_at(str(tmp_path), "s1", 1) is None
    assert context_at("", "s1", 1) is None
    assert context_at(str(tmp_path), "s1", 0) is None


def test_malformed_lines_skipped(tmp_path):
    state = str(tmp_path)
    record_turn_context(state, "s1", 1, repo=str(tmp_path))
    path = os.path.join(state, JOURNAL_FILENAME)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{not json}\n")
        fh.write("[1,2,3]\n")
    record = context_at(state, "s1", 1)
    assert record is not None
    assert record["turn"] == 1


def test_session_isolation(tmp_path):
    state = str(tmp_path)
    record_turn_context(state, "s1", 1, repo=str(tmp_path))
    assert context_at(state, "s2", 1) is None


def test_check_specs_hash_changes_with_specs(tmp_path):
    repo = tmp_path / "repo"
    checks_dir = repo / ".marionette" / "checks"
    checks_dir.mkdir(parents=True)
    state = str(tmp_path / "state")

    record_turn_context(state, "s1", 1, repo=str(repo))
    empty_hash = context_at(state, "s1", 1)["check_specs_hash"]
    assert empty_hash == ""

    (checks_dir / "c.json").write_text(
        json.dumps({"post": [{"id": "f", "kind": "file", "path": "x.txt", "exists": True}]}),
        encoding="utf-8",
    )
    record_turn_context(state, "s1", 2, repo=str(repo))
    populated = context_at(state, "s1", 2)
    assert populated["check_specs_hash"] != ""
    assert populated["check_spec_count"] == 1


def test_kill_switch_disables_recording(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_TURN_CONTEXT", "0")
    assert turn_context_enabled() is False
    state = str(tmp_path)
    record_turn_context(state, "s1", 1, repo=str(tmp_path))
    assert not os.path.exists(os.path.join(state, JOURNAL_FILENAME))


def test_recording_failure_is_swallowed(tmp_path):
    # A state_dir that collides with an existing FILE cannot take a journal;
    # the recorder must swallow the failure.
    blocker = tmp_path / "blocked"
    blocker.write_text("occupied", encoding="utf-8")
    record_turn_context(str(blocker), "s1", 1, repo=str(tmp_path))
