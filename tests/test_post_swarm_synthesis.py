"""Regression coverage for terminal post-swarm synthesis."""

from __future__ import annotations

import copy
import json
import tempfile
from typing import Any, Callable, Dict, List, Optional, Union

import harness.send_loop as send_loop
from harness.config import HarnessConfig
from harness.conversation import ConvEvent, ConversationalSession
from harness.send_loop import (
    POST_SWARM_SYNTHESIS_FALLBACK,
    POST_SWARM_SYNTHESIS_NUDGE,
)
from pmharness.drivers.base import DriverResponse


_ResponseFactory = Callable[..., DriverResponse]


def _pilot_envelope(
    *,
    say: str = "",
    actions: Optional[List[Dict[str, Any]]] = None,
) -> DriverResponse:
    return DriverResponse(
        text=json.dumps({"say": say, "actions": actions or []}),
    )


class _SequencePilot:
    """Small deterministic pilot that records each rendered request."""

    def __init__(
        self,
        responses: List[Union[DriverResponse, _ResponseFactory]],
        *,
        streaming: bool = False,
    ) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, Any]] = []
        self.supports_streaming = streaming

    def _next_response(self, messages: list[dict], kwargs: dict[str, Any]) -> DriverResponse:
        self.calls.append({
            "messages": copy.deepcopy(messages),
            "kwargs": copy.deepcopy(kwargs),
        })
        response = next(self._responses)
        return response(**kwargs) if callable(response) else response

    def chat(self, messages: list[dict], **kwargs: Any) -> DriverResponse:
        return self._next_response(messages, kwargs)

    def chat_stream(self, messages: list[dict], **kwargs: Any) -> DriverResponse:
        return self._next_response(messages, kwargs)


def _fake_execute_turn_actions(
    session: ConversationalSession,
    *,
    turn: Any,
    counters: dict[str, int],
    turn_findings: list,
    **_: Any,
):
    """Model one completed synchronous run_swarm without starting a worker."""
    if not turn.actions:
        return (None, [])
    assert [action.kind for action in turn.actions] == ["run_swarm"]
    counters["swarms"] += 1
    counters["synchronous_swarms"] = counters.get("synchronous_swarms", 0) + 1
    turn_findings.append({"kind": "finding", "headline": "one completed finding"})
    session._history.append({
        "role": "user",
        "content": "[swarm result] one completed finding",
    })
    yield ConvEvent("swarm_result", {"findings": 1})
    return (None, [])


def _fake_failed_execute_turn_actions(
    session: ConversationalSession,
    *,
    turn: Any,
    counters: dict[str, int],
    **_: Any,
):
    """Model a synchronous run_swarm that fails before producing a result."""
    assert [action.kind for action in turn.actions] == ["run_swarm"]
    counters["synchronous_swarms"] = counters.get("synchronous_swarms", 0) + 1
    session._history.append({
        "role": "user",
        "content": "[swarm result] failed before producing findings",
    })
    yield ConvEvent("action_result", {"error": "swarm failed"})
    return (None, [])


def _fake_partial_delivery_execute_turn_actions(
    session: ConversationalSession,
    *,
    turn: Any,
    counters: dict[str, int],
    turn_findings: list,
    **_: Any,
):
    """A delivery warning is evidence for synthesis, never a completion block."""
    assert [action.kind for action in turn.actions] == ["run_swarm"]
    counters["swarms"] += 1
    counters["synchronous_swarms"] = counters.get("synchronous_swarms", 0) + 1
    turn_findings.append({"kind": "finding", "headline": "delivered finding"})
    session._history.append({
        "role": "user",
        "content": (
            "PM artifacts: 17\nAvailable to inspect: 16/17\n"
            "WARNING: Synthesis continued with incomplete PM evidence.\n"
            "missing artifact-16 task=task-4"
        ),
    })
    yield ConvEvent("action_result", {
        "artifact_delivery": {
            "pm_artifacts": 17,
            "available_to_inspect": 16,
            "complete": False,
            "missing": [{"id": "artifact-16", "task_id": "task-4"}],
        },
    })
    return (None, [])


def _fake_drain_idle_turn(
    _session: ConversationalSession,
    *,
    user_message: str,
    step: int,
    swarms: int,
    **_: Any,
):
    yield ConvEvent("assistant_done", {"turns": step + 1, "swarms": swarms})
    return ("return", user_message)


