"""SessionActionStore — illegal transitions, snapshot/restore, TurnInputMode."""
from __future__ import annotations

import json

import pytest

from harness.session_actions import (
    ActionKind,
    DeliveryPolicy,
    MAILBOX_DRAIN_LIMIT,
    SessionActionIllegalTransition,
    SessionActionStore,
    TurnInputMode,
    WakePolicy,
    normalize_turn_input_mode,
)


def test_recover_requires_expected_turn_id():
    store = SessionActionStore()
    with pytest.raises(SessionActionIllegalTransition) as exc:
        store.admit(ActionKind.RECOVER, "resume this turn")
    assert exc.value.code == "recover_requires_expected_turn_id"
    store.admit(ActionKind.RECOVER, "resume this turn", expected_turn_id="turn-1")
    assert [a.kind for a in store] == [ActionKind.RECOVER]
    assert store.drain_ready(DeliveryPolicy.NEXT_TURN_BOUNDARY)[0].expected_turn_id == "turn-1"


def test_steer_expected_turn_id_must_match_current():
    store = SessionActionStore()
    store.set_current_turn_id("turn-a")
    with pytest.raises(SessionActionIllegalTransition) as exc:
        store.admit(ActionKind.STEER, "nudge", expected_turn_id="turn-b")
    assert exc.value.code == "steer_turn_mismatch"
    action = store.admit(ActionKind.STEER, "nudge", expected_turn_id="turn-a")
    assert action.kind is ActionKind.STEER
    assert action.delivery is DeliveryPolicy.NEXT_TURN_BOUNDARY


def test_recover_expected_turn_id_must_match_current_when_set():
    store = SessionActionStore()
    store.set_current_turn_id("turn-a")
    with pytest.raises(SessionActionIllegalTransition) as exc:
        store.admit(ActionKind.RECOVER, "resume", expected_turn_id="turn-b")
    assert exc.value.code == "recover_turn_mismatch"
    action = store.admit(ActionKind.RECOVER, "resume", expected_turn_id="turn-a")
    assert action.kind is ActionKind.RECOVER
    assert action.expected_turn_id == "turn-a"


def test_admit_after_closed_is_illegal():
    store = SessionActionStore()
    store.admit(ActionKind.STEER, "before close")
    store.close()
    with pytest.raises(SessionActionIllegalTransition) as exc:
        store.admit(ActionKind.MAILBOX, "too late")
    assert exc.value.code == "store_closed"


def test_snapshot_restore_is_json_safe_and_committible():
    store = SessionActionStore()
    store.set_current_turn_id("turn-9")
    store.admit(ActionKind.STEER, "keep going", images=["/tmp/a.png"])
    store.admit(
        ActionKind.MAILBOX,
        "later",
        delivery=DeliveryPolicy.WHEN_RUN_IDLE,
        wake=WakePolicy.ON_IDLE,
    )
    snap = store.snapshot()
    encoded = json.dumps(snap)
    loaded = json.loads(encoded)
    assert loaded["current_turn_id"] == "turn-9"
    assert loaded["closed"] is False
    assert [row["kind"] for row in loaded["actions"]] == ["steer", "mailbox"]
    assert loaded["actions"][0]["images"] == ["/tmp/a.png"]

    other = SessionActionStore()
    other.restore(loaded)
    assert other.current_turn_id == "turn-9"
    assert [a.kind.value for a in other] == ["steer", "mailbox"]
    assert other.drain_ready(DeliveryPolicy.WHEN_RUN_IDLE)[0].text == "later"
    assert [a.text for a in other] == ["keep going"]


def test_turn_input_mode_start_if_idle_vs_steer():
    idle = SessionActionStore()
    started = idle.admit_turn_input("begin", TurnInputMode.START_IF_IDLE, idle=True)
    assert started.kind is ActionKind.START
    assert idle.current_turn_id

    busy = SessionActionStore()
    busy.set_current_turn_id("live")
    with pytest.raises(SessionActionIllegalTransition) as exc:
        busy.admit_turn_input("nope", TurnInputMode.START_IF_IDLE, idle=False)
    assert exc.value.code == "start_if_idle_busy"

    steered = busy.admit_turn_input(
        "course correct",
        TurnInputMode.STEER,
        expected_turn_id="live",
        idle=False,
    )
    assert steered.kind is ActionKind.STEER

    either = SessionActionStore()
    assert either.admit_turn_input("go", TurnInputMode.START_OR_STEER, idle=True).kind is ActionKind.START
    running = SessionActionStore()
    running.set_current_turn_id("t2")
    assert (
        running.admit_turn_input("go", TurnInputMode.START_OR_STEER, idle=False).kind
        is ActionKind.STEER
    )


