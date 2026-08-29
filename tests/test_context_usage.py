"""Tests for REAL-preferring live context estimation.

These mirror test_compaction.py::test_estimate_context_tokens_grows and
::test_context_usage, but focus on the behavior added so the compaction
trigger + composer % meter track the driver's actual billed prompt tokens
(self._last_prompt_tokens) instead of the chars//4 heuristic, while keeping
the offline-safe chars//4 fallback intact.
"""

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession

_GOOD_SUMMARY = (
    "## Historical Task Snapshot\n"
    "Context-usage fixture summary seeded past the degenerate-char floor.\n"
    "## Resolved\nTrigger path exercised.\n"
    "## Pending / Open Questions\nNone.\n"
    "## Key Facts / Decisions / Files\ntests/test_context_usage.py\n"
)


@pytest.fixture(autouse=True)
def _allow_small_fixture_compaction(monkeypatch):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)


class MockDriverResponse:
    def __init__(self, text="", error=None, tokens_out=10, tokens_in=0, meta=None):
        self.text = text
        self.error = error
        self.tokens_out = tokens_out
        self.tokens_in = tokens_in
        self.meta = meta or {}


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


def _session(budget=1000):
    cfg = HarnessConfig(max_context_tokens=budget)
    s = ConversationalSession(cfg)
    s._history[0]["content"] = "sys"
    return s


def test_estimate_falls_back_to_heuristic_when_no_real_usage():
    """Offline / pre-first-turn: no real prompt tokens recorded -> chars//4."""
    s = _session()
    # No driver usage yet.
    assert s._last_prompt_tokens == 0

    s._history.append({"role": "user", "content": "A" * 400})
    heuristic = s._estimate_context_tokens_for_list(s._history)
    # With no real usage the live estimate must equal the pure heuristic.
    assert s._estimate_context_tokens() == heuristic


def test_estimate_prefers_real_when_it_exceeds_heuristic():
    """A real prompt-token count larger than the heuristic wins (never under-count)."""
    s = _session()
    s._history.append({"role": "user", "content": "A" * 40})  # ~10 tokens heuristic
    heuristic = s._estimate_context_tokens_for_list(s._history)

    # Simulate the driver billing a much larger real prompt (denser tokenization).
    s._last_prompt_tokens = heuristic + 5000
    assert s._estimate_context_tokens() == heuristic + 5000
    assert s._estimate_context_tokens() > heuristic


def test_estimate_adds_growth_without_letting_heuristic_win():
    """Billed usage is the clock. Growth since the sample is added; the
    full chars//4 walk must not replace the real count via max()."""
    s = _session()
    s._history.append({"role": "assistant", "content": "B" * 800})
    baseline = s._estimate_context_tokens_for_list(s._history)
    s._last_prompt_tokens = 20
    s._last_prompt_heuristic = baseline
    s._history.append({"role": "assistant", "content": "B" * 8000})
    heuristic = s._estimate_context_tokens_for_list(s._history)
    assert heuristic > 20
    grown = s._estimate_context_tokens()
    assert grown == 20 + (heuristic - baseline)
    assert grown < heuristic


def test_compaction_trigger_fires_on_real_value(monkeypatch):
    """The 75% trigger must fire based on the larger (real) value even when the
    chars//4 heuristic alone is well under the trigger."""
    # Tiny middle cannot clear degenerate/reduction floors; relax those so this
    # test stays focused on the real-token trigger.
    monkeypatch.setattr("harness.compaction_mixin.MIN_SUMMARY_SEED_CHARS", 1)
    monkeypatch.setattr("harness.compaction_mixin.MAX_REDUCTION_RATIO", 1000.0)

    s = _session(budget=1000)  # trigger = 750
    s.pilot = MockPilot("Fixed mock summary")  # type: ignore

    # A tiny history: heuristic is far below the 750 trigger.
    for i in range(4):
        s._history.append({"role": "user", "content": f"m{i}"})
        s._history.append({"role": "assistant", "content": f"r{i}"})
    heuristic = s._estimate_context_tokens_for_list(s._history)
    assert heuristic < 750  # would NOT compact on the heuristic alone

    # But the driver reported a real prompt size above the trigger.
    s._last_prompt_tokens = 900
    assert s._estimate_context_tokens() >= 750

    events = list(s._maybe_compact_history())
    assert len(events) == 2
    assert events[0].kind == "compacting"
    assert events[1].kind == "compaction"


def test_compaction_does_not_fire_when_both_below_trigger():
    """Sanity: with a small real number AND small heuristic, no compaction."""
    s = _session(budget=10000)  # trigger = 7500
    s.pilot = MockPilot()  # type: ignore
    s._history.append({"role": "user", "content": "short message"})
    s._history.append({"role": "assistant", "content": "short response"})
    s._last_prompt_tokens = 50

    original = list(s._history)
    events = list(s._maybe_compact_history())
    assert len(events) == 0
    assert s._history == original


def test_context_usage_prefers_real_total():
    """The composer % (get_context_usage total) should track the real billed
    prompt tokens when they exceed the heuristic category sum."""
    s = _session(budget=200000)
    s._history.append({"role": "user", "content": "Hello computer"})

    usage_heuristic = s.get_context_usage()
    heuristic_total = usage_heuristic["total"]

    # Record a real prompt size far above the heuristic total.
    s._last_prompt_tokens = heuristic_total + 12345
    usage_real = s.get_context_usage()
    assert usage_real["total"] == heuristic_total + 12345
    assert usage_real["total"] > heuristic_total


def test_context_usage_exposes_compaction_advice(monkeypatch):
    session = _session(budget=200000)
    monkeypatch.setattr(
        "harness.turn_economy.TurnEconomy.advise_compaction",
        lambda *_args, **_kwargs: {
            "compaction_advice": {
                "level": "soon",
                "needs_intervention": True,
                "budget_kind": "absolute",
                "budget_tokens": 120000,
            }
        },
    )

    assert session.get_context_usage()["compaction_advice"] == {
        "level": "soon",
        "needs_intervention": True,
        "budget_kind": "absolute",
        "budget_tokens": 120000,
    }


def test_context_usage_limit_follows_live_driver_when_unpinned(monkeypatch):
    monkeypatch.setenv("PMHARNESS_OR_LIVE_WINDOWS", "0")
    import pmharness.registry as reg
    monkeypatch.setattr(reg, "_CW_MEM", None, raising=False)
    monkeypatch.setattr(
        "harness.providers.build_pilot",
        lambda driver: MockPilot(),
    )
    monkeypatch.setattr(
        reg,
        "context_window",
        lambda driver, default=200000: 1_000_000 if driver == "glm-5.3" else default,
    )
    cfg = HarnessConfig(driver="glm-5.3", max_context_tokens=128000)
    cfg.max_context_tokens_pinned = False
    s = ConversationalSession(cfg)
    s._history[0]["content"] = "sys"
    usage = s.get_context_usage()
    assert usage["limit"] == 1000000
    assert cfg.max_context_tokens == 1000000


def test_context_usage_falls_back_offline():
    """No real usage -> total is the heuristic category sum (unchanged behavior)."""
    s = _session(budget=5000)
    s._history[0]["content"] = "This is base system content."
    s._history.append({"role": "user", "content": "Hello computer"})
    s._history.append(
        {"role": "user", "content": "Summary of prior discussion", "_compressed_summary": True}
    )
    assert s._last_prompt_tokens == 0
    usage = s.get_context_usage()
    cat_sum = sum(c["tokens"] for c in usage["categories"])
    assert usage["total"] == cat_sum
