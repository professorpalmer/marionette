"""Tests for the persistent eval history (harness/eval_history.py)."""
import os

from harness.eval_history import (
    DB_FILENAME,
    eval_history_enabled,
    eval_history_payload,
    record_eval_results,
    summarize_eval_history,
)


def _results(passed=True, failed=1):
    rows = [
        {"id": "check-pass", "phase": "post", "passed": True, "on_fail": "warn"},
    ]
    for i in range(failed):
        rows.append(
            {"id": f"check-fail-{i}", "phase": "post", "passed": False, "on_fail": "failed"}
        )
    return rows


def test_record_and_summarize_round_trip(tmp_path):
    state = str(tmp_path)
    record_eval_results(state, "job_1", "declarative_check", _results(failed=2))
    recorded, failed = summarize_eval_history(state)
    assert recorded == 3
    assert failed == 2


def test_session_scoping(tmp_path):
    state = str(tmp_path)
    record_eval_results(state, "job_a", "declarative_check", _results(failed=1))
    record_eval_results(state, "job_b", "declarative_check", _results(failed=0))
    recorded_a, failed_a = summarize_eval_history(state, "job_a")
    assert (recorded_a, failed_a) == (2, 1)
    recorded_all, failed_all = summarize_eval_history(state)
    assert (recorded_all, failed_all) == (3, 1)


def test_payload_fields(tmp_path):
    state = str(tmp_path)
    assert eval_history_payload(state) == {"evals_recorded": 0, "evals_failed": 0}
    record_eval_results(state, "job_1", "declarative_check", _results(failed=1))
    payload = eval_history_payload(state)
    assert payload["evals_recorded"] == 2
    assert payload["evals_failed"] == 1


def test_rows_without_id_are_skipped(tmp_path):
    state = str(tmp_path)
    record_eval_results(
        state, "job_1", "declarative_check",
        [{"passed": False}, {"id": "", "passed": False}, "not a dict"],
    )
    assert summarize_eval_history(state) == (0, 0)


def test_failed_write_does_not_raise(tmp_path):
    # A state_dir path that is an existing FILE cannot host the db.
    blocker = tmp_path / "blocked"
    blocker.write_text("occupied", encoding="utf-8")
    record_eval_results(str(blocker), "job_1", "declarative_check", _results())
    assert summarize_eval_history(str(blocker)) == (0, 0)


def test_kill_switch_disables_recording(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_EVAL_HISTORY", "0")
    assert eval_history_enabled() is False
    state = str(tmp_path)
    record_eval_results(state, "job_1", "declarative_check", _results())
    assert not os.path.exists(os.path.join(state, DB_FILENAME))


def test_empty_inputs_are_noops(tmp_path):
    record_eval_results("", "job_1", "declarative_check", _results())
    record_eval_results(str(tmp_path), "job_1", "declarative_check", [])
    assert summarize_eval_history(str(tmp_path)) == (0, 0)
