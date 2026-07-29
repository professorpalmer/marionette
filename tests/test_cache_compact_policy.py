"""Hermetic tests for HARNESS_CACHE_COMPACT_POLICY (off|defer|refreeze)."""
from __future__ import annotations

import sqlite3
import tempfile
import time
from types import SimpleNamespace

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.history_compaction_journal import (
    EVENT_COMPACT,
    EVENT_COMPACT_DEFERRED,
    EVENT_COMPACT_REFREEZE,
    history_compaction_payload,
    summarize_history_compactions,
)
from pmharness.drivers.prompt_cache import (
    cache_compact_policy,
    prompt_cache_warm_for_session,
)

_GOOD_SUMMARY = (
    "## Historical Task Snapshot\n"
    "Cache compact policy fixture summary past degenerate floor.\n"
    "## Resolved\nCompaction deferred or refroze as expected.\n"
    "## Pending / Open Questions\nNone.\n"
    "## Key Facts / Decisions / Files\ntests/test_cache_compact_policy.py\n"
)


@pytest.fixture(autouse=True)
def _allow_small_fixture_compaction(monkeypatch):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)


class _MockPilot:
    name = "mock"

    def __init__(self, return_text=_GOOD_SUMMARY):
        self.return_text = return_text
        self.chat_calls = []

    def chat(self, messages, tools=None, system=None):
        self.chat_calls.append((messages, system))
        return type(
            "R", (), {"text": self.return_text, "error": "", "tokens_out": 1}
        )()

    def complete(self, prompt, system=None):
        return type(
            "R", (), {"text": self.return_text, "error": "", "tokens_out": 1}
        )()


def _over_limit_session(state_dir: str) -> ConversationalSession:
    cfg = HarnessConfig(max_context_tokens=1000, state_dir=state_dir)
    session = ConversationalSession(cfg)
    session.harness_session_id = "cache-policy"
    session._history[0]["content"] = "sys"
    session.pilot = _MockPilot()  # type: ignore
    for i in range(10):
        session._history.append(
            {"role": "user", "content": f"User {i}: " + ("A" * 150)}
        )
        session._history.append(
            {"role": "assistant", "content": f"Assistant {i}: " + ("B" * 150)}
        )
    assert session._estimate_context_tokens() > 750
    return session


def _mark_warm(session: ConversationalSession) -> None:
    session.config.driver = "anthropic/claude-sonnet-4"
    session._last_prompt_cache_activity_at = time.time() - 30
    session._last_turn_cache_read_tokens = 8000


def test_cache_compact_policy_defaults_off(monkeypatch):
    monkeypatch.delenv("HARNESS_CACHE_COMPACT_POLICY", raising=False)
    assert cache_compact_policy() == "off"


def _cache_warm_session_stub(**attrs: object) -> SimpleNamespace:
    """Minimal session shape for prompt_cache_warm_for_session — no pilot/registry."""
    session = SimpleNamespace(
        config=SimpleNamespace(driver="anthropic/claude-sonnet-4"),
    )
    for key, value in attrs.items():
        setattr(session, key, value)
    return session


def test_prompt_cache_warm_requires_recent_read_and_ttl(monkeypatch):
    monkeypatch.delenv("HARNESS_ANTHROPIC_CACHE_TTL", raising=False)
    now = time.time()

    warm_session = _cache_warm_session_stub(
        _last_prompt_cache_activity_at=now - 60,
        _last_turn_cache_read_tokens=5000,
    )
    ok, detail = prompt_cache_warm_for_session(warm_session)
    assert ok is True
    assert detail["warm_reason"] == "warm"

    expired = _cache_warm_session_stub(
        _last_prompt_cache_activity_at=now - 7200,
        _last_turn_cache_read_tokens=5000,
    )
    ok2, detail2 = prompt_cache_warm_for_session(expired)
    assert ok2 is False
    assert detail2["warm_reason"] == "expired"

    no_read = _cache_warm_session_stub(
        _last_prompt_cache_activity_at=now - 60,
        _last_turn_cache_read_tokens=0,
    )
    ok3, detail3 = prompt_cache_warm_for_session(no_read)
    assert ok3 is False
    assert detail3["warm_reason"] == "no_cache_read"


def test_off_policy_parity_with_warm_cache(monkeypatch):
    monkeypatch.setenv("HARNESS_CACHE_COMPACT_POLICY", "off")
    with tempfile.TemporaryDirectory() as state_dir:
        session = _over_limit_session(state_dir)
        _mark_warm(session)
        before_len = len(session._history)
        events = list(session._maybe_compact_history())
        assert any(e.kind == "compaction" for e in events)
        assert len(session._history) < before_len
        assert session._last_compaction_attempt["reason"] == "ok"


