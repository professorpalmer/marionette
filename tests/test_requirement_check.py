"""A turn that edits files for a long request checks the request once before it ends.

Hidden tests on spec-style tasks check single sentences of the request (an
exact error text, a field in a String() output). The pilot ended its turn
with those sentences unmet. One check step before the end lists each
requirement and its proof.
"""

from __future__ import annotations

import json
import tempfile

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.send_loop_phases import REQUIREMENT_CHECK_MIN_CHARS, REQUIREMENT_CHECK_NOTE
from pmharness.drivers.openai_compat import DriverResponse

SPEC = "Implement the parser. " + "Reject a bad default with the error `invalid default argument declaration`. " * 20


class _Pilot:
    supports_streaming = True
    name = "spec-pilot"

    def __init__(self, edit: bool):
        self.edit = edit
        self.calls = 0
        self.seen = []

    def chat(self, messages, **_k):
        raise AssertionError("chat() must not run when streaming is available")

    def chat_stream(self, messages, *, tools=None, system=None, on_delta, **_k):
        self.calls += 1
        self.seen.append(json.dumps(messages, default=str))
        if self.calls == 1 and self.edit:
            call = {"id": "c1", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"path": "a.txt", "content": "x\n"})}}
            return DriverResponse(text="", meta={"tool_calls": [call], "finish_reason": "tool_calls",
                                                 "stream_terminal": "tool_calls"})
        on_delta("Done.")
        return DriverResponse(text="Done.", tokens_out=2, meta={
            "tool_calls": [], "finish_reason": "stop", "stream_terminal": "stop", "stream_started": True})


def _run(message: str, edit: bool = True):
    repo = tempfile.mkdtemp()
    s = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", repo=repo, state_dir=tempfile.mkdtemp()))
    s.pilot = _Pilot(edit)
    events = list(s.send(message))
    return s, events


def _checks(s):
    return sum(1 for m in s._history if m.get("source") == "requirement_check")


def test_a_long_request_with_edits_gets_one_check():
    assert len(SPEC) >= REQUIREMENT_CHECK_MIN_CHARS
    s, events = _run(SPEC)
    assert not [e for e in events if e.kind == "error"]
    assert _checks(s) == 1
    first = next(i for i, seen in enumerate(s.pilot.seen) if "acceptance check" in seen)
    assert first >= 2  # after the edit step and the first final answer
    rows = [m.get("source") or m.get("role") for m in s._history]
    assert rows[rows.index("requirement_check") + 1] == "assistant"


def test_a_short_request_gets_no_check():
    s, _ = _run("Fix the typo in a.txt.")
    assert _checks(s) == 0
    assert "acceptance check" not in s.pilot.seen[-1]


def test_a_long_request_without_edits_gets_no_check():
    s, _ = _run(SPEC, edit=False)
    assert _checks(s) == 0
    assert s.pilot.calls == 1


def test_the_check_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("HARNESS_REQUIREMENT_CHECK", "0")
    s, _ = _run(SPEC)
    assert _checks(s) == 0


def test_the_note_names_the_request_parts_hidden_tests_check():
    note = REQUIREMENT_CHECK_NOTE.lower()
    for word in ("error", "edge case", "format"):
        assert word in note


def test_the_minimum_length_can_be_lowered(monkeypatch):
    monkeypatch.setenv("HARNESS_REQUIREMENT_CHECK_MIN_CHARS", "10")
    s, _ = _run("Fix the typo in a.txt and keep the rest.")
    assert _checks(s) == 1
