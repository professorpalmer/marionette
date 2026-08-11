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
    assert "## Historical Task Snapshot" in s._history[1]["content"]
    
    after_tokens = s._estimate_context_tokens()
    assert after_tokens <= 750


def test_short_history_huge_prior_summary_compacts(monkeypatch):
    """sys + huge prior summary + few recent turns must re-compact (not early-return)."""
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    cfg = HarnessConfig(max_context_tokens=4000)
    s = ConversationalSession(cfg)
    s.pilot = MockPilot(_GOOD_SUMMARY)  # type: ignore
    s._history = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": "[Earlier conversation summarized to fit context]\n" + ("OLD " * 4000),
            "_compressed_summary": True,
        },
        {"role": "user", "content": "latest ask " + ("x" * 200)},
        {"role": "assistant", "content": "latest answer " + ("y" * 200)},
    ]
    before = s._estimate_context_tokens()
    assert before > int(4000 * 0.75)

    events = list(s._maybe_compact_history(force=True))
    assert [e.kind for e in events] == ["compacting", "compaction"]
    assert s._history[0]["role"] == "system"
    assert s._history[1].get("_compressed_summary") is True
    assert "Compaction fixture summary" in s._history[1]["content"]
    # Nested prior-summary wrappers must not grow unbounded.
    assert s._history[1]["content"].count("PREVIOUS HISTORICAL CONVERSATION SUMMARY:") <= 1
    after = s._estimate_context_tokens()
    assert after < before
    assert s._last_compaction_attempt.get("reason") == "ok"


def test_adaptive_tail_shrinks_when_six_recent_exceed_budget(monkeypatch):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    cfg = HarnessConfig(max_context_tokens=2000)
    s = ConversationalSession(cfg)
    s.pilot = MockPilot(_GOOD_SUMMARY)  # type: ignore
    # sys + prior summary + six huge recent messages (preferred fixed-6 would noop).
    s._history = [{"role": "system", "content": "sys"}]
    s._history.append({
        "role": "user",
        "content": "[Earlier conversation summarized to fit context]\n" + ("SUM " * 500),
        "_compressed_summary": True,
    })
    for i in range(6):
        role = "user" if i % 2 == 0 else "assistant"
        s._history.append({"role": role, "content": f"turn-{i} " + ("Z" * 2500)})

    before = s._estimate_context_tokens()
    events = list(s._maybe_compact_history(force=True))
    assert [e.kind for e in events] == ["compacting", "compaction"]
    after = s._estimate_context_tokens()
    assert after < before
    # Kept recent window must be smaller than the preferred six when they blow the budget.
    assert len(s._history) < 8
    assert s._history[-1]["content"].startswith("turn-5")


def test_adaptive_tail_keeps_tool_pairs_intact(monkeypatch):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    cfg = HarnessConfig(max_context_tokens=2000)
    s = ConversationalSession(cfg)
    s.pilot = MockPilot(_GOOD_SUMMARY)  # type: ignore
    s._history = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": "[Earlier conversation summarized to fit context]\n" + ("P" * 4000),
            "_compressed_summary": True,
        },
        {"role": "user", "content": "old " + ("A" * 2000)},
        {"role": "assistant", "content": "old-a " + ("B" * 2000)},
        {"role": "user", "content": "please read"},
        {
            "role": "assistant",
            "content": "calling",
            "tool_calls": [{
                "id": "call_keep",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_keep", "content": "tool body " + ("T" * 500)},
        {"role": "assistant", "content": "done with tool"},
    ]
    events = list(s._maybe_compact_history(force=True))
    assert any(e.kind == "compaction" for e in events)
    kept = s._history[2:]
    tool_ids_in_kept = {
        m.get("tool_call_id") for m in kept if m.get("role") == "tool"
    }
    assistant_call_ids = set()
    for m in kept:
        for tc in m.get("tool_calls") or []:
            if tc.get("id"):
                assistant_call_ids.add(tc["id"])
    # No orphaned tool results in the kept window.
    assert tool_ids_in_kept <= assistant_call_ids


def test_fallback_bounds_few_huge_messages(monkeypatch):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    cfg = HarnessConfig(max_context_tokens=3000)
    s = ConversationalSession(cfg)

    class ErrorPilot:
        name = "mock"
        def chat(self, messages, tools=None, system=None):
            return MockDriverResponse(error="boom")

    s.pilot = ErrorPilot()  # type: ignore
    huge = "H" * 20000
    s._history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "one " + huge},
        {"role": "assistant", "content": "two " + huge},
        {"role": "user", "content": "three " + huge},
        {"role": "assistant", "content": "four " + huge},
        {"role": "user", "content": "keep me recent"},
        {"role": "assistant", "content": "keep me too"},
    ]
    before = s._estimate_context_tokens()
    events = list(s._maybe_compact_history(force=True))
    assert [e.kind for e in events] == ["compacting", "compaction"]
    injected = s._history[1]["content"]
    assert "elided" in injected.lower()
    assert "## Historical Task Snapshot" in injected
    assert len(injected) < 20000
    assert s._estimate_context_tokens() < before