def _run_post_swarm_turn(
    monkeypatch,
    pilot: _SequencePilot,
    *,
    execute_actions=_fake_execute_turn_actions,
) -> tuple[ConversationalSession, list[ConvEvent]]:
    config = HarnessConfig(
        driver="stub-oracle-v2",
        state_dir=tempfile.mkdtemp(prefix="post-swarm-test-"),
    )
    session = ConversationalSession(config)
    session.pilot = pilot
    session._build_visible_tools_schema = lambda: [{"name": "run_swarm"}]
    session._maybe_compact_history = lambda *args, **kwargs: iter(())
    session._submit_housekeeping = lambda *args, **kwargs: None
    session._turn_budget_exhausted = lambda: False

    monkeypatch.setattr(send_loop, "execute_turn_actions", execute_actions)
    monkeypatch.setattr(send_loop, "drain_idle_turn", _fake_drain_idle_turn)
    events = list(session._send_locked_inner("audit the repository"))
    return session, events


def test_empty_post_swarm_synthesis_gets_one_hidden_nudge_then_fallback(monkeypatch):
    pilot = _SequencePilot([
        _pilot_envelope(actions=[{"kind": "run_swarm", "goal": "audit findings"}]),
        _pilot_envelope(),
        _pilot_envelope(),
    ])

    session, events = _run_post_swarm_turn(monkeypatch, pilot)

    messages = [event for event in events if event.kind == "message"]
    assert [event.data["text"] for event in messages] == [POST_SWARM_SYNTHESIS_FALLBACK]
    assert events[-1].kind == "assistant_done"
    assert len(pilot.calls) == 3
    assert pilot.calls[2]["kwargs"]["tools"] == []
    assert pilot.calls[2]["messages"][-1] == {
        "role": "user",
        "content": POST_SWARM_SYNTHESIS_NUDGE,
    }
    assert not any(
        item.get("text") == POST_SWARM_SYNTHESIS_NUDGE
        for item in session._display_transcript
    )
    roles = [message["role"] for message in session._history]
    assert all(left != right for left, right in zip(roles, roles[1:]))
    assert session._history[-1]["content"] == POST_SWARM_SYNTHESIS_FALLBACK


def test_failed_post_swarm_action_still_gets_a_user_facing_fallback(monkeypatch):
    pilot = _SequencePilot([
        _pilot_envelope(actions=[{"kind": "run_swarm", "goal": "audit findings"}]),
        _pilot_envelope(),
        _pilot_envelope(),
    ])

    _session, events = _run_post_swarm_turn(
        monkeypatch,
        pilot,
        execute_actions=_fake_failed_execute_turn_actions,
    )

    messages = [event for event in events if event.kind == "message"]
    assert [event.data["text"] for event in messages] == [POST_SWARM_SYNTHESIS_FALLBACK]
    assert len(pilot.calls) == 3


def test_partial_delivery_warning_does_not_block_synthesis(monkeypatch):
    pilot = _SequencePilot([
        _pilot_envelope(actions=[{"kind": "run_swarm", "goal": "audit findings"}]),
        _pilot_envelope(say="Synthesis from the 16 available artifacts."),
    ])

    session, events = _run_post_swarm_turn(
        monkeypatch,
        pilot,
        execute_actions=_fake_partial_delivery_execute_turn_actions,
    )

    assert [
        event.data["text"] for event in events if event.kind == "message"
    ] == ["Synthesis from the 16 available artifacts."]
    assert any(
        "Available to inspect: 16/17" in str(message.get("content") or "")
        for message in session._history
    )
    assert events[-1].kind == "assistant_done"


def test_synthesis_nudge_never_dispatches_hallucinated_tools(monkeypatch):
    pilot = _SequencePilot([
        _pilot_envelope(actions=[{"kind": "run_swarm", "goal": "audit findings"}]),
        _pilot_envelope(),
        _pilot_envelope(actions=[{"kind": "run_swarm", "goal": "hallucinated action"}]),
    ])

    _session, events = _run_post_swarm_turn(monkeypatch, pilot)

    messages = [event for event in events if event.kind == "message"]
    assert [event.data["text"] for event in messages] == [POST_SWARM_SYNTHESIS_FALLBACK]
    assert len(pilot.calls) == 3
    assert pilot.calls[2]["kwargs"]["tools"] == []


