"""Persist fail-until + skip automatic compact when the transcript is idle."""
from __future__ import annotations

import json
import shutil
import tempfile
import time

import pytest

from harness.compaction_mixin import (
    ANTI_THRASH_STRIKES,
    MIN_EFFECTIVE_SAVINGS_RATIO,
    REASON_IDLE_UNGROWN,
    REASON_OK,
)
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.history_compaction_journal import (
    fingerprint_transcript,
    load_compaction_session_state,
)


@pytest.fixture(autouse=True)
def _allow_small_fixture_compaction(monkeypatch):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "catalog")
    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", "off")
    monkeypatch.setenv("HARNESS_CACHE_COMPACT_POLICY", "off")


_OK_SUMMARY = (
    "## Historical Task Snapshot\n"
    "Fat fixture history compacted past the degenerate-char floor for tests.\n"
    "## Resolved\nIdle-ungrown skip and fail-until persist were exercised.\n"
    "## Pending / Open Questions\nNone.\n"
    "## Key Facts / Decisions / Files\ntests/test_compaction_idle_ungrown.py\n"
)


class _OkPilot:
    name = "ok-pilot"
    base_url = "http://localhost:11434/v1"

    def __init__(self):
        self.calls = 0

    def complete(self, prompt, *, system=None):
        self.calls += 1
        from pmharness.drivers.openai_compat import DriverResponse
        return DriverResponse(text=_OK_SUMMARY, tokens_out=5, latency_ms=1.0)

    def chat(self, messages, tools=None, system=None):
        return self.complete("", system=system)


def _fat_session(monkeypatch, pilot=None, *, cooldown_s="120"):
    monkeypatch.setenv("HARNESS_COMPACTION_TIMEOUT_S", "30")
    monkeypatch.setenv("HARNESS_COMPACTION_COOLDOWN_S", cooldown_s)
    temp_dir = tempfile.mkdtemp()
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=temp_dir)
    cfg.repo = temp_dir
    cfg.max_context_tokens = 800
    session = ConversationalSession(cfg)
    if pilot is not None:
        session.pilot = pilot
    session._history = [{"role": "system", "content": "sys"}]
    for i in range(20):
        session._history.append({
            "role": "user",
            "content": f"msg {i} " + ("x" * 400),
        })
        session._history.append({
            "role": "assistant",
            "content": json.dumps({"say": f"a{i}", "actions": []}),
        })
    return session, temp_dir


def test_fail_until_restored_across_new_session(monkeypatch):
    """Anti-thrash trip persists fail-until; a new session object restores it."""
    session, temp_dir = _fat_session(monkeypatch, _OkPilot())
    try:
        for _ in range(ANTI_THRASH_STRIKES):
            pct, strikes = session._note_compaction_effectiveness(
                before_tokens=10_000,
                after_tokens=9_500,
            )
            assert pct < MIN_EFFECTIVE_SAVINGS_RATIO
        assert strikes >= ANTI_THRASH_STRIKES
        until = float(session._compaction_fail_until)
        assert until > time.time()
        persisted = load_compaction_session_state(temp_dir, "default")
        assert persisted.fail_until == pytest.approx(until)

        cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=temp_dir)
        cfg.repo = temp_dir
        restored = ConversationalSession(cfg)
        assert restored._compaction_fail_until == pytest.approx(until)
    finally:
        shutil.rmtree(temp_dir)


def test_expired_fail_until_restores_as_zero(monkeypatch):
    from harness.history_compaction_journal import save_compaction_session_state

    session, temp_dir = _fat_session(monkeypatch, _OkPilot())
    try:
        save_compaction_session_state(
            temp_dir, "default", fail_until=time.time() - 30
        )
        cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=temp_dir)
        cfg.repo = temp_dir
        restored = ConversationalSession(cfg)
        assert float(restored._compaction_fail_until or 0.0) == 0.0
    finally:
        shutil.rmtree(temp_dir)


def test_auto_compact_skipped_when_fingerprint_unchanged(monkeypatch):
    pilot = _OkPilot()
    session, temp_dir = _fat_session(monkeypatch, pilot)
    try:
        events = list(session._maybe_compact_history(force=True))
        assert any(e.kind == "compaction" and not e.data.get("aborted") for e in events)
        assert session._last_compaction_attempt.get("reason") == REASON_OK
        after = list(session._history)
        fp, n = fingerprint_transcript(after)
        persisted = load_compaction_session_state(temp_dir, "default")
        assert persisted.transcript_fp == fp
        assert persisted.transcript_len == n

        skipped = list(session._maybe_compact_history(force=False))
        assert skipped == []
        assert session._last_compaction_attempt.get("reason") == REASON_IDLE_UNGROWN
        assert session._history == after
    finally:
        shutil.rmtree(temp_dir)


def test_force_still_runs_when_fingerprint_unchanged(monkeypatch):
    pilot = _OkPilot()
    session, temp_dir = _fat_session(monkeypatch, pilot)
    try:
        list(session._maybe_compact_history(force=True))
        assert session._last_compaction_attempt.get("reason") == REASON_OK
        frozen = list(session._history)

        events = list(session._maybe_compact_history(force=True))
        assert session._last_compaction_attempt.get("reason") != REASON_IDLE_UNGROWN
        assert any(e.kind in ("compacting", "compaction") for e in events)
        # Force entered the compact path; history may rewrite again or stay.
        assert isinstance(session._history, list)
        assert frozen[0]["role"] == "system"
    finally:
        shutil.rmtree(temp_dir)


def test_emergency_bypasses_idle_ungrown(monkeypatch):
    session, temp_dir = _fat_session(monkeypatch, _OkPilot())
    try:
        list(session._maybe_compact_history(force=True))
        assert session._last_compaction_attempt.get("reason") == REASON_OK

        list(session._maybe_compact_history(force=False, emergency=True))
        # Emergency skips the idle-ungrown gate; it may still stop at
        # below_trigger because that check only yields to force / advisor-now.
        assert session._last_compaction_attempt.get("reason") != REASON_IDLE_UNGROWN
    finally:
        shutil.rmtree(temp_dir)


def test_grown_transcript_is_not_skipped(monkeypatch):
    session, temp_dir = _fat_session(monkeypatch, _OkPilot())
    try:
        list(session._maybe_compact_history(force=True))
        assert session._last_compaction_attempt.get("reason") == REASON_OK
        before_fp, _ = fingerprint_transcript(session._history)

        for i in range(12):
            session._history.append({
                "role": "user",
                "content": f"grown {i} " + ("y" * 400),
            })
            session._history.append({
                "role": "assistant",
                "content": json.dumps({"say": f"g{i}", "actions": []}),
            })
        after_fp, _ = fingerprint_transcript(session._history)
        assert after_fp != before_fp

        list(session._maybe_compact_history(force=False))
        assert session._last_compaction_attempt.get("reason") != REASON_IDLE_UNGROWN
    finally:
        shutil.rmtree(temp_dir)