def test_admit_front_moves_new_action_to_head():
    store = SessionActionStore()
    store.admit(ActionKind.STEER, "older")
    store.admit_front(ActionKind.STEER, "newer")
    assert [a.text for a in store] == ["newer", "older"]


def test_admit_front_reorders_existing_input_id():
    store = SessionActionStore()
    older = store.admit(ActionKind.STEER, "older", input_id="steer-1")
    mid = store.admit(ActionKind.MAILBOX, "mid", input_id="mail-1")
    store.admit(ActionKind.STEER, "newer", input_id="steer-2")
    again = store.admit_front(ActionKind.MAILBOX, "ignored-on-dedupe", input_id="mail-1")
    assert again is mid
    assert [a.id for a in store] == ["mail-1", "steer-1", "steer-2"]
    assert [a.text for a in store] == ["mid", "older", "newer"]
    assert list(store)[0] is mid
    assert list(store)[1] is older


def test_command_id_retry_ack_while_inflight():
    store = SessionActionStore()
    first = store.admit(ActionKind.STEER, "once", input_id="cmd-1")
    drained = store.drain_ready(DeliveryPolicy.NEXT_TURN_BOUNDARY, kinds=(ActionKind.STEER,))
    assert [row.id for row in drained] == ["cmd-1"]
    again = store.admit(ActionKind.STEER, "ignored", input_id="cmd-1")
    assert again is first
    assert len(store) == 0


def test_command_id_pin_live_after_settle():
    store = SessionActionStore()
    first = store.admit(ActionKind.STEER, "once", input_id="cmd-1")
    store.drain_ready(DeliveryPolicy.NEXT_TURN_BOUNDARY, kinds=(ActionKind.STEER,))
    store.settle("cmd-1")
    again = store.admit(ActionKind.STEER, "ignored", input_id="cmd-1")
    assert again is first
    assert len(store) == 0


def test_command_gated_fifo_until_settle():
    store = SessionActionStore()
    store.admit(ActionKind.START, "first", input_id="start-1")
    store.admit(ActionKind.START, "second", input_id="start-2")
    first = store.drain_ready(DeliveryPolicy.NEXT_TURN_BOUNDARY, kinds=(ActionKind.START,))
    assert [row.id for row in first] == ["start-1"]
    assert store.drain_ready(DeliveryPolicy.NEXT_TURN_BOUNDARY, kinds=(ActionKind.START,)) == []
    assert [row.id for row in store] == ["start-2"]
    store.settle("start-1")
    second = store.drain_ready(DeliveryPolicy.NEXT_TURN_BOUNDARY, kinds=(ActionKind.START,))
    assert [row.id for row in second] == ["start-2"]


def test_mailbox_drain_is_capped():
    store = SessionActionStore()
    for index in range(MAILBOX_DRAIN_LIMIT + 5):
        store.admit(ActionKind.MAILBOX, "m%s" % index, input_id="mail-%s" % index)
    drained = store.drain_ready(DeliveryPolicy.WHEN_RUN_IDLE, kinds=(ActionKind.MAILBOX,))
    assert len(drained) == MAILBOX_DRAIN_LIMIT
    assert len(store) == 5


def test_restore_old_snapshot_without_pin_live():
    store = SessionActionStore()
    store.restore({"closed": False, "current_turn_id": None, "actions": []})
    assert dict(store._inflight) == {}
    assert list(store._settled) == []


def test_normalize_turn_input_mode_rejects_unknown():
    assert normalize_turn_input_mode("start-if-idle") == TurnInputMode.START_IF_IDLE.value
    assert normalize_turn_input_mode("STEER") == TurnInputMode.STEER.value
    assert normalize_turn_input_mode("recover") is None
    assert normalize_turn_input_mode("nope") is None
    assert normalize_turn_input_mode(None) is None
