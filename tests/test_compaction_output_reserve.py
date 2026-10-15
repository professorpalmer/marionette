"""Automatic compaction leaves room for the request's output ceiling."""

from __future__ import annotations

import pytest

from harness.compaction_mixin import compaction_trigger_tokens
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def test_no_reserve_keeps_75_percent():
    assert compaction_trigger_tokens(128_000) == 96_000
    assert compaction_trigger_tokens(128_000, None) == 96_000
    assert compaction_trigger_tokens(128_000, 0) == 96_000


def test_local_reasoning_ceiling_lowers_trigger():
    # Local pilot with a 40960 reasoning budget sends max_tokens = 40960 + 4096.
    assert compaction_trigger_tokens(128_000, 45_056) == 128_000 - 45_056 - 2_560


def test_small_reserve_on_large_window_keeps_75_percent():
    assert compaction_trigger_tokens(1_000_000, 32_000) == 750_000


def test_reserve_is_capped_at_half_the_window():
    assert compaction_trigger_tokens(32_000, 45_056) == 32_000 - 16_000 - 2_048


class _Pilot:
    name = "reserve-pilot"

    def __init__(self, max_tokens):
        self.max_tokens = max_tokens

    def complete(self, prompt, *, system=None):
        raise AssertionError("no summarizer call expected")


@pytest.mark.parametrize("max_tokens, expect_attempt", [(None, False), (45_056, True)])
def test_session_trigger_uses_pilot_output_ceiling(monkeypatch, max_tokens, expect_attempt):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    s = ConversationalSession(HarnessConfig(max_context_tokens=128_000))
    s._history[0]["content"] = "sys"
    s.pilot = _Pilot(max_tokens)  # type: ignore[assignment]
    monkeypatch.setattr(s, "_estimate_context_tokens", lambda: 85_000)
    calls = []

    def _no_split(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(s, "_choose_compaction_split", _no_split)
    list(s._maybe_compact_history())
    assert bool(calls) is expect_attempt
