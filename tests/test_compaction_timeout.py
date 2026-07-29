"""Compaction summarizer timeout + cooldown."""
from __future__ import annotations

import json
import shutil
import tempfile
import time

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


@pytest.fixture(autouse=True)
def _allow_small_fixture_compaction(monkeypatch):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)


class _HangPilot:
    name = "hang-pilot"
    base_url = "http://localhost:11434/v1"

    def complete(self, prompt, *, system=None):
        # Must exceed the compaction outer wait (~5s) so the timeout path
        # fires; shorter sleeps let complete() finish first and skip cooldown.
        time.sleep(6)
        from pmharness.drivers.openai_compat import DriverResponse
        return DriverResponse(text="never", tokens_out=1, latency_ms=1.0)


_OK_SUMMARY = (
    "## Historical Task Snapshot\n"
    "Fat fixture history compacted past the degenerate-char floor for tests.\n"
    "## Resolved\nAnti-thrash force bypass exercised the summarizer successfully.\n"
    "## Pending / Open Questions\nNone.\n"
    "## Key Facts / Decisions / Files\ntests/test_compaction_timeout.py\n"
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


def _fat_session(monkeypatch, pilot, *, timeout_s="1", cooldown_s="60"):
    monkeypatch.setenv("HARNESS_COMPACTION_TIMEOUT_S", timeout_s)
    monkeypatch.setenv("HARNESS_COMPACTION_COOLDOWN_S", cooldown_s)
    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", "off")
    temp_dir = tempfile.mkdtemp()
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=temp_dir)
    cfg.repo = temp_dir
    cfg.max_context_tokens = 800
    session = ConversationalSession(cfg)
    session.pilot = pilot
    # Inflate history past the 0.75 trigger.
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


def test_compaction_timeout_uses_fallback_and_sets_cooldown(monkeypatch):
    session, temp_dir = _fat_session(monkeypatch, _HangPilot(), timeout_s="1", cooldown_s="90")
    try:
        events = list(session._maybe_compact_history(force=True))
        assert any(e.kind == "compaction" for e in events)
        assert session._compaction_fail_until > time.time()
        # Summary message present
        assert any(m.get("_compressed_summary") for m in session._history)
    finally:
        shutil.rmtree(temp_dir)


def test_compaction_cooldown_skips_llm(monkeypatch):
    """Automatic compaction honors summarizer-fail cooldown (force bypasses it)."""
    pilot = _OkPilot()
    session, temp_dir = _fat_session(monkeypatch, pilot, timeout_s="30", cooldown_s="90")
    try:
        session._compaction_fail_until = time.time() + 60
        list(session._maybe_compact_history(force=False))
        assert pilot.calls == 0
    finally:
        shutil.rmtree(temp_dir)


def test_anti_thrash_blocks_automatic_compaction_after_ineffective_strikes(monkeypatch):
    """Two ineffective reclamations arm shared _compaction_fail_until breaker."""
    from harness.compaction_mixin import (
        ANTI_THRASH_STRIKES,
        MIN_EFFECTIVE_SAVINGS_RATIO,
        REASON_THRASH_COOLDOWN,
    )

    pilot = _OkPilot()
    session, temp_dir = _fat_session(monkeypatch, pilot, timeout_s="30", cooldown_s="120")
    try:
        # Simulate two ineffective passes (<10% savings).
        for _ in range(ANTI_THRASH_STRIKES):
            pct, strikes = session._note_compaction_effectiveness(
                before_tokens=10_000,
                after_tokens=9_500,  # 5% < MIN_EFFECTIVE_SAVINGS_RATIO
            )
            assert pct < MIN_EFFECTIVE_SAVINGS_RATIO
        assert strikes >= ANTI_THRASH_STRIKES
        assert session._compaction_fail_until > time.time()

        events = list(session._maybe_compact_history(force=False))
        assert events == []
        assert session._last_compaction_attempt.get("reason") == REASON_THRASH_COOLDOWN
        assert pilot.calls == 0
    finally:
        shutil.rmtree(temp_dir)


def test_anti_thrash_force_bypasses_and_effective_reset_clears_strikes(monkeypatch):
    pilot = _OkPilot()
    session, temp_dir = _fat_session(monkeypatch, pilot, timeout_s="30", cooldown_s="120")
    try:
        session._compaction_ineffective_count = 2
        session._compaction_fail_until = time.time() + 60
        # Manual compact bypasses anti-thrash AND summarizer-fail cooldown.
        events = list(session._maybe_compact_history(force=True))
        assert any(e.kind == "compaction" for e in events)
        assert pilot.calls > 0
        # Effective reclamation (fat history → short summary) clears strikes
        # and the shared fail-until plane.
        assert int(getattr(session, "_compaction_ineffective_count", 0) or 0) == 0
        assert float(getattr(session, "_compaction_fail_until", 0.0) or 0.0) == 0.0
    finally:
        shutil.rmtree(temp_dir)


def test_anti_thrash_recovery_probe_after_cooldown(monkeypatch):
    from harness.compaction_mixin import ANTI_THRASH_STRIKES

    pilot = _OkPilot()
    session, temp_dir = _fat_session(monkeypatch, pilot, timeout_s="30", cooldown_s="1")
    try:
        session._compaction_ineffective_count = ANTI_THRASH_STRIKES
        session._compaction_fail_until = time.time() - 1  # expired
        assert session._anti_thrash_blocked(force=False) is False
        assert session._compaction_ineffective_count == 1  # probation strike
    finally:
        shutil.rmtree(temp_dir)
