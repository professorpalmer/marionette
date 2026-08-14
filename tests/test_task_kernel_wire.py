"""Last-mile wiring: receipts, task transaction, and soft verify in the send loop."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from harness.send_loop_phases import (
    _yield_task_profile_escalation,
    begin_turn_task_kernel,
    drain_idle_turn,
    finalize_assistant_turn,
    maybe_soft_verify_nudge,
    persist_turn_receipt,
)
from harness.task_profile import MICRO, STANDARD
from harness.task_receipt import JSONL_FILENAME, load_receipts, prompt_hash
from harness.task_transaction import as_dict, new_transaction, note_files
from harness.tool_requirement import SoftToolRequirement


def test_begin_turn_task_kernel_starts_transaction():
    session = SimpleNamespace()
    begin_turn_task_kernel(session, "  fix the typo in README  ")
    assert session._turn_ran_command is False
    assert session._verify_remind_count == 0
    assert session._turn_user_message == "  fix the typo in README  "
    assert session._turn_verification == ""
    assert as_dict(session._task_tx)["goal"] == "fix the typo in README"
    assert as_dict(session._task_tx)["phase"] == "intake"


def test_yield_task_profile_escalation_notes_files():
    session = SimpleNamespace(
        _task_tx=new_transaction("edit helpers"),
        _maybe_escalate_task_profile=lambda **_k: None,
    )
    events = list(_yield_task_profile_escalation(session, ["a.py", "a.py", "b.py"]))
    assert events == []
    assert as_dict(session._task_tx)["files"] == ["a.py", "b.py"]
    assert as_dict(session._task_tx)["phase"] == "acting"


def test_persist_turn_receipt_writes_jsonl(tmp_path: Path):
    changed = tmp_path / "edited.py"
    changed.write_text("x = 1\n", encoding="utf-8")
    session = SimpleNamespace(
        config=SimpleNamespace(
            state_dir=str(tmp_path),
            repo=str(tmp_path),
            driver="stub-oracle-v2",
        ),
        harness_session_id="sess-1",
        _task_profile=MICRO,
        _task_profile_source="heuristic",
        _task_profile_escalated_from=None,
        _task_tx=note_files(new_transaction("typo in README"), [str(changed)]),
        _turn_ran_command=True,
        _turn_verification="pass",
        _turn_user_message="typo in README",
        pilot=SimpleNamespace(model="stub"),
    )
    persist_turn_receipt(session, "typo in README")
    rows = load_receipts(str(tmp_path), limit=5)
    assert len(rows) == 1
    rec = rows[0]
    assert rec["task_id"] == "sess-1"
    assert rec["profile"] == MICRO
    assert rec["prompt_hash"] == prompt_hash("typo in README")
    assert rec["verification"] == "pass"
    assert rec["changed_files"] == [str(changed)]
    assert rec["adapter"] == "stub-oracle-v2"
    assert (tmp_path / JSONL_FILENAME).is_file()


def test_persist_turn_receipt_never_raises_without_state_dir():
    persist_turn_receipt(SimpleNamespace(), "hi")


def test_maybe_soft_verify_nudge_reminds_then_continues():
    session = SimpleNamespace(
        _history=[],
        _task_profile=MICRO,
        _task_tx=note_files(new_transaction("edit"), ["a.py"]),
        _turn_ran_command=False,
        _verify_remind_count=0,
    )
    gen = maybe_soft_verify_nudge(session)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        nudged = stop.value
    assert nudged is True
    assert session._verify_remind_count == 1
    assert session._history[-1]["role"] == "user"
    assert session._history[-1]["content"] == SoftToolRequirement.remind_message()


def test_maybe_soft_verify_nudge_skips_when_command_ran():
    session = SimpleNamespace(
        _history=[],
        _task_profile=STANDARD,
        _task_tx=note_files(new_transaction("edit"), ["a.py"]),
        _turn_ran_command=True,
        _verify_remind_count=0,
    )
    gen = maybe_soft_verify_nudge(session)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        nudged = stop.value
    assert nudged is False
    assert session._history == []


def test_drain_idle_turn_nudges_unverified_micro_edits():
    session = SimpleNamespace(
        drain_steer=lambda: [],
        _history=[],
        _steer_pending=False,
        _next_queued_needs_driver_swap=lambda: False,
        _pop_next_prompt=lambda: None,
        _submit_housekeeping=lambda *_a, **_k: None,
        _maybe_ingest="ingest",
        _task_profile=MICRO,
        _task_tx=note_files(new_transaction("typo"), ["README.md"]),
        _turn_ran_command=False,
        _verify_remind_count=0,
        config=SimpleNamespace(state_dir="", repo=""),
    )
    events = []
    gen = drain_idle_turn(
        session,
        user_message="typo",
        step=0,
        swarms=0,
        turn_prose=[],
        turn_findings=[],
    )
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        disposition, _msg = stop.value
    assert disposition == "continue"
    assert events == []
    assert session._verify_remind_count == 1


def test_finalize_assistant_turn_emits_done_and_persists(tmp_path: Path):
    submitted = []
    session = SimpleNamespace(
        config=SimpleNamespace(state_dir=str(tmp_path), repo="", driver=""),
        harness_session_id="s",
        _task_profile=MICRO,
        _task_profile_source="heuristic",
        _task_profile_escalated_from=None,
        _task_tx=new_transaction("hi"),
        _turn_ran_command=False,
        _turn_verification="",
        _turn_user_message="hi",
        _submit_housekeeping=lambda fn, *a: submitted.append((fn, a)),
        _maybe_ingest="ingest",
        pilot=SimpleNamespace(model=""),
    )
    events = list(finalize_assistant_turn(
        session,
        user_message="hi",
        step=1,
        swarms=0,
        turn_prose=["p"],
        turn_findings=[],
        extra={"stagnation_halt": True},
    ))
    assert events[0].kind == "assistant_done"
    assert events[0].data["turns"] == 2
    assert events[0].data["stagnation_halt"] is True
    assert submitted == [("ingest", ("hi", ["p"], []))]
    rows = load_receipts(str(tmp_path), limit=1)
    assert rows[0]["task_id"] == "s"
    assert rows[0]["profile"] == MICRO
