from __future__ import annotations

from types import SimpleNamespace

from harness.pilot import PilotAction, PilotError, from_wire
from harness.pilot_wait import (
    dispatch_wait_action,
    format_wait_status,
    parse_wait_seconds,
    pending_jobs_keep_alive,
    apply_ready_command_results,
)


def test_parse_wait_seconds_clamps_and_falls_back():
    assert parse_wait_seconds(2) == 2.0
    assert parse_wait_seconds(0) == 0.1
    assert parse_wait_seconds(99) == 30.0
    assert parse_wait_seconds("nope") == 2.0
    assert parse_wait_seconds(None) == 2.0


def test_from_wire_wait_and_validate():
    act = from_wire("wait", {"seconds": 3})
    assert act.kind == "wait"
    assert act.arguments.get("seconds") == 3
    try:
        PilotAction(kind="wait", arguments={"seconds": "nope"}).validate()
    except PilotError:
        pass
    else:
        raise AssertionError("invalid wait seconds must raise")


def test_pending_jobs_keep_alive():
    assert pending_jobs_keep_alive(SimpleNamespace()) is False
    assert pending_jobs_keep_alive(
        SimpleNamespace(has_pending_swarms=lambda: True)
    ) is True
    q = SimpleNamespace(empty=lambda: False)
    assert pending_jobs_keep_alive(SimpleNamespace(_swarm_results=q)) is False
    cancel = SimpleNamespace(is_set=lambda: True)
    assert pending_jobs_keep_alive(
        SimpleNamespace(has_pending_swarms=lambda: True, _cancel=cancel)
    ) is False


def test_pending_command_job_keeps_wait_open_and_delivers_receipt():
    delivered = []
    session = SimpleNamespace(
        has_pending_swarms=lambda: False,
        has_pending_command_jobs=lambda: True,
        drain_swarm_results=lambda **_k: iter(()),
        drain_command_job_receipts=lambda: [{
            "id": "a-command", "job_id": "local-cmd-1",
            "status": "completed", "terminal_receipt": {"status": "completed"},
        }],
        _cancel=SimpleNamespace(is_set=lambda: False),
    )
    assert pending_jobs_keep_alive(session) is True
    delivered.extend(list(apply_ready_command_results(session)))
    assert delivered[0].kind == "action_result"


def test_wait_targets_the_requested_command_job():
    state = {"status": "running", "terminal_receipt": None}

    def get_local_job(_job_id):
        return {
            "id": "local-cmd-target",
            "job_kind": "run_command",
            "session_id": "session-1",
            "action_id": "a-command",
            **state,
        }

    session = SimpleNamespace(
        get_local_job=get_local_job,
        has_pending_swarms=lambda: False,
        has_pending_command_jobs=lambda: False,
        _cancel=SimpleNamespace(is_set=lambda: False),
    )
    assert pending_jobs_keep_alive(session, job_id="local-cmd-target") is True
    state.update(status="completed", terminal_receipt={"status": "completed"})
    assert pending_jobs_keep_alive(session, job_id="local-cmd-target") is False


def test_missing_requested_command_is_not_reported_settled():
    appended = []
    session = SimpleNamespace(
        get_local_job=lambda _job_id: None,
        has_pending_swarms=lambda: False,
        has_pending_command_jobs=lambda: False,
        drain_swarm_results=lambda **_kwargs: iter(()),
        drain_command_job_receipts=lambda **_kwargs: [],
        _cancel=SimpleNamespace(is_set=lambda: False),
        _append_action_result=lambda *args, **kwargs: appended.append((args, kwargs)),
    )
    act = SimpleNamespace(arguments={"seconds": 1, "job_id": "local-cmd-missing"})
    events = list(dispatch_wait_action(session, act, "w-missing", True))
    result = events[-1]
    assert result.kind == "action_result"
    assert result.data["settled"] is False
    assert "unavailable" in result.data["message"].lower()


def test_dispatch_wait_action_returns_status():
    times = [0.0]

    def mono():
        return times[0]

    def sleep(dt):
        times[0] += max(dt, 0.1)

    appended = []
    session = SimpleNamespace(
        has_pending_swarms=lambda: False,
        drain_swarm_results=lambda **_k: iter(()),
        _cancel=SimpleNamespace(is_set=lambda: False),
        _append_action_result=lambda *a, **k: appended.append((a, k)),
    )
    act = from_wire("wait", {"seconds": 1})
    events = list(dispatch_wait_action(
        session, act, "w1", True, sleep=sleep, monotonic=mono,
    ))
    assert events[-1].kind == "action_result"
    assert events[-1].data.get("settled") is True
    assert appended


def test_format_wait_status_settled():
    session = SimpleNamespace(has_pending_swarms=lambda: False)
    text = format_wait_status(session, 2.0, True)
    assert "settled" in text.lower()


def test_late_receipts_update_the_existing_card_instead_of_appending(tmp_path, monkeypatch):
    """A wait replays finished command receipts; each replay appended a card copy."""
    from harness.config import HarnessConfig
    from harness.conversation import ConvEvent, ConversationalSession

    s = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path), repo=str(tmp_path)))
    s._display_transcript.append({"type": "card", "id": "run_command:3", "kind": "run_command",
                                  "goal": "make test", "result": None})
    receipt = {"id": "run_command:3", "job_id": "local-cmd-9", "status": "completed", "message": "exit 0"}

    def fake_send_locked(*_a, **_k):
        for _ in range(3):
            yield ConvEvent("action_result", dict(receipt))
        yield ConvEvent("action_result", {"id": "run_command:4", "status": "completed"})

    monkeypatch.setattr(s, "_send_locked", fake_send_locked)
    kinds = [ev.kind for ev in s.send("go")]
    assert kinds.count("action_result") == 4
    cards = [r for r in s._display_transcript if r.get("type") == "card"]
    assert [c["id"] for c in cards] == ["run_command:3", "run_command:4"]
    assert cards[0]["result"]["status"] == "completed"


def test_wait_drains_only_fresh_command_receipts(tmp_path):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    s = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path), repo=str(tmp_path)))
    s.harness_session_id = "wait-fresh"
    s._local_jobs["local-cmd-1"] = {
        "id": "local-cmd-1", "job_kind": "run_command", "role": "command", "session_id": "wait-fresh",
        "action_id": "run_command:1", "status": "completed", "exit_code": 0,
        "terminal_receipt": {"status": "completed", "exit_code": 0},
    }
    first = list(apply_ready_command_results(s))
    second = list(apply_ready_command_results(s))
    assert len(first) == 1 and second == []
    assert len(s.drain_command_job_receipts()) == 1  # the HTTP replay lane still replays
