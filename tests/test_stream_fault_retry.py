"""A looped or stuck stream retries the step instead of ending the turn.

Unattended runs (headless exec, schedules) have nobody to press Retry, so
a cut stream ended the run. The step now retries, at most twice.
"""

from __future__ import annotations

import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.send_loop_phases import STREAM_IDLE_STUCK_MESSAGE, StreamIdleStuckError
from pmharness.drivers.openai_compat import DriverResponse

LOOP = "OK. Let me write the file now. " * 2_000


class _Pilot:
    supports_streaming = True
    name = "fault-pilot"

    def __init__(self, faults):
        self.faults = list(faults)
        self.calls = 0
        self.seen = []

    def chat(self, messages, **_k):
        raise AssertionError("chat() must not run when streaming is available")

    def chat_stream(self, messages, *, tools=None, system=None, on_delta,
                    on_reasoning_delta=None, on_tool_hint=None, is_cancelled=None, **_k):
        self.calls += 1
        self.seen.append(list(messages))
        fault = self.faults.pop(0) if self.faults else None
        if fault == "loop":
            for i in range(0, len(LOOP), 200):
                if is_cancelled and is_cancelled():
                    break
                on_reasoning_delta(LOOP[i:i + 200])
            return DriverResponse(text="", meta={"tool_calls": [], "finish_reason": "stop", "stream_terminal": "stop"})
        if fault == "stuck":
            raise StreamIdleStuckError(STREAM_IDLE_STUCK_MESSAGE)
        on_delta("Done.")
        return DriverResponse(text="Done.", tokens_out=2, meta={
            "tool_calls": [], "finish_reason": "stop", "stream_terminal": "stop", "stream_started": True})


def _run(faults):
    s = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp()))
    s.pilot = _Pilot(faults)
    return s, list(s.send("Write the file."))


def _texts(messages):
    return " ".join(str(m.get("content") or "") for m in messages if isinstance(m, dict))


def test_a_looped_stream_retries_with_a_nudge():
    s, events = _run(["loop"])
    assert not [e for e in events if e.kind == "error"]
    assert s.pilot.calls == 2
    assert any(e.kind == "notice" and "repeat" in str(e.data.get("message", "")).lower() for e in events)
    assert "repeated itself" in _texts(s.pilot.seen[1])
    assert "Let me write the file now. OK." not in _texts(s._history)
    assert [e for e in events if e.kind == "message"][-1].data["text"] == "Done."


def test_a_stuck_stream_retries_without_a_nudge():
    s, events = _run(["stuck"])
    assert not [e for e in events if e.kind == "error"]
    assert s.pilot.calls == 2
    assert "repeated itself" not in _texts(s.pilot.seen[1])


def test_the_retry_is_bounded():
    s, events = _run(["loop", "loop", "loop", "loop"])
    errors = [e for e in events if e.kind == "error"]
    assert s.pilot.calls == 3
    assert errors and "repeat itself" in errors[-1].data["error"]
