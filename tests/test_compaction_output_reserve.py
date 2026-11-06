"""Automatic compaction leaves room for the request's output ceiling."""

from __future__ import annotations

import pytest

from harness.compaction_mixin import compaction_trigger_tokens
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


_ABSENT = object()


@pytest.mark.parametrize("window, reserve, expected", [
    pytest.param(128_000, _ABSENT, 96_000, id="no-reserve-absent-keeps-75-percent"),
    pytest.param(128_000, None, 96_000, id="no-reserve-none-keeps-75-percent"),
    pytest.param(128_000, 0, 96_000, id="no-reserve-zero-keeps-75-percent"),
    # Local pilot with a 40960 reasoning budget sends max_tokens = 40960 + 4096.
    pytest.param(128_000, 45_056, 128_000 - 45_056 - 2_560, id="local-reasoning-ceiling-lowers-trigger"),
    pytest.param(1_000_000, 32_000, 750_000, id="small-reserve-on-large-window-keeps-75-percent"),
    pytest.param(32_000, 45_056, 32_000 - 16_000 - 2_048, id="reserve-capped-at-half-the-window"),
])
def test_compaction_trigger_tokens(window, reserve, expected):
    if reserve is _ABSENT:
        assert compaction_trigger_tokens(window) == expected
    else:
        assert compaction_trigger_tokens(window, reserve) == expected


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
