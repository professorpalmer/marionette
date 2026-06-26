import pytest
import tempfile
from unittest.mock import MagicMock

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, ConvEvent



class MockDriverResponse:
    def __init__(self, text="", error=None, tokens_out=10):
        self.text = text
        self.error = error
        self.tokens_out = tokens_out


class MockPilot:
    name = "mock"
    def __init__(self, return_text="This is a summary."):
        self.return_text = return_text
        self.chat_calls = []
        self.complete_calls = []

    def chat(self, messages, tools=None, system=None):
        self.chat_calls.append((messages, system))
        return MockDriverResponse(text=self.return_text)

    def complete(self, prompt, system=None):
        self.complete_calls.append((prompt, system))
        return MockDriverResponse(text=self.return_text)


def test_estimate_context_tokens_grows():
    cfg = HarnessConfig(max_context_tokens=1000)
    s = ConversationalSession(cfg)
    s._history[0]["content"] = "sys"
    
    initial = s._estimate_context_tokens()
    
    # Append a small user message
    s._history.append({"role": "user", "content": "hello world"})
    tokens_1 = s._estimate_context_tokens()
    assert tokens_1 > initial
    
    # Append a large message
    s._history.append({"role": "assistant", "content": "A" * 1000})
    tokens_2 = s._estimate_context_tokens()
    assert tokens_2 > tokens_1


def test_maybe_compact_history_below_trigger():
    cfg = HarnessConfig(max_context_tokens=10000)
    s = ConversationalSession(cfg)
    s._history[0]["content"] = "sys"
    s.pilot = MockPilot()  # type: ignore
    
    # Add a couple of messages (approx 100 characters total -> ~25 tokens)
    s._history.append({"role": "user", "content": "short message"})
    s._history.append({"role": "assistant", "content": "short response"})
    
    original_history = list(s._history)
    events = list(s._maybe_compact_history())
    
    assert len(events) == 0
    assert s._history == original_history


def test_maybe_compact_history_above_trigger():
    # Budget = 1000, trigger = 750, target = 500
    cfg = HarnessConfig(max_context_tokens=1000)
    s = ConversationalSession(cfg)
    s._history[0]["content"] = "sys"
    s.pilot = MockPilot("Fixed mock summary")  # type: ignore
    
    # Let's add many large messages so we exceed 750 tokens
    # (need > 3000 chars total)
    for i in range(10):
        s._history.append({"role": "user", "content": f"User message number {i}: " + ("A" * 150)})
        s._history.append({"role": "assistant", "content": f"Assistant response number {i}: " + ("B" * 150)})
        
    before_tokens = s._estimate_context_tokens()
    assert before_tokens > 750
    
    original_system = s._history[0]["content"]
    
    events = list(s._maybe_compact_history())
    assert len(events) == 2
    assert events[0].kind == "compacting"
    assert events[1].kind == "compaction"
    
    # Verify that the system message is completely unchanged
    assert s._history[0]["role"] == "system"
    assert s._history[0]["content"] == original_system
    
    # The middle block should be replaced by exactly ONE summary message (which is at index 1)
    assert s._history[1]["role"] == "user"
    assert "[Earlier conversation summarized to fit context]" in s._history[1]["content"]
    assert "Fixed mock summary" in s._history[1]["content"]
    
    # The recent messages at the end should be preserved verbatim
    # Let's check that the very last message is untouched
    assert s._history[-1]["role"] == "assistant"
    assert s._history[-1]["content"].startswith("Assistant response number 9:")
    
    after_tokens = s._estimate_context_tokens()
    assert after_tokens <= 500


def test_fallback_truncation_on_pilot_failure():
    cfg = HarnessConfig(max_context_tokens=1500)
    s = ConversationalSession(cfg)
    s._history[0]["content"] = "sys"
    
    # Mock pilot that returns error
    class ErrorPilot:
        name = "mock"
        def chat(self, messages, tools=None, system=None):
            return MockDriverResponse(error="Simulated LLM error")
            
    s.pilot = ErrorPilot()  # type: ignore
    
    for i in range(24):
        s._history.append({"role": "user", "content": f"Msg {i}: " + ("A" * 200)})
        
    before_tokens = s._estimate_context_tokens()
    assert before_tokens > 1125
    
    events = list(s._maybe_compact_history())
    assert len(events) == 2
    assert events[0].kind == "compacting"
    assert events[1].kind == "compaction"
    
    # Verify we compacted and didn't crash
    assert s._history[1]["role"] == "user"
    assert "[Earlier conversation summarized to fit context]" in s._history[1]["content"]
    # Fallback should keep first 2 and last 2 of the old block + note
    assert "Msg 0:" in s._history[1]["content"]
    assert "Msg 1:" in s._history[1]["content"]
    assert "were elided here" in s._history[1]["content"]
    
    after_tokens = s._estimate_context_tokens()
    assert after_tokens <= 750