def test_progress_only_post_swarm_synthesis_cannot_close_without_message(monkeypatch):
    def progress_only(**kwargs: Any) -> DriverResponse:
        kwargs["on_delta"]({
            "text": "planning the report",
            "channel": "progress",
            "stream_id": "progress-1",
        })
        return DriverResponse(text="")

    pilot = _SequencePilot([
        _pilot_envelope(actions=[{"kind": "run_swarm", "goal": "audit findings"}]),
        progress_only,
        _pilot_envelope(),
    ], streaming=True)

    _session, events = _run_post_swarm_turn(monkeypatch, pilot)

    progress_events = [
        event for event in events
        if event.kind == "message_delta"
        and event.data.get("channel") == "progress"
    ]
    assert progress_events
    message_indexes = [
        index for index, event in enumerate(events) if event.kind == "message"
    ]
    done_index = next(index for index, event in enumerate(events) if event.kind == "assistant_done")
    assert message_indexes
    assert all(index < done_index for index in message_indexes)
    assert events[message_indexes[-1]].data["text"] == POST_SWARM_SYNTHESIS_FALLBACK
    assert len(pilot.calls) == 3
    assert pilot.calls[2]["kwargs"]["tools"] == []


def test_streamed_answer_does_not_get_a_second_synthesis(monkeypatch):
    def answer_only(**kwargs: Any) -> DriverResponse:
        kwargs["on_delta"]('{"say":"The audit found one issue."}')
        return DriverResponse(text="")

    pilot = _SequencePilot([
        _pilot_envelope(actions=[{"kind": "run_swarm", "goal": "audit findings"}]),
        answer_only,
    ], streaming=True)

    _session, events = _run_post_swarm_turn(monkeypatch, pilot)

    answer_deltas = [
        event for event in events
        if event.kind == "message_delta"
        and event.data.get("channel") == "answer"
    ]
    assert answer_deltas
    assert "".join(event.data["text"] for event in answer_deltas) == (
        "The audit found one issue."
    )
    assert not any(
        event.kind == "message"
        and event.data.get("text") in {
            POST_SWARM_SYNTHESIS_NUDGE,
            POST_SWARM_SYNTHESIS_FALLBACK,
        }
        for event in events
    )
    assert len(pilot.calls) == 2
    assert events[-1].kind == "assistant_done"


def test_reasoning_only_post_swarm_synthesis_is_promoted_before_done(monkeypatch):
    def reasoning_only(**kwargs: Any) -> DriverResponse:
        kwargs["on_reasoning_delta"]("The audit found one issue.")
        return DriverResponse(text="")

    pilot = _SequencePilot([
        _pilot_envelope(actions=[{"kind": "run_swarm", "goal": "audit findings"}]),
        reasoning_only,
    ], streaming=True)

    _session, events = _run_post_swarm_turn(monkeypatch, pilot)

    message_indexes = [
        index for index, event in enumerate(events) if event.kind == "message"
    ]
    done_index = next(index for index, event in enumerate(events) if event.kind == "assistant_done")
    assert len(message_indexes) == 1
    assert message_indexes[0] < done_index
    assert events[message_indexes[0]].data["text"] == "The audit found one issue."
    assert len(pilot.calls) == 2


def test_successful_post_swarm_synthesis_emits_one_message_before_done(monkeypatch):
    pilot = _SequencePilot([
        _pilot_envelope(actions=[{"kind": "run_swarm", "goal": "audit findings"}]),
        _pilot_envelope(say="The audit found one issue."),
    ])

    _session, events = _run_post_swarm_turn(monkeypatch, pilot)

    message_indexes = [
        index for index, event in enumerate(events) if event.kind == "message"
    ]
    done_index = next(index for index, event in enumerate(events) if event.kind == "assistant_done")
    assert len(message_indexes) == 1
    assert message_indexes[0] < done_index
    assert events[message_indexes[0]].data["text"] == "The audit found one issue."
    assert len(pilot.calls) == 2
    assert pilot.calls[1]["kwargs"]["tools"] == [{"name": "run_swarm"}]
