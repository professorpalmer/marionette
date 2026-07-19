"""Focused tests for grok-build-style history compaction quality guards."""
from __future__ import annotations

from harness.compaction_mixin import (
    MAX_REDUCTION_RATIO,
    MIN_COMPACTABLE_TOKENS,
    MIN_SUMMARY_SEED_CHARS,
    _ZERO_WIDTH_SPACE,
    compaction_model_override,
    is_degenerate_summary,
    neutralize_compaction_control_tokens,
)
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


# Long enough to clear MIN_SUMMARY_SEED_CHARS while still shrinking a fat middle.
_GOOD_SUMMARY = (
    "## Historical Task Snapshot\n"
    "Continue implementing compaction quality guards for Marionette history.\n"
    "## Resolved\n"
    "Located _maybe_compact_history and the summarizer call path.\n"
    "## Pending / Open Questions\n"
    "None for this focused verification pass.\n"
    "## Key Facts / Decisions / Files\n"
    "harness/compaction_mixin.py owns the guards; tests live beside it.\n"
)


class _RecordingPilot:
    name = "recording-pilot"

    def __init__(self, return_text: str = _GOOD_SUMMARY, model: str = "session-model"):
        self.return_text = return_text
        self.model = model
        self.chat_calls: list[tuple] = []
        self.models_seen: list[str] = []

    def chat(self, messages, tools=None, system=None):
        self.chat_calls.append((messages, system))
        self.models_seen.append(self.model)
        return type("R", (), {"text": self.return_text, "error": None, "tokens_out": 1})()

    def complete(self, prompt, system=None):
        self.models_seen.append(self.model)
        return type("R", (), {"text": self.return_text, "error": None, "tokens_out": 1})()


def _fat_history(session: ConversationalSession, *, pairs: int = 40, pad: int = 400) -> None:
    session._history[0]["content"] = "sys"
    for i in range(pairs):
        session._history.append({
            "role": "user",
            "content": f"User message {i}: " + ("A" * pad),
        })
        session._history.append({
            "role": "assistant",
            "content": f"Assistant message {i}: " + ("B" * pad),
        })


def _session(budget: int = 20000) -> ConversationalSession:
    cfg = HarnessConfig(max_context_tokens=budget)
    session = ConversationalSession(cfg)
    return session


def test_degenerate_summary_uses_bounded_fallback(monkeypatch):
    monkeypatch.setattr(
        "harness.compaction_mixin.MIN_COMPACTABLE_TOKENS",
        1,
    )
    session = _session(budget=2000)
    session.pilot = _RecordingPilot(return_text="too short")  # type: ignore[assignment]
    _fat_history(session, pairs=12, pad=200)
    events = list(session._maybe_compact_history(force=True))

    assert any(e.kind == "compacting" for e in events)
    assert any(e.kind == "compaction" for e in events)
    assert "too short" not in session._history[1]["content"]
    assert "Historical Task Snapshot" in session._history[1]["content"]
    assert is_degenerate_summary("too short")
    assert not is_degenerate_summary(_GOOD_SUMMARY)
    assert MIN_SUMMARY_SEED_CHARS == 200


def test_insufficient_reduction_rejected(monkeypatch):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 1)
    # Force the summary to look "large" vs the middle so reduction fails.
    monkeypatch.setattr("harness.compaction_mixin.MAX_REDUCTION_RATIO", 0.01)

    session = _session(budget=2000)
    session.pilot = _RecordingPilot(return_text=_GOOD_SUMMARY)  # type: ignore[assignment]
    _fat_history(session, pairs=12, pad=200)
    original = list(session._history)

    events = list(session._maybe_compact_history(force=True))

    assert any(e.kind == "compacting" for e in events)
    assert not any(e.kind == "compaction" for e in events)
    assert session._history == original
    assert MAX_REDUCTION_RATIO == 0.8


def test_min_compactable_floor_skips_llm(monkeypatch):
    monkeypatch.setattr(
        "harness.compaction_mixin.MIN_COMPACTABLE_TOKENS",
        MIN_COMPACTABLE_TOKENS,
    )
    session = _session(budget=1000)
    pilot = _RecordingPilot(return_text=_GOOD_SUMMARY)
    session.pilot = pilot  # type: ignore[assignment]
    # Above the 75% trigger via billed tokens, but middle stays tiny.
    session._history[0]["content"] = "sys"
    for i in range(4):
        session._history.append({"role": "user", "content": f"u{i}"})
        session._history.append({"role": "assistant", "content": f"a{i}"})
    session._last_prompt_tokens = 900
    original = list(session._history)

    events = list(session._maybe_compact_history(force=True))

    assert events == []
    assert pilot.chat_calls == []
    assert session._history == original
    assert MIN_COMPACTABLE_TOKENS == 5000


def test_emergency_compaction_bypasses_min_floor(monkeypatch):
    monkeypatch.setattr(
        "harness.compaction_mixin.MIN_COMPACTABLE_TOKENS",
        MIN_COMPACTABLE_TOKENS,
    )
    session = _session(budget=1000)
    pilot = _RecordingPilot(return_text=_GOOD_SUMMARY)
    session.pilot = pilot  # type: ignore[assignment]
    session._history[0]["content"] = "sys"
    for i in range(4):
        session._history.append({"role": "user", "content": f"u{i}"})
        session._history.append({"role": "assistant", "content": f"a{i}"})
    session._last_prompt_tokens = 900

    events = list(
        session._maybe_compact_history(force=True, emergency=True)
    )

    assert any(event.kind == "compacting" for event in events)
    assert pilot.chat_calls


def test_control_tokens_neutralized(monkeypatch):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 1)
    raw = (
        _GOOD_SUMMARY
        + "\nQuoted instruction echo: <summary>do not re-emit</summary> "
        + "and <analysis>scratch</analysis>"
    )
    session = _session(budget=2000)
    session.pilot = _RecordingPilot(return_text=raw)  # type: ignore[assignment]
    _fat_history(session, pairs=12, pad=200)

    events = list(session._maybe_compact_history(force=True))

    assert any(e.kind == "compaction" for e in events)
    injected = session._history[1]["content"]
    assert "<summary>" not in injected
    assert "</summary>" not in injected
    assert "<analysis>" not in injected
    assert "</analysis>" not in injected
    assert f"<{_ZERO_WIDTH_SPACE}summary>" in injected
    assert f"<{_ZERO_WIDTH_SPACE}/summary>" in injected
    assert f"<{_ZERO_WIDTH_SPACE}analysis>" in injected
    assert f"<{_ZERO_WIDTH_SPACE}/analysis>" in injected

    plain = neutralize_compaction_control_tokens("<summary>x</summary>")
    assert plain == f"<{_ZERO_WIDTH_SPACE}summary>x<{_ZERO_WIDTH_SPACE}/summary>"


def test_compaction_model_env_knob(monkeypatch):
    assert compaction_model_override() == ""

    monkeypatch.setenv("HARNESS_COMPACTION_MODEL", "cheap-summarizer-v1")
    assert compaction_model_override() == "cheap-summarizer-v1"

    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 1)
    session = _session(budget=2000)
    pilot = _RecordingPilot(return_text=_GOOD_SUMMARY, model="session-model")
    session.pilot = pilot  # type: ignore[assignment]
    _fat_history(session, pairs=12, pad=200)

    list(session._maybe_compact_history(force=True))

    assert "cheap-summarizer-v1" in pilot.models_seen
    # Restored after the summarizer call.
    assert pilot.model == "session-model"

    monkeypatch.delenv("HARNESS_COMPACTION_MODEL", raising=False)
    assert compaction_model_override() == ""