def test_no_orphaned_tool_messages_in_kept_window():
    cfg = HarnessConfig(max_context_tokens=1000)
    s = ConversationalSession(cfg)
    s.pilot = MockPilot("Summary block")  # type: ignore
    
    # Setup history where initial split_idx = 4
    # Total messages: 10
    # recent_count = 6
    # split_idx = 10 - 6 = 4.
    # We want total tokens to be > 750, so let's make Msg 1 and Msg 7 very large.
    s._history = [
        {"role": "system", "content": "sys"}, # Msg 0
        {"role": "user", "content": "A" * 1500}, # Msg 1 (large)
        {"role": "user", "content": "init 2"}, # Msg 2
        {"role": "assistant", "content": "call", "tool_calls": [{"id": "call_123", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]}, # Msg 3
        {"role": "user", "content": "intervening message"}, # Msg 4
        {"role": "tool", "tool_call_id": "call_123", "content": "result"}, # Msg 5
        {"role": "assistant", "content": "response to result"}, # Msg 6
        {"role": "user", "content": "B" * 1500}, # Msg 7 (large)
        {"role": "user", "content": "B" * 200}, # Msg 8
        {"role": "assistant", "content": "final response"}, # Msg 9
    ]
    
    # Verify token size is above trigger before calling
    assert s._estimate_context_tokens() > 750
    
    # Let's perform compaction.
    events = list(s._maybe_compact_history())
    
    # The tool result at Msg 5 is orphaned if Msg 3 is summarized but Msg 5 is kept.
    # The safety split should force split_idx to advance past Msg 5 (to split_idx = 6).
    # Thus, Msg 5 is in the middle block (summarized), and the kept tail starts at Msg 6.
    # Therefore, there should be no tool message for "call_123" in the kept tail.
    # Let's verify the kept window (which is s._history[2:]) contains no tool message with id "call_123".
    kept_window = s._history[2:]
    for m in kept_window:
        if m.get("role") == "tool":
            assert m.get("tool_call_id") != "call_123"


def test_single_writer_synchronous_compaction():
    cfg = HarnessConfig(max_context_tokens=1000)
    s = ConversationalSession(cfg)
    s._history[0]["content"] = "sys"
    s.pilot = MockPilot("Sync compact")  # type: ignore
    
    # Add large history (> 3000 chars to trigger compaction)
    for i in range(10):
        s._history.append({"role": "user", "content": "C" * 300})
        
    original_id = id(s._history)
    
    # Compacting directly mutates self._history in-place (same list identity)
    list(s._maybe_compact_history())
    
    assert id(s._history) == original_id
    assert len(s._history) < 11


def test_context_usage():
    cfg = HarnessConfig(max_context_tokens=5000)
    s = ConversationalSession(cfg)
    s._history[0]["content"] = "This is base system content."
    
    # Let's add some simulated conversation turns
    s._history.append({"role": "user", "content": "Hello computer"})
    s._history.append({"role": "user", "content": "Summary of prior discussion", "_compressed_summary": True})
    
    usage = s.get_context_usage()
    assert isinstance(usage, dict)
    assert "total" in usage
    assert "limit" in usage
    assert "categories" in usage
    assert usage["limit"] == 5000
    
    # Check that we have the 8 requested categories
    cats = {c["name"]: c["tokens"] for c in usage["categories"]}
    expected_keys = [
        "System prompt", "Tool definitions", "Rules", "Skills", 
        "MCP", "Subagent", "Summarized conversation", "Conversation"
    ]
    for key in expected_keys:
        assert key in cats
        
    assert cats["Summarized conversation"] > 0
    assert cats["Conversation"] > 0
    assert cats["Subagent"] == 0
