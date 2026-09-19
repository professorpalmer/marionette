from __future__ import annotations

from types import SimpleNamespace

from harness.pilot import PilotAction
from harness.runaway_guard import (
    PLUGIN_SOURCE,
    SIGNAL_ABAB,
    SIGNAL_POLLING,
    SIGNAL_SAME_ERROR,
    SIGNAL_UNCHANGED,
    error_family,
    note_runaway_and_maybe_steer,
    observe_step,
    reset_runaway_state,
    take_preferred_steer,
)


def _session():
    return SimpleNamespace(_runaway_guard=None)


def _cmd(command="echo hi"):
    return PilotAction(kind="run_command", command=command, arguments={"command": command})


def _wait(job="job_1"):
    return PilotAction(kind="wait", arguments={"job_id": job})


def test_same_error_family_steers_once_at_three():
    session = _session()
    act = _cmd("python missing.py")
    err = "error: ModuleNotFoundError: missing"
    assert observe_step(session, "run_command", act, err, True) == []
    assert observe_step(session, "run_command", act, err, True) == []
    third = observe_step(session, "run_command", act, err, True)
    assert SIGNAL_SAME_ERROR in third
    first = take_preferred_steer(session, third)
    assert first is not None
    assert first[0] == SIGNAL_SAME_ERROR
    assert PLUGIN_SOURCE in first[1]
    assert "do not save this reminder" in first[1].lower()
    assert take_preferred_steer(session, third) is None


def test_exact_action_is_not_a_steer_candidate():
    session = _session()
    act = _cmd("echo hi")
    observe_step(session, "run_command", act, "ok", False)
    observe_step(session, "run_command", act, "ok", False)
    third = observe_step(session, "run_command", act, "ok", False)
    assert SIGNAL_SAME_ERROR not in third
    assert take_preferred_steer(session, third) is None


def test_polling_wait_steers_and_progress_outranks():
    session = _session()
    act = _wait()
    status = '{"status":"running","next_offset":0}'
    observe_step(session, "wait", act, status, False)
    observe_step(session, "wait", act, status, False)
    third = observe_step(session, "wait", act, status, False)
    assert SIGNAL_POLLING in third
    assert SIGNAL_UNCHANGED in third
    picked = take_preferred_steer(session, third)
    assert picked is not None
    assert picked[0] == SIGNAL_UNCHANGED


def test_abab_is_observe_only():
    session = _session()
    a = _cmd("echo a")
    b = _cmd("echo b")
    observe_step(session, "run_command", a, "a", False)
    observe_step(session, "run_command", b, "b", False)
    observe_step(session, "run_command", a, "a", False)
    fourth = observe_step(session, "run_command", b, "b", False)
    assert SIGNAL_ABAB not in fourth
    obs = session._runaway_guard["observations"]
    assert any(item.get("kind") == SIGNAL_ABAB for item in obs)
    assert take_preferred_steer(session, fourth) is None


def test_shadow_mode_records_but_does_not_suffix(monkeypatch):
    monkeypatch.setenv("HARNESS_RUNAWAY_SHADOW", "1")
    session = _session()
    act = _cmd("python missing.py")
    err = "error: boom"
    note_runaway_and_maybe_steer(session, act, err, is_error=True)
    note_runaway_and_maybe_steer(session, act, err, is_error=True)
    third = note_runaway_and_maybe_steer(session, act, err, is_error=True)
    assert third == err
    assert session._runaway_guard["observations"]


def test_suffix_does_not_veto_and_one_steer_per_turn():
    session = _session()
    act = _cmd("python missing.py")
    err = "error: boom"
    note_runaway_and_maybe_steer(session, act, err, is_error=True)
    note_runaway_and_maybe_steer(session, act, err, is_error=True)
    third = note_runaway_and_maybe_steer(session, act, err, is_error=True)
    assert third.startswith(err)
    assert PLUGIN_SOURCE in third
    fourth = note_runaway_and_maybe_steer(session, act, err, is_error=True)
    assert fourth == err


def test_reset_clears_steer_budget():
    session = _session()
    act = _cmd("python missing.py")
    err = "error: boom"
    note_runaway_and_maybe_steer(session, act, err, is_error=True)
    note_runaway_and_maybe_steer(session, act, err, is_error=True)
    note_runaway_and_maybe_steer(session, act, err, is_error=True)
    reset_runaway_state(session)
    first = note_runaway_and_maybe_steer(session, act, err, is_error=True)
    assert first == err
    note_runaway_and_maybe_steer(session, act, err, is_error=True)
    third = note_runaway_and_maybe_steer(session, act, err, is_error=True)
    assert PLUGIN_SOURCE in third


def test_error_family_normalizes_digits():
    assert error_family("error: failed on line 12", True) == error_family(
        "error: failed on line 99", True
    )


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("HARNESS_RUNAWAY_GUARD", "0")
    session = _session()
    act = _cmd("python missing.py")
    err = "error: boom"
    for _ in range(3):
        out = note_runaway_and_maybe_steer(session, act, err, is_error=True)
    assert out == err


def test_append_action_result_suffixes_runaway_without_extra_row(tmp_path):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    cfg = HarnessConfig(state_dir=str(tmp_path))
    session = ConversationalSession(cfg)
    act = PilotAction(
        kind="run_command",
        command="python missing.py",
        arguments={"command": "python missing.py"},
    )
    err = json_error()
    session._append_action_result(act, "c1", err, True, ok=False)
    session._append_action_result(act, "c2", err, True, ok=False)
    before = len(session._history)
    session._append_action_result(act, "c3", err, True, ok=False)
    assert len(session._history) == before + 1
    last = session._history[-1]
    assert last["role"] == "tool"
    assert PLUGIN_SOURCE in last["content"]


def json_error():
    return '{"ok": false, "error": "ModuleNotFoundError: missing"}'
