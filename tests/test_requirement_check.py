"""A low-effort turn that edits files for a long request runs two check rounds before it ends.

At high effort the check gave no gain, so a high-effort turn is unchanged.
"""

from __future__ import annotations

import json
import tempfile

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.requirement_check import AUTHOR_NOTE, MIN_REQUEST_CHARS, NOTE
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


@pytest.fixture(autouse=True)
def _effort(monkeypatch):
    monkeypatch.delenv("HARNESS_REQUIREMENT_CHECK", raising=False)
    monkeypatch.delenv("HARNESS_REQUIREMENT_CHECK_ROUNDS", raising=False)
    monkeypatch.setenv("HARNESS_ENDPOINT_REASONING_EFFORT", "low")


def _run(message: str, edit: bool = True):
    s = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", repo=tempfile.mkdtemp(),
                                            state_dir=tempfile.mkdtemp()))
    s.pilot = _Pilot(edit)
    events = list(s.send(message))
    return s, events


def _checks(s):
    return sum(1 for m in s._history if m.get("source") == "requirement_check")


def test_a_low_effort_long_request_with_edits_gets_the_check_then_the_author_round():
    assert len(SPEC) >= MIN_REQUEST_CHARS
    s, events = _run(SPEC)
    assert not [e for e in events if e.kind == "error"]
    notes = [m["content"] for m in s._history if m.get("source") == "requirement_check"]
    assert notes == [NOTE, AUTHOR_NOTE]
    first = next(i for i, seen in enumerate(s.pilot.seen) if "acceptance check" in seen)
    assert first >= 2  # after the edit step and the first final answer
    rows = [m.get("source") or m.get("role") for m in s._history]
    for i, row in enumerate(rows):
        if row == "requirement_check":
            assert rows[i + 1] == "assistant"
    assert rows[-1] == "assistant"


def test_one_round_when_the_env_asks(monkeypatch):
    monkeypatch.setenv("HARNESS_REQUIREMENT_CHECK_ROUNDS", "1")
    assert _checks(_run(SPEC)[0]) == 1


@pytest.mark.parametrize("effort", ["high", "xhigh", "max", ""])
def test_high_or_unset_effort_gets_no_check(monkeypatch, effort):
    monkeypatch.setenv("HARNESS_ENDPOINT_REASONING_EFFORT", effort)
    s, _ = _run(SPEC)
    assert _checks(s) == 0


def test_the_env_forces_the_check_on_and_off(monkeypatch):
    monkeypatch.setenv("HARNESS_ENDPOINT_REASONING_EFFORT", "high")
    monkeypatch.setenv("HARNESS_REQUIREMENT_CHECK", "1")
    assert _checks(_run(SPEC)[0]) == 2
    monkeypatch.setenv("HARNESS_ENDPOINT_REASONING_EFFORT", "low")
    monkeypatch.setenv("HARNESS_REQUIREMENT_CHECK", "0")
    assert _checks(_run(SPEC)[0]) == 0


def test_a_short_request_gets_no_check():
    s, _ = _run("Fix the typo in a.txt.")
    assert _checks(s) == 0


def test_a_long_request_without_edits_gets_no_check():
    s, _ = _run(SPEC, edit=False)
    assert _checks(s) == 0
