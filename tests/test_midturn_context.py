import pytest
import os
import tempfile
from unittest.mock import MagicMock

from harness.config import HarnessConfig
from harness.conversation import (
    ConversationalSession,
    ConvEvent,
    _clamp_tool_result,
)


class MockDriverResponse:
    def __init__(self, text="", error=None, tokens_out=10, tokens_in=10):
        self.text = text
        self.error = error
        self.tokens_out = tokens_out
        self.tokens_in = tokens_in
        self.meta = {}


class MockPilotWithOverflow:
    name = "mock_overflow"
    def __init__(self, normal_text="Done"):
        self.normal_text = normal_text
        self.calls = 0

    def chat(self, messages, tools=None, system=None):
        self.calls += 1
        if self.calls == 1:
            return MockDriverResponse(error="HTTP 400: maximum context length exceeded")
        return MockDriverResponse(text=self.normal_text)

    def complete(self, prompt, system=None):
        self.calls += 1
        if self.calls == 1:
            return MockDriverResponse(error="HTTP 400: maximum context length exceeded")
        return MockDriverResponse(text=self.normal_text)


def test_clamp_tool_result_small():
    text = "hello world"
    clamped = _clamp_tool_result(text, max_chars=100)
    assert clamped == text


def test_clamp_tool_result_large():
    max_chars = 20
    text = "0123456789abcdefghijklmnopqrstuvwxyz"
    clamped = _clamp_tool_result(text, max_chars=max_chars)
    
    # Text length should be clamped.
    # Keeps head and tail, elides middle.
    assert len(clamped) > max_chars
    assert "truncated" in clamped
    assert text[:10] in clamped  # head: max_chars // 2 = 10
    assert text[-10:] in clamped # tail: max_chars - 10 = 10


def test_append_action_result_clamped():
    with tempfile.TemporaryDirectory() as temp_dir:
        cfg = HarnessConfig(state_dir=temp_dir)
        session = ConversationalSession(cfg)
        
        # Drive with an oversized result
        large_content = "A" * 50000
        
        # Simulate what the loop does: yields an event with the full content, then appends
        # to history via _append_action_result.
        event = ConvEvent("action_result", {"id": "call_1", "artifacts": large_content})
        
        # Ensure that the event contains full text
        assert event.data["artifacts"] == large_content
        
        class StubAction:
            kind = "read_file"
            path = "dummy.txt"
            goal = "read dummy"
            tool_call_id = "call_1"
        
        act = StubAction()
        # Append to action result
        session._append_action_result(act, "call_1", large_content, is_native=True)
        
        last_history_entry = session._history[-1]
        assert last_history_entry["role"] == "tool"
        assert "truncated" in last_history_entry["content"]
        assert len(last_history_entry["content"]) < 30000


def test_midturn_overflow_recovery():
    with tempfile.TemporaryDirectory() as temp_dir:
        cfg = HarnessConfig(state_dir=temp_dir, max_context_tokens=1000)
        session = ConversationalSession(cfg)
        session.pilot = MockPilotWithOverflow("Clean final response")
        
        # Populate history with enough messages to compact
        for i in range(10):
            session._history.append({"role": "user", "content": f"User msg {i}: " + ("A" * 150)})
            session._history.append({"role": "assistant", "content": f"Assistant msg {i}: " + ("B" * 150)})
        
        events = list(session.send("Solve the issue"))
        
        # Verify that compaction was triggered
        compaction_events = [ev for ev in events if ev.kind in ("compacting", "compaction")]
        assert len(compaction_events) > 0
        
        # Verify the turn completed successfully
        done_events = [ev for ev in events if ev.kind == "assistant_done"]
        assert len(done_events) == 1
        
        # No error event because recovery handled the context overflow
        error_events = [ev for ev in events if ev.kind == "error"]
        assert len(error_events) == 0