def test_repeated_compaction_can_reduce_again(monkeypatch):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    cfg = HarnessConfig(max_context_tokens=4000)
    s = ConversationalSession(cfg)
    s.pilot = MockPilot(_GOOD_SUMMARY)  # type: ignore
    s._history = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": "[Earlier conversation summarized to fit context]\n" + ("PRIOR " * 3000),
            "_compressed_summary": True,
        },
    ]
    for i in range(8):
        s._history.append({"role": "user", "content": f"u{i} " + ("A" * 800)})
        s._history.append({"role": "assistant", "content": f"a{i} " + ("B" * 800)})

    first = list(s._maybe_compact_history(force=True))
    assert any(e.kind == "compaction" for e in first)
    # Grow again after first compaction so a second pass has work to do.
    for i in range(6):
        s._history.append({"role": "user", "content": f"more-u{i} " + ("C" * 1200)})
        s._history.append({"role": "assistant", "content": f"more-a{i} " + ("D" * 1200)})
    before_second = s._estimate_context_tokens()
    second = list(s._maybe_compact_history(force=True))
    assert any(e.kind == "compaction" for e in second)
    assert s._estimate_context_tokens() < before_second
    assert s._last_compaction_attempt.get("reason") == "ok"


def test_true_no_compactable_sets_reason(monkeypatch):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    cfg = HarnessConfig(max_context_tokens=10000)
    s = ConversationalSession(cfg)
    s.pilot = MockPilot(_GOOD_SUMMARY)  # type: ignore
    # Only system — nothing to compact.
    s._history = [{"role": "system", "content": "sys"}]
    events = list(s._maybe_compact_history(force=True))
    assert events == []
    assert s._last_compaction_attempt.get("reason") == "no_compactable_history"


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
    force_idx = src.find("emergency=True")
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
    assert "yield from self._maybe_compact_history()" not in after_loop

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


def test_million_token_window_keeps_bounded_tail_and_reduces_large_session():
    cfg = HarnessConfig(max_context_tokens=1_048_576)
    session = ConversationalSession(cfg)
    session.pilot = MockPilot(_GOOD_SUMMARY)  # type: ignore
    session._history[0]["content"] = "system"
    # About 215k heuristic tokens. The old 25%-of-window tail (262k) selected
    # only a tiny middle and returned summary_rejected.
    for i in range(62):
        session._history.append({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"turn {i}\n" + ("x" * 13_850),
        })

    before = session._estimate_context_tokens()
    assert 200_000 < before < 240_000
    events = list(session._maybe_compact_history(force=True))

    assert [event.kind for event in events] == ["compacting", "compaction"]
    assert session._estimate_context_tokens() < before * 0.5
    assert session._history[-1]["content"].startswith("turn 61")


def test_degenerate_model_summary_uses_deterministic_fallback():
    cfg = HarnessConfig(max_context_tokens=1_000)
    session = ConversationalSession(cfg)
    session.pilot = MockPilot("I cannot summarize this conversation.")  # type: ignore
    session._history[0]["content"] = "system"
    for i in range(16):
        session._history.append({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"important decision {i}: " + ("z" * 180),
        })

    events = list(session._maybe_compact_history(force=True))

    assert [event.kind for event in events] == ["compacting", "compaction"]
    summary = session._history[1]["content"]
    assert "I cannot summarize" not in summary
    assert "Historical Task Snapshot" in summary


def test_advisor_soon_does_not_bypass_hard_trigger(monkeypatch):
    """``soon`` warns; only ``now`` may auto-compact below the 75% trigger."""
    monkeypatch.setenv("HARNESS_ADVISOR_COMPACTION", "1")
    monkeypatch.setattr(
        "harness.memory_layers.latest_layer_snapshot",
        lambda *_args, **_kwargs: {"L0": {"tokens": 2000}},
    )
    cfg = HarnessConfig(max_context_tokens=20_000)
    session = ConversationalSession(cfg)
    session.pilot = MockPilot(_GOOD_SUMMARY)  # type: ignore
    monkeypatch.setattr(
        "harness.turn_economy.TurnEconomy.advise_compaction",
        lambda *_args, **_kwargs: {"level": "soon"},
    )
    session._history[0]["content"] = "system"
    for i in range(80):
        session._history.append({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"turn {i}: " + ("q" * 500),
        })
    assert session._estimate_context_tokens() < 15_000

    events = list(session._maybe_compact_history())

    assert events == []


