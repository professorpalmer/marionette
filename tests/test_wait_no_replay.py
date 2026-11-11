"""``wait`` must not show finished foreground commands again.

A foreground run_command delivers its result inline. Its job row was never
stamped as delivered, so the first ``wait`` drain re-sent every earlier
command as a new "completed" card, timed from its start (an ``ls`` showed
692 s in a leaderboard trial).
"""

from __future__ import annotations

import json
import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from pmharness.drivers.openai_compat import DriverResponse


class _Pilot:
    supports_streaming = True
    name = "wait-pilot"

    def __init__(self, calls):
        self.calls = list(calls)

    def chat(self, messages, **_k):
        raise AssertionError("chat() must not run when streaming is available")

    def chat_stream(self, messages, *, tools=None, system=None, on_delta, **_k):
        if self.calls:
            name, args = self.calls.pop(0)
            call = {"id": "c%d" % len(self.calls), "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}
            return DriverResponse(text="", meta={"tool_calls": [call], "finish_reason": "tool_calls",
                                                 "stream_terminal": "tool_calls"})
        on_delta("Done.")
        return DriverResponse(text="Done.", tokens_out=2, meta={
            "tool_calls": [], "finish_reason": "stop", "stream_terminal": "stop", "stream_started": True})


def test_wait_does_not_resend_finished_foreground_commands():
    s = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", repo=tempfile.mkdtemp(), state_dir=tempfile.mkdtemp()))
    s.pilot = _Pilot([("run_command", {"command": "echo one"}),
                      ("run_command", {"command": "echo two"}),
                      ("wait", {"seconds": 1})])
    events = list(s.send("Run two commands, then wait."))
    results = [e.data for e in events if e.kind == "action_result" and e.data.get("job_id")]
    jobs = [r["job_id"] for r in results]
    assert len(jobs) == 2, results
    assert len(set(jobs)) == 2
