"""SessionActionStore — illegal transitions, snapshot/restore, TurnInputMode."""
from __future__ import annotations

import json

import pytest

from harness.session_actions import (
    ActionKind,
    DeliveryPolicy,
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


def test_normalize_turn_input_mode_rejects_unknown():
    assert normalize_turn_input_mode("start-if-idle") == TurnInputMode.START_IF_IDLE.value
    assert normalize_turn_input_mode("STEER") == TurnInputMode.STEER.value
    assert normalize_turn_input_mode("recover") is None
    assert normalize_turn_input_mode("nope") is None
    assert normalize_turn_input_mode(None) is None
