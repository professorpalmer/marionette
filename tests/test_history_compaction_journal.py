"""Tests for history compaction journal."""
from __future__ import annotations

import sqlite3
import tempfile

from harness.history_compaction_journal import (
    history_compaction_payload,
    record_history_compaction,
    summarize_history_compactions,
)


def test_record_and_summarize_round_trip():
    with tempfile.TemporaryDirectory() as state_dir:
        record_history_compaction(
            state_dir,
            "sess-a",
            messages_compacted=12,
            chars_before=8000,
            chars_after=1200,
            summary_preview="## Historical Task Snapshot\nDone.",
        )
        summary = summarize_history_compactions(state_dir, session_id="sess-a")
        assert summary.record_count == 1
        assert summary.chars_before == 8000
        assert summary.chars_after == 1200
        assert summary.tokens_saved > 0

        payload = history_compaction_payload(state_dir, "sess-a")
        assert payload["history_compactions"] == 1
        assert payload["history_tokens_saved"] == summary.tokens_saved


def test_compaction_journal_written_during_history_compact():
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession
    from tests.test_compaction import MockPilot

    with tempfile.TemporaryDirectory() as state_dir:
        cfg = HarnessConfig(max_context_tokens=1000, state_dir=state_dir)
        session = ConversationalSession(cfg)
        session.harness_session_id = "compact-test"
        session._history[0]["content"] = "sys"
        session.pilot = MockPilot("Fixed mock summary")  # type: ignore

        for i in range(10):
            session._history.append({"role": "user", "content": f"User {i}: " + ("A" * 150)})
            session._history.append({"role": "assistant", "content": f"Assistant {i}: " + ("B" * 150)})

        list(session._maybe_compact_history())

        summary = summarize_history_compactions(state_dir, session_id="compact-test")
        assert summary.record_count == 1
        assert summary.tokens_saved > 0

        conn = sqlite3.connect(f"{state_dir}/history_compaction.sqlite")
        try:
            row = conn.execute("SELECT COUNT(*) FROM compactions").fetchone()
            assert row[0] == 1
        finally:
            conn.close()