def test_advisor_now_triggers_safe_boundary_compaction(monkeypatch):
    monkeypatch.setenv("HARNESS_ADVISOR_COMPACTION", "1")
    monkeypatch.setattr(
        "harness.memory_layers.latest_layer_snapshot",
        lambda *_args, **_kwargs: {"L0": {"tokens": 2000}},
    )
    cfg = HarnessConfig(max_context_tokens=20_000)
    session = ConversationalSession(cfg)
    session.pilot = MockPilot(_GOOD_SUMMARY)  # type: ignore
    monkeypatch.setattr(
        "harness.turn_economy.TurnEconomy.advise_compaction",
        lambda *_args, **_kwargs: {"level": "now"},
    )
    session._history[0]["content"] = "system"
    for i in range(80):
        session._history.append({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"turn {i}: " + ("q" * 500),
        })
    assert session._estimate_context_tokens() < 15_000

    events = list(session._maybe_compact_history())

    assert [event.kind for event in events] == ["compacting", "compaction"]


def test_pre_summarizer_tool_prune_keeps_head_and_tail(monkeypatch, tmp_path):
    """Tool-body prune for the summarizer must be head+tail (not head-only)."""
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    cfg = HarnessConfig(max_context_tokens=4000, state_dir=str(tmp_path))
    session = ConversationalSession(cfg)
    session.harness_session_id = "sess-compact"
    session.pilot = MockPilot(_GOOD_SUMMARY)  # type: ignore

    # Unique tail marker past a head-only 1000-char cut.
    tool_body = ("HEADMARKER-" + ("A" * 900) + "-MID-" + ("B" * 900) + "-TAIL_ERROR_XYZ")
    assert "TAIL_ERROR_XYZ" not in tool_body[:1000]

    session._history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "please run " + ("u" * 800)},
        {
            "role": "assistant",
            "content": "calling",
            "tool_calls": [{
                "id": "call_tail",
                "type": "function",
                "function": {"name": "run_command", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_tail", "content": tool_body},
        {"role": "assistant", "content": "saw the error " + ("a" * 800)},
        {"role": "user", "content": "keep recent " + ("r" * 200)},
        {"role": "assistant", "content": "kept " + ("k" * 200)},
    ]

    events = list(session._maybe_compact_history(force=True))
    assert any(e.kind == "compaction" for e in events)
    assert session.pilot.chat_calls
    summarizer_input = session.pilot.chat_calls[0][0][0]["content"]
    assert "HEADMARKER-" in summarizer_input
    assert "TAIL_ERROR_XYZ" in summarizer_input
    assert "truncated" in summarizer_input.lower()
    assert "middle elided" in summarizer_input or "spill://" in summarizer_input


def test_pre_summarizer_registers_spill_pointer(monkeypatch, tmp_path):
    """Truncating for summary must register/retain a spill:// handle."""
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    # Below-floor bodies would skip offload; force the gate open for this unit.
    monkeypatch.setattr("harness.offload_policy.should_offload", lambda *_a, **_k: True)
    cfg = HarnessConfig(max_context_tokens=4000, state_dir=str(tmp_path))
    session = ConversationalSession(cfg)
    session.harness_session_id = "sess-spill"
    session.pilot = MockPilot(_GOOD_SUMMARY)  # type: ignore

    tool_body = "START-" + ("X" * 5000) + "-END_UNIQUE_42"
    session._history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "run tool " + ("u" * 800)},
        {
            "role": "assistant",
            "content": "calling",
            "tool_calls": [{
                "id": "call_spill",
                "type": "function",
                "function": {"name": "run_command", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_spill", "content": tool_body},
        {"role": "assistant", "content": "done " + ("a" * 800)},
        {"role": "user", "content": "recent " + ("r" * 200)},
        {"role": "assistant", "content": "ok " + ("k" * 200)},
    ]

    list(session._maybe_compact_history(force=True))
    summarizer_input = session.pilot.chat_calls[0][0][0]["content"]
    assert "spill://sess-spill/call_spill" in summarizer_input
    assert "END_UNIQUE_42" in summarizer_input  # tail retained


def test_pre_summarizer_retains_existing_spill_uri(monkeypatch, tmp_path):
    """Already-persisted tool bodies keep their spill:// through summary prune."""
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    cfg = HarnessConfig(max_context_tokens=4000, state_dir=str(tmp_path))
    session = ConversationalSession(cfg)
    session.harness_session_id = "sess-keep"
    session.pilot = MockPilot(_GOOD_SUMMARY)  # type: ignore

    persisted = (
        "<persisted-output>\n"
        "This tool result was too large.\n"
        "Also addressable as: spill://sess-keep/call_keep "
        "(read_file works on this URI)\n"
        "Preview (head and tail):\n"
        + ("P" * 2000)
        + "\n</persisted-output>"
    )
    session._history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "run " + ("u" * 800)},
        {
            "role": "assistant",
            "content": "calling",
            "tool_calls": [{
                "id": "call_keep",
                "type": "function",
                "function": {"name": "run_command", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_keep", "content": persisted},
        {"role": "assistant", "content": "done " + ("a" * 800)},
        {"role": "user", "content": "recent " + ("r" * 200)},
        {"role": "assistant", "content": "ok " + ("k" * 200)},
    ]

    list(session._maybe_compact_history(force=True))
    summarizer_input = session.pilot.chat_calls[0][0][0]["content"]
    assert "spill://sess-keep/call_keep" in summarizer_input


def test_fallback_uses_pruned_middle_not_raw_tool_flood(monkeypatch, tmp_path):
    """Fallback summarizer must not format unpruned tool bodies."""
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    cfg = HarnessConfig(max_context_tokens=12_000, state_dir=str(tmp_path))
    session = ConversationalSession(cfg)
    session.harness_session_id = "sess-fb"

    class ErrorPilot:
        name = "mock"
        def chat(self, messages, tools=None, system=None):
            return MockDriverResponse(error="boom")

    session.pilot = ErrorPilot()  # type: ignore
    unique = "FALLBACK_RAW_FLOOD_MARKER_99"
    tool_body = ("H" * 2500) + unique + ("T" * 2500)
    # Pad with non-tool turns so pruned middle stays large enough for the
    # reduction guard even after the tool body is head+tail clipped.
    session._history = [{"role": "system", "content": "sys"}]
    for i in range(8):
        session._history.append({
            "role": "user",
            "content": f"pad-{i} " + ("P" * 700),
        })
        session._history.append({
            "role": "assistant",
            "content": f"pad-a-{i} " + ("Q" * 700),
        })
    session._history.extend([
        {"role": "user", "content": "one " + ("u" * 400)},
        {
            "role": "assistant",
            "content": "calling",
            "tool_calls": [{
                "id": "call_fb",
                "type": "function",
                "function": {"name": "run_command", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_fb", "content": tool_body},
        {"role": "assistant", "content": "two " + ("a" * 400)},
        {"role": "user", "content": "keep recent"},
        {"role": "assistant", "content": "kept"},
    ])

    events = list(session._maybe_compact_history(force=True))
    done = [e for e in events if e.kind == "compaction"]
    assert done and not done[0].data.get("aborted")
    injected = session._history[1]["content"]
    assert session._history[1].get("_compressed_summary") is True
    # Middle-of-tool marker must not survive head+tail prune into the fallback.
    assert unique not in injected
    assert len(injected) < len(tool_body)
    assert "## Historical Task Snapshot" in injected

    # Unit seam: _fallback path formats pruned_middle, not raw middle_block.
    pruned = session._prune_middle_for_summary(
        [
            {"role": "tool", "tool_call_id": "call_fb", "content": tool_body},
        ],
        tool_keep=1000,
        args_keep=500,
    )
    assert unique not in (pruned[0].get("content") or "")
    assert "truncated" in (pruned[0].get("content") or "").lower()


def test_summarizer_input_aggregate_hard_cap(monkeypatch, tmp_path):
    """N large tools must not flood content_to_summarize past the aggregate cap."""
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    monkeypatch.setattr(
        "harness.compaction_mixin.DEFAULT_MAX_SUMMARIZER_INPUT_CHARS",
        4_000,
    )
    monkeypatch.setenv("HARNESS_COMPACTION_MAX_SUMMARIZER_INPUT_CHARS", "4000")
    cfg = HarnessConfig(max_context_tokens=20_000, state_dir=str(tmp_path))
    session = ConversationalSession(cfg)
    session.harness_session_id = "sess-cap"
    session.pilot = MockPilot(_GOOD_SUMMARY)  # type: ignore

    session._history = [{"role": "system", "content": "sys"}]
    for i in range(20):
        session._history.append({"role": "user", "content": f"ask {i} " + ("u" * 100)})
        session._history.append({
            "role": "assistant",
            "content": f"call {i}",
            "tool_calls": [{
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": "run_command", "arguments": "{}"},
            }],
        })
        session._history.append({
            "role": "tool",
            "tool_call_id": f"call_{i}",
            "content": f"TOOL{i}-" + ("Z" * 900) + f"-END{i}",
        })
        session._history.append({"role": "assistant", "content": f"done {i} " + ("a" * 100)})
    # Keep a small recent tail outside the middle.
    session._history.append({"role": "user", "content": "recent ask"})
    session._history.append({"role": "assistant", "content": "recent ans"})

    list(session._maybe_compact_history(force=True))
    assert session.pilot.chat_calls
    summarizer_input = session.pilot.chat_calls[0][0][0]["content"]
    # Cap is 4000; marker may add a little overhead beyond the clip target.
    assert len(summarizer_input) < 6_000
    assert "summarizer input" in summarizer_input or "middle elided" in summarizer_input
