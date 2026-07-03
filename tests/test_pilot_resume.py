"""Keep-alive: after a background swarm finishes, the pilot must continue on its
own -- assess the result and take the next step -- without a new user message and
without autopilot. drain_swarm_results injects a user-role continuation into
history; send(resume=True) then generates OFF that history without appending any
new user turn. These tests pin that contract so the pilot never again "launches
swarms then sleeps"."""
import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def _session():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    return ConversationalSession(cfg)


def _user_turns(history):
    return [m for m in history if m.get("role") == "user"]


def test_resume_generates_without_appending_a_user_turn():
    s = _session()
    continuation = (
        "[background job abc finished] The result above is now available. "
        "Report the outcome and take the next step."
    )
    s._history.append({"role": "user", "content": continuation})
    users_before = _user_turns(s._history)

    events = list(s.send("", resume=True))

    # The pilot actually reached the model and drove a turn -- proven by emitting
    # events (assistant_done live, or an error event when the test harness blocks
    # the network). The bail path (nothing pending) emits nothing; a real resume
    # never does. This makes the "did it generate?" check network-independent.
    assert events, "resume must drive a real pilot turn, not no-op"
    # No blank/duplicate user turn was fabricated: the continuation is still the
    # only user message with that content, and no empty-content user turn exists.
    matching = [m for m in _user_turns(s._history) if continuation in m["content"]]
    assert len(matching) == 1, "resume must not append or duplicate the continuation"
    assert all(m["content"].strip() for m in _user_turns(s._history)), (
        "resume must never append an empty user turn"
    )
    # It did not smuggle in extra user turns beyond what was already there.
    assert len(_user_turns(s._history)) == len(users_before)


def test_resume_bails_cleanly_when_nothing_to_continue():
    """A stray resume trigger with no pending continuation (last turn is not a
    user message) must be a clean no-op -- never a fabricated empty turn or a
    crash."""
    s = _session()
    # Fresh session: history is just the system prompt, so the last turn is not
    # a user message and there is nothing to respond to.
    assert s._history[-1]["role"] != "user"
    len_before = len(s._history)

    events = list(s.send("", resume=True))

    assert events == [], "resume with nothing pending must yield no events"
    assert len(s._history) == len_before, "resume no-op must not mutate history"


def test_resume_after_drain_is_the_full_keep_alive_loop():
    """End to end: a finished job drains (injecting the continuation), then a
    resume turn consumes it and drives the pilot -- the exact 'wait, assess,
    continue' behavior the user asked for."""
    s = _session()
    s._swarm_results.put({
        "job_id": "job-1",
        "objective": "add a helper",
        "result": {"applied": True, "files": ["helper.py"], "summary": "added it"},
    })
    drain_events = list(s.drain_swarm_results())
    assert any(e.kind == "pilot_resume" for e in drain_events)
    assert s._history[-1]["role"] == "user"  # continuation is pending

    resume_events = list(s.send("", resume=True))
    assert resume_events, "resume must drive a turn off the drained continuation"
    # And the continuation was consumed as the turn's basis, not re-appended.
    assert len(_user_turns(s._history)) == 1
