"""Auto-distill's hidden pilot call stays bounded and needs real new signal."""
from __future__ import annotations

import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.wiki_distill import DISTILL_MIN_NEW_TOOL_CALLS, TRANSCRIPT_DIGEST_MAX_CHARS


def _session(monkeypatch):
    s = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp()))
    s._auto_distill = True
    calls = []
    monkeypatch.setattr(s, "distill", lambda: calls.append(1) or {"status": "ok"})
    return s, calls


def test_digest_keeps_the_most_recent_rows_within_budget(monkeypatch):
    s, _ = _session(monkeypatch)
    rows = [{"role": "assistant", "text": f"turn {i} " + "x" * 400} for i in range(200)]
    monkeypatch.setattr(s, "export_display_transcript", lambda: rows)
    digest = s._build_transcript_digest()
    assert len(digest) <= TRANSCRIPT_DIGEST_MAX_CHARS
    assert digest.endswith(rows[-1]["text"])
    assert "turn 0 " not in digest


def test_a_bare_turn_does_not_trigger_a_distill_call(monkeypatch):
    s, calls = _session(monkeypatch)
    s._turn_count += 1
    assert s._maybe_auto_distill() is None
    s._total_tool_calls += DISTILL_MIN_NEW_TOOL_CALLS
    s._maybe_auto_distill()
    assert calls == [1]
    s._turn_count += 1
    s._session_findings.append({"type": "finding", "claim": "x"})
    s._maybe_auto_distill()
    assert calls == [1, 1]
