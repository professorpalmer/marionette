import pytest
import tempfile
from unittest.mock import MagicMock

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, ConvEvent

# Clears the production min-compactable floor so small fixture histories still
# exercise the summarizer path; quality-guard coverage lives in
# test_compaction_quality_guards.py.
_GOOD_SUMMARY = (
    "## Historical Task Snapshot\n"
    "Compaction fixture summary with enough seed characters to pass guards.\n"
    "## Resolved\nPrior turns were compacted for the unit test.\n"
    "## Pending / Open Questions\nNone.\n"
    "## Key Facts / Decisions / Files\ntests/test_compaction.py\n"
)


@pytest.fixture(autouse=True)
def _allow_small_fixture_compaction(monkeypatch):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)


class MockDriverResponse:
    def __init__(self, text="", error=None, tokens_out=10):
        self.text = text
        self.error = error
        self.tokens_out = tokens_out


class MockPilot:
    name = "mock"
    def __init__(self, return_text=_GOOD_SUMMARY):
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
    s.pilot = MockPilot(_GOOD_SUMMARY)  # type: ignore
    
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
    assert "Compaction fixture summary" in s._history[1]["content"]
    
    # The recent messages at the end should be preserved verbatim
    # Let's check that the very last message is untouched
    assert s._history[-1]["role"] == "assistant"
    assert s._history[-1]["content"].startswith("Assistant response number 9:")
    
    after_tokens = s._estimate_context_tokens()
    assert after_tokens <= 500


def test_compaction_clears_stale_prompt_token_telemetry():
    """A stale provider prompt-token count must not mask the reduction.

    ``_estimate_context_tokens()`` takes max(real, heuristic); after compaction
    the "real" number still describes the PRE-compaction history, so keeping it
    would report after_tokens == before_tokens and the pressure advisor would
    never clear.
    """
    cfg = HarnessConfig(max_context_tokens=1000)
    s = ConversationalSession(cfg)
    s._history[0]["content"] = "sys"
    s.pilot = MockPilot(_GOOD_SUMMARY)  # type: ignore

    for i in range(10):
        s._history.append({"role": "user", "content": f"User message number {i}: " + ("A" * 150)})
        s._history.append({"role": "assistant", "content": f"Assistant response number {i}: " + ("B" * 150)})

    # Simulate a billed turn that reported the (large) pre-compaction prompt.
    s._last_prompt_tokens = 5000
    assert s._estimate_context_tokens() == 5000

    events = list(s._maybe_compact_history(force=True))
    assert [e.kind for e in events] == ["compacting", "compaction"]

    assert s._last_prompt_tokens == 0
    payload = events[-1].data
    assert payload["before_tokens"] == 5000
    assert payload["after_tokens"] < payload["before_tokens"]
    # The live estimate now reflects the compacted history, not the stale real.
    assert s._estimate_context_tokens() == payload["after_tokens"]


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
    s.pilot = MockPilot(_GOOD_SUMMARY)  # type: ignore
    
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
    s.pilot = MockPilot(_GOOD_SUMMARY)  # type: ignore
    
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


def test_advisory_compact_once_per_user_turn_not_per_tool_step(monkeypatch, tmp_path):
    """Advisory compact runs at the user-turn boundary, not mid tool-loop.

    Prefix-cache hygiene: rewriting history at the start of every pilot step
    busts the prompt prefix. force=True on CONTEXT_OVERFLOW remains available.
    """
    import inspect
    import json

    from pmharness.drivers.openai_compat import DriverResponse

    # Call-site contract: advisory compact is before the step loop; force=True
    # overflow path stays inside the loop.
    src = inspect.getsource(ConversationalSession._send_locked_inner)
    advisory_idx = src.find("yield from self._maybe_compact_history()")
    force_idx = src.find("yield from self._maybe_compact_history(force=True)")
    step_loop_idx = src.find("for step in _step_iter:")
    assert advisory_idx != -1, "advisory _maybe_compact_history() must remain"
    assert force_idx != -1, "CONTEXT_OVERFLOW force=True compact must remain"
    assert step_loop_idx != -1
    assert advisory_idx < step_loop_idx, (
        "advisory compact must run once before the tool-loop step iterator"
    )
    assert force_idx > step_loop_idx, (
        "force=True overflow compact must stay inside the step loop"
    )
    # No per-step advisory call after the loop starts (only force=True).
    after_loop = src[step_loop_idx:]
    assert "yield from self._maybe_compact_history()" not in after_loop.replace(
        "yield from self._maybe_compact_history(force=True)", ""
    )

    class _TwoStepPilot:
        name = "two-step-compact-spy"
        base_url = "https://openrouter.ai/api/v1"

        def __init__(self):
            self.calls = 0

        def chat(self, messages, *, tools=None, system=None):
            self.calls += 1
            if self.calls == 1:
                return DriverResponse(
                    text="",
                    tokens_out=5,
                    latency_ms=1.0,
                    meta={
                        "tool_calls": [
                            {
                                "id": "call_spy_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": "spy.txt"}),
                                },
                            }
                        ],
                        "finish_reason": "tool_calls",
                    },
                )
            return DriverResponse(
                text="done after tool",
                tokens_out=5,
                latency_ms=1.0,
                meta={"tool_calls": [], "finish_reason": "stop"},
            )

        def complete(self, prompt, *, system=None):
            return DriverResponse(text="summary", tokens_out=1, latency_ms=1.0)

    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", "on")
    (tmp_path / "spy.txt").write_text("hello", encoding="utf-8")
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path), repo=str(tmp_path))
    session = ConversationalSession(cfg)
    session.pilot = _TwoStepPilot()

    compact_calls: list[dict] = []
    real_compact = session._maybe_compact_history

    def _spy_compact(force: bool = False):
        compact_calls.append({"force": force})
        yield from real_compact(force=force)

    monkeypatch.setattr(session, "_maybe_compact_history", _spy_compact)

    list(session.send("read spy.txt then finish"))

    assert session.pilot.calls >= 2, "expected a multi-step tool loop"
    advisory = [c for c in compact_calls if not c["force"]]
    assert len(advisory) == 1, (
        f"advisory compact must run once per user turn, got {compact_calls!r}"
    )

    # force=True path remains callable (overflow last resort).
    list(session._maybe_compact_history(force=True))
    assert any(c["force"] for c in compact_calls)
