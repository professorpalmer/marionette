from __future__ import annotations

"""A turn that ends on an announced next action, with no tool call, is sent
back to the model at most twice. Before, the step counted as productive
because it had prose, and the turn ended as complete with no work done."""

import pytest

from harness.terminal_empty_recovery import STALL_NUDGE_LIMIT, STALL_PROMPT, announces_next_action
from test_post_swarm_synthesis import _SequencePilot, _pilot_envelope, _run_post_swarm_turn


@pytest.mark.parametrize("say", [
    "Let me now run the tests.",
    "I found the bug. I'll now fix it:",
    "Next, I run the suite.",
])
def test_announced_action_is_a_stall(say):
    assert announces_next_action(say)


@pytest.mark.parametrize("say", [
    "Done. The tests pass.",
    "Let me know if you want more.",
    "I will now explain. " + "The cache key skips the model id. " * 12,
])
def test_answers_are_not_stalls(say):
    assert not announces_next_action(say)


def test_promoted_reasoning_that_ends_on_a_plan_is_a_stall():
    assert announces_next_action("", "The file is at x. Let me run the tests to check.")
    assert not announces_next_action("", "Let me check. The answer is 42.")


def test_stalled_turn_is_nudged_then_finishes(monkeypatch):
    pilot = _SequencePilot([
        _pilot_envelope(say="Let me now run the tests."),
        _pilot_envelope(say="The tests pass."),
    ])
    _, events = _run_post_swarm_turn(monkeypatch, pilot)
    assert len(pilot.calls) == 2
    assert pilot.calls[1]["messages"][-1] == {"role": "user", "content": STALL_PROMPT}
    assert events[-1].kind == "assistant_done"


def test_stall_nudges_stop_at_the_limit(monkeypatch):
    replies = [_pilot_envelope(say="Let me now run the tests.") for _ in range(STALL_NUDGE_LIMIT + 2)]
    pilot = _SequencePilot(replies)
    _, events = _run_post_swarm_turn(monkeypatch, pilot)
    assert len(pilot.calls) == STALL_NUDGE_LIMIT + 1
    assert events[-1].kind == "assistant_done"