def test_defer_skips_automatic_compaction_when_warm(monkeypatch):
    monkeypatch.setenv("HARNESS_CACHE_COMPACT_POLICY", "defer")
    with tempfile.TemporaryDirectory() as state_dir:
        session = _over_limit_session(state_dir)
        _mark_warm(session)
        before = list(session._history)
        events = list(session._maybe_compact_history())
        assert events == []
        assert session._history == before
        assert session._last_compaction_attempt["reason"] == "cache_deferred"

        summary = summarize_history_compactions(state_dir, "cache-policy")
        assert summary.deferred_count == 1
        assert summary.record_count == 0
        assert summary.cache_bust_tokens == 0
        assert summary.cache_read_tokens == 0
        assert summary.estimated_cost_usd is None

        payload = history_compaction_payload(state_dir, "cache-policy")
        assert payload["history_compaction_deferred"] == 1
        assert "history_compaction_ran" not in payload


def test_defer_compacts_when_cache_cold(monkeypatch):
    monkeypatch.setenv("HARNESS_CACHE_COMPACT_POLICY", "defer")
    with tempfile.TemporaryDirectory() as state_dir:
        session = _over_limit_session(state_dir)
        session.config.driver = "anthropic/claude-sonnet-4"
        session._last_prompt_cache_activity_at = time.time() - 7200
        session._last_turn_cache_read_tokens = 8000
        events = list(session._maybe_compact_history())
        assert any(e.kind == "compaction" for e in events)
        assert session._last_compaction_attempt["reason"] == "ok"


def test_force_and_emergency_bypass_defer(monkeypatch):
    monkeypatch.setenv("HARNESS_CACHE_COMPACT_POLICY", "defer")
    with tempfile.TemporaryDirectory() as state_dir:
        session = _over_limit_session(state_dir)
        _mark_warm(session)

        events_force = list(session._maybe_compact_history(force=True))
        assert any(e.kind == "compaction" for e in events_force)

        session2 = _over_limit_session(state_dir)
        session2.harness_session_id = "cache-policy-2"
        _mark_warm(session2)
        events_emergency = list(session2._maybe_compact_history(emergency=True))
        assert any(e.kind == "compaction" for e in events_emergency)


def test_advisor_now_bypasses_defer(monkeypatch):
    monkeypatch.setenv("HARNESS_CACHE_COMPACT_POLICY", "defer")
    monkeypatch.setenv("HARNESS_ADVISOR_COMPACTION", "1")
    monkeypatch.setattr(
        "harness.memory_layers.latest_layer_snapshot",
        lambda *_args, **_kwargs: {"L0": {"tokens": 2000}},
    )
    monkeypatch.setattr(
        "harness.turn_economy.TurnEconomy.advise_compaction",
        lambda *_args, **_kwargs: {"level": "now"},
    )

    with tempfile.TemporaryDirectory() as state_dir:
        session = _over_limit_session(state_dir)
        _mark_warm(session)
        events = list(session._maybe_compact_history())
        assert any(e.kind == "compaction" for e in events)


def test_refreeze_records_signal_without_fabricated_reads_or_cost(monkeypatch):
    monkeypatch.setenv("HARNESS_CACHE_COMPACT_POLICY", "refreeze")
    with tempfile.TemporaryDirectory() as state_dir:
        session = _over_limit_session(state_dir)
        session._frozen_system_prompt = "frozen-sys"
        events = list(session._maybe_compact_history())
        done = [e for e in events if e.kind == "compaction" and not e.data.get("aborted")]
        assert done
        assert done[0].data.get("refreeze") is True
        assert session._frozen_system_prompt is None

        summary = summarize_history_compactions(state_dir, "cache-policy")
        assert summary.record_count == 1
        assert summary.refreeze_count == 1
        assert summary.cache_read_tokens == 0
        assert summary.estimated_cost_usd is None

        conn = sqlite3.connect(f"{state_dir}/history_compaction.sqlite")
        try:
            kinds = {
                r[0]
                for r in conn.execute("SELECT event_kind FROM compactions").fetchall()
            }
            assert EVENT_COMPACT in kinds
            assert EVENT_COMPACT_REFREEZE in kinds
            refreeze = conn.execute(
                "SELECT cache_read_tokens, estimated_cost_usd, compact_policy "
                "FROM compactions WHERE event_kind = ?",
                (EVENT_COMPACT_REFREEZE,),
            ).fetchone()
            assert refreeze[0] == 0
            assert refreeze[1] is None
            assert refreeze[2] == "refreeze"
        finally:
            conn.close()

        payload = history_compaction_payload(state_dir, "cache-policy")
        assert payload["history_compaction_refreeze"] == 1
        assert "history_compaction_cost_usd" not in payload


def test_deferred_journal_row_has_no_cache_bust(monkeypatch):
    monkeypatch.setenv("HARNESS_CACHE_COMPACT_POLICY", "defer")
    with tempfile.TemporaryDirectory() as state_dir:
        session = _over_limit_session(state_dir)
        _mark_warm(session)
        list(session._maybe_compact_history())

        conn = sqlite3.connect(f"{state_dir}/history_compaction.sqlite")
        try:
            row = conn.execute(
                "SELECT event_kind, cache_bust_tokens, cache_read_tokens, "
                "estimated_cost_usd, summary_preview FROM compactions"
            ).fetchone()
            assert row[0] == EVENT_COMPACT_DEFERRED
            assert row[1] == 0
            assert row[2] == 0
            assert row[3] is None
            assert "last_turn_cache_read_tokens=8000" in (row[4] or "")
        finally:
            conn.close()
