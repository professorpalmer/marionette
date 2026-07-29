"""Tests for history compaction journal."""
from __future__ import annotations

import sqlite3
import tempfile

import pytest

from harness.history_compaction_journal import (
    EVENT_CACHE_BUST,
    EVENT_CACHE_READ,
    EVENT_COMPACT,
    history_compaction_payload,
    record_cache_signal,
    record_history_compaction,
    summarize_history_compactions,
)

_GOOD_SUMMARY = (
    "## Historical Task Snapshot\n"
    "Journal fixture summary seeded past the degenerate-char floor.\n"
    "## Resolved\nHistory compaction journal record written.\n"
    "## Pending / Open Questions\nNone.\n"
    "## Key Facts / Decisions / Files\ntests/test_history_compaction_journal.py\n"
)


@pytest.fixture(autouse=True)
def _allow_small_fixture_compaction(monkeypatch):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)


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
        assert payload["history_compaction_ran"] is True


def test_history_compaction_payload_no_ran_flag_when_empty():
    with tempfile.TemporaryDirectory() as state_dir:
        payload = history_compaction_payload(state_dir, "missing")
        assert payload["history_compactions"] == 0
        assert "history_compaction_ran" not in payload
        assert payload["history_cache_read_tokens"] == 0
        assert payload["history_cache_bust_tokens"] == 0
        assert payload["history_thrash_events"] == 0
        assert "history_compaction_cost_usd" not in payload


def test_journal_schema_records_cache_bust_and_thrash_telemetry():
    """Cache-bust / thrash fields persist; USD slot stays empty unless measured."""
    with tempfile.TemporaryDirectory() as state_dir:
        # Compact row carries thrash/savings; cache_bust tokens live only on
        # the dedicated signal row so aggregation counts each bust once.
        record_history_compaction(
            state_dir,
            "sess-telemetry",
            messages_compacted=8,
            chars_before=4000,
            chars_after=800,
            summary_preview=_GOOD_SUMMARY,
            event_kind=EVENT_COMPACT,
            tokens_before=1200,
            tokens_after=400,
            cache_read_tokens=0,
            cache_bust_tokens=0,
            estimated_cost_usd=None,
            thrash_strikes=1,
            savings_pct=0.667,
        )
        record_cache_signal(
            state_dir,
            "sess-telemetry",
            event_kind=EVENT_CACHE_BUST,
            cache_bust_tokens=800,
            cache_read_tokens=0,
            tokens_before=1200,
            tokens_after=400,
        )
        record_cache_signal(
            state_dir,
            "sess-telemetry",
            event_kind=EVENT_CACHE_READ,
            cache_read_tokens=120,
        )

        conn = sqlite3.connect(f"{state_dir}/history_compaction.sqlite")
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(compactions)").fetchall()
            }
            for required in (
                "event_kind",
                "tokens_before",
                "tokens_after",
                "cache_read_tokens",
                "cache_bust_tokens",
                "estimated_cost_usd",
                "thrash_strikes",
                "savings_pct",
            ):
                assert required in cols
            kinds = {
                row[0]
                for row in conn.execute(
                    "SELECT event_kind FROM compactions"
                ).fetchall()
            }
            assert EVENT_COMPACT in kinds
            assert EVENT_CACHE_BUST in kinds
            assert EVENT_CACHE_READ in kinds
            cost_row = conn.execute(
                "SELECT estimated_cost_usd, savings_pct, cache_bust_tokens "
                "FROM compactions WHERE event_kind = ?",
                (EVENT_COMPACT,),
            ).fetchone()
            assert cost_row[0] is None
            assert cost_row[1] == pytest.approx(0.667)
            assert cost_row[2] == 0
            bust_row = conn.execute(
                "SELECT cache_bust_tokens FROM compactions WHERE event_kind = ?",
                (EVENT_CACHE_BUST,),
            ).fetchone()
            assert bust_row[0] == 800
        finally:
            conn.close()

        summary = summarize_history_compactions(state_dir, session_id="sess-telemetry")
        assert summary.record_count == 1  # compact rows only
        assert summary.cache_bust_tokens == 800  # signal row only — counted once
        assert summary.cache_read_tokens == 120
        assert summary.thrash_events == 1
        assert summary.estimated_cost_usd is None

        payload = history_compaction_payload(state_dir, "sess-telemetry")
        assert payload["history_compactions"] == 1
        assert payload["history_cache_bust_tokens"] == 800
        assert payload["history_cache_read_tokens"] == 120
        assert payload["history_thrash_events"] == 1
        assert "history_compaction_cost_usd" not in payload


def test_savings_pct_legacy_percentage_coerced_to_ratio():
    """Writers that still pass 0..100 percentage points are stored as 0..1."""
    with tempfile.TemporaryDirectory() as state_dir:
        record_history_compaction(
            state_dir,
            "sess-pct",
            messages_compacted=3,
            chars_before=2000,
            chars_after=500,
            summary_preview=_GOOD_SUMMARY,
            savings_pct=66.7,
        )
        conn = sqlite3.connect(f"{state_dir}/history_compaction.sqlite")
        try:
            row = conn.execute(
                "SELECT savings_pct FROM compactions"
            ).fetchone()
            assert row[0] == pytest.approx(0.667)
        finally:
            conn.close()


def test_partial_migration_insert_omits_missing_columns(monkeypatch):
    """If additive ALTER fails mid-way, INSERT must not require missing cols."""
    with tempfile.TemporaryDirectory() as state_dir:
        path = f"{state_dir}/history_compaction.sqlite"
        conn = sqlite3.connect(path)
        try:
            # Legacy pre-telemetry table with only two telemetry columns added
            # (simulates a partial migration that stopped early).
            conn.execute(
                "CREATE TABLE compactions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "session_id TEXT NOT NULL, "
                "ts REAL NOT NULL, "
                "messages_compacted INTEGER NOT NULL, "
                "chars_before INTEGER NOT NULL, "
                "chars_after INTEGER NOT NULL, "
                "summary_preview TEXT NOT NULL DEFAULT '', "
                "event_kind TEXT NOT NULL DEFAULT 'compact', "
                "tokens_before INTEGER NOT NULL DEFAULT 0)"
            )
            conn.commit()
        finally:
            conn.close()

        # Remaining telemetry columns are not attempted — same end state as
        # ALTER failures being swallowed — so INSERT must omit them.
        monkeypatch.setattr(
            "harness.history_compaction_journal._TELEMETRY_COLUMNS",
            (
                ("event_kind", "TEXT NOT NULL DEFAULT 'compact'"),
                ("tokens_before", "INTEGER NOT NULL DEFAULT 0"),
            ),
        )

        # Hot path must not raise; row lands with available columns only.
        record_history_compaction(
            state_dir,
            "sess-partial",
            messages_compacted=4,
            chars_before=1000,
            chars_after=200,
            summary_preview=_GOOD_SUMMARY,
            tokens_before=500,
            tokens_after=100,
            cache_bust_tokens=400,
            savings_pct=0.8,
            thrash_strikes=1,
        )
        conn = sqlite3.connect(path)
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(compactions)").fetchall()
            }
            assert "event_kind" in cols
            assert "tokens_before" in cols
            assert "savings_pct" not in cols
            row = conn.execute(
                "SELECT messages_compacted, event_kind, tokens_before "
                "FROM compactions WHERE session_id = ?",
                ("sess-partial",),
            ).fetchone()
            assert row is not None
            assert row[0] == 4
            assert row[1] == EVENT_COMPACT
            assert row[2] == 500
        finally:
            conn.close()


def test_journal_stores_measured_cost_only_when_provided():
    with tempfile.TemporaryDirectory() as state_dir:
        record_history_compaction(
            state_dir,
            "sess-cost",
            messages_compacted=2,
            chars_before=1000,
            chars_after=200,
            summary_preview="ok",
            estimated_cost_usd=0.0123,
        )
        summary = summarize_history_compactions(state_dir, session_id="sess-cost")
        assert summary.estimated_cost_usd == pytest.approx(0.0123)
        payload = history_compaction_payload(state_dir, "sess-cost")
        assert payload["history_compaction_cost_usd"] == pytest.approx(0.0123)


def test_summarize_partial_table_preserves_event_kind_without_cache_columns(
    monkeypatch,
):
    """event_kind present + cache_* absent: count real compact rows only.

    Under a partial telemetry migration, summarize must not hardcode
    kind='compact' (which would inflate record_count from cache_bust rows)
    and must not invent cache token aggregates for missing columns.
    """
    with tempfile.TemporaryDirectory() as state_dir:
        path = f"{state_dir}/history_compaction.sqlite"
        conn = sqlite3.connect(path)
        try:
            # Partial schema: event_kind migrated, cache token columns not.
            conn.execute(
                "CREATE TABLE compactions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "session_id TEXT NOT NULL, "
                "ts REAL NOT NULL, "
                "messages_compacted INTEGER NOT NULL, "
                "chars_before INTEGER NOT NULL, "
                "chars_after INTEGER NOT NULL, "
                "summary_preview TEXT NOT NULL DEFAULT '', "
                "event_kind TEXT NOT NULL DEFAULT 'compact', "
                "tokens_before INTEGER NOT NULL DEFAULT 0, "
                "tokens_after INTEGER NOT NULL DEFAULT 0, "
                "thrash_strikes INTEGER NOT NULL DEFAULT 0, "
                "estimated_cost_usd REAL, "
                "savings_pct REAL)"
            )
            conn.execute(
                "INSERT INTO compactions ("
                "session_id, ts, messages_compacted, chars_before, chars_after, "
                "summary_preview, event_kind, tokens_before, tokens_after, "
                "thrash_strikes, estimated_cost_usd, savings_pct) "
                "VALUES (?, 1.0, 5, 4000, 800, ?, ?, 1000, 200, 0, NULL, 0.8)",
                ("sess-partial-sum", _GOOD_SUMMARY, EVENT_COMPACT),
            )
            conn.execute(
                "INSERT INTO compactions ("
                "session_id, ts, messages_compacted, chars_before, chars_after, "
                "summary_preview, event_kind, tokens_before, tokens_after, "
                "thrash_strikes, estimated_cost_usd, savings_pct) "
                "VALUES (?, 2.0, 0, 0, 0, '', ?, 1000, 200, 0, NULL, NULL)",
                ("sess-partial-sum", EVENT_CACHE_BUST),
            )
            conn.commit()
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(compactions)").fetchall()
            }
            assert "event_kind" in cols
            assert "cache_read_tokens" not in cols
            assert "cache_bust_tokens" not in cols
        finally:
            conn.close()

        # Freeze migration so _ensure_schema cannot ADD the missing cache cols.
        monkeypatch.setattr(
            "harness.history_compaction_journal._TELEMETRY_COLUMNS",
            (
                ("event_kind", "TEXT NOT NULL DEFAULT 'compact'"),
                ("tokens_before", "INTEGER NOT NULL DEFAULT 0"),
                ("tokens_after", "INTEGER NOT NULL DEFAULT 0"),
                ("thrash_strikes", "INTEGER NOT NULL DEFAULT 0"),
                ("estimated_cost_usd", "REAL"),
                ("savings_pct", "REAL"),
            ),
        )

        summary = summarize_history_compactions(
            state_dir, session_id="sess-partial-sum"
        )
        assert summary.record_count == 1  # compact only; cache_bust not counted
        assert summary.chars_before == 4000
        assert summary.chars_after == 800
        assert summary.tokens_saved > 0
        # Missing columns → zero / None, never invented from other fields.
        assert summary.cache_read_tokens == 0
        assert summary.cache_bust_tokens == 0
        assert summary.estimated_cost_usd is None
        assert summary.thrash_events == 0

        payload = history_compaction_payload(state_dir, "sess-partial-sum")
        assert payload["history_compactions"] == 1
        assert payload["history_compaction_ran"] is True
        assert payload["history_cache_read_tokens"] == 0
        assert payload["history_cache_bust_tokens"] == 0
        assert "history_compaction_cost_usd" not in payload


def test_inverse_partial_schema_without_event_kind_declines_deferred_rows(
    monkeypatch,
):
    """Without event_kind, deferred/refreeze rows must not land or masquerade."""
    from harness.history_compaction_journal import (
        record_compact_deferred,
        record_compact_refreeze,
    )

    with tempfile.TemporaryDirectory() as state_dir:
        path = f"{state_dir}/history_compaction.sqlite"
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "CREATE TABLE compactions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "session_id TEXT NOT NULL, "
                "ts REAL NOT NULL, "
                "messages_compacted INTEGER NOT NULL, "
                "chars_before INTEGER NOT NULL, "
                "chars_after INTEGER NOT NULL, "
                "summary_preview TEXT NOT NULL DEFAULT '')"
            )
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setattr(
            "harness.history_compaction_journal._TELEMETRY_COLUMNS",
            (),
        )

        record_compact_deferred(state_dir, "sess-no-kind", cache_read_tokens=9000)
        record_compact_refreeze(state_dir, "sess-no-kind", cache_bust_tokens=500)

        conn = sqlite3.connect(path)
        try:
            assert conn.execute("SELECT COUNT(*) FROM compactions").fetchone()[0] == 0
        finally:
            conn.close()

        summary = summarize_history_compactions(state_dir, "sess-no-kind")
        assert summary.record_count == 0
        assert summary.deferred_count == 0
        assert summary.refreeze_count == 0
        assert summary.cache_read_tokens == 0

        payload = history_compaction_payload(state_dir, "sess-no-kind")
        assert payload["history_compactions"] == 0
        assert payload["history_compaction_deferred"] == 0
        assert payload["history_compaction_refreeze"] == 0
        assert "history_compaction_ran" not in payload
        assert payload["history_cache_read_tokens"] == 0


def test_deferred_rows_do_not_inflate_history_cache_read_tokens(monkeypatch):
    from harness.history_compaction_journal import record_compact_deferred

    with tempfile.TemporaryDirectory() as state_dir:
        record_compact_deferred(
            state_dir,
            "sess-defer-read",
            cache_read_tokens=12_000,
            warm_detail={"warm_reason": "warm", "last_turn_cache_read_tokens": 12_000},
        )
        summary = summarize_history_compactions(state_dir, "sess-defer-read")
        assert summary.deferred_count == 1
        assert summary.cache_read_tokens == 0
        payload = history_compaction_payload(state_dir, "sess-defer-read")
        assert payload["history_compaction_deferred"] == 1
        assert payload["history_cache_read_tokens"] == 0
        assert "history_compaction_ran" not in payload


def test_cache_bust_uses_measured_delta_not_middle_tokens(monkeypatch):
    """When measured before→after delta is <=0, journal and event report 0."""
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    class _MockPilot:
        name = "mock"

        def chat(self, messages, tools=None, system=None):
            return type(
                "R", (), {"text": _GOOD_SUMMARY, "error": "", "tokens_out": 1}
            )()

        def complete(self, prompt, system=None):
            return type(
                "R", (), {"text": _GOOD_SUMMARY, "error": "", "tokens_out": 1}
            )()

    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)

    with tempfile.TemporaryDirectory() as state_dir:
        cfg = HarnessConfig(max_context_tokens=1000, state_dir=state_dir)
        session = ConversationalSession(cfg)
        session.harness_session_id = "zero-delta"
        session._history[0]["content"] = "sys"
        session.pilot = _MockPilot()  # type: ignore

        for i in range(10):
            session._history.append(
                {"role": "user", "content": f"User {i}: " + ("A" * 150)}
            )
            session._history.append(
                {"role": "assistant", "content": f"Assistant {i}: " + ("B" * 150)}
            )

        # Sticky estimate: before == after so measured bust delta is 0.
        # Pre-fix code fabricated cache_bust_tokens from middle_tokens.
        session._estimate_context_tokens = lambda: 5000  # type: ignore[method-assign]

        events = list(session._maybe_compact_history())
        done = [
            e for e in events
            if e.kind == "compaction" and not e.data.get("aborted")
        ]
        assert done, "expected a successful compaction event"
        assert done[0].data.get("cache_bust_tokens") == 0

        summary = summarize_history_compactions(state_dir, session_id="zero-delta")
        assert summary.record_count == 1
        assert summary.cache_bust_tokens == 0

        conn = sqlite3.connect(f"{state_dir}/history_compaction.sqlite")
        try:
            kinds = {
                r[0]
                for r in conn.execute("SELECT event_kind FROM compactions").fetchall()
            }
            # No dedicated cache_bust row when measured delta is zero.
            assert kinds == {EVENT_COMPACT}
        finally:
            conn.close()


def test_compaction_journal_written_during_history_compact():
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    class _MockPilot:
        name = "mock"

        def __init__(self, return_text=_GOOD_SUMMARY):
            self.return_text = return_text

        def chat(self, messages, tools=None, system=None):
            return type("R", (), {"text": self.return_text, "error": "", "tokens_out": 1})()

        def complete(self, prompt, system=None):
            return type("R", (), {"text": self.return_text, "error": "", "tokens_out": 1})()

    with tempfile.TemporaryDirectory() as state_dir:
        cfg = HarnessConfig(max_context_tokens=1000, state_dir=state_dir)
        session = ConversationalSession(cfg)
        session.harness_session_id = "compact-test"
        session._history[0]["content"] = "sys"
        session.pilot = _MockPilot()  # type: ignore

        for i in range(10):
            session._history.append({"role": "user", "content": f"User {i}: " + ("A" * 150)})
            session._history.append({"role": "assistant", "content": f"Assistant {i}: " + ("B" * 150)})

        list(session._maybe_compact_history())

        summary = summarize_history_compactions(state_dir, session_id="compact-test")
        assert summary.record_count == 1
        assert summary.tokens_saved > 0
        assert summary.cache_bust_tokens > 0
        assert summary.estimated_cost_usd is None

        conn = sqlite3.connect(f"{state_dir}/history_compaction.sqlite")
        try:
            # Deterministic event model: one compact + one cache_bust signal.
            row = conn.execute("SELECT COUNT(*) FROM compactions").fetchone()
            assert row[0] == 2
            kinds = {
                r[0]
                for r in conn.execute("SELECT event_kind FROM compactions").fetchall()
            }
            assert kinds == {EVENT_COMPACT, EVENT_CACHE_BUST}
            compact = conn.execute(
                "SELECT estimated_cost_usd, cache_bust_tokens, savings_pct "
                "FROM compactions WHERE event_kind = ?",
                (EVENT_COMPACT,),
            ).fetchone()
            assert compact[0] is None
            assert compact[1] == 0  # bust counted on the signal row only
            assert 0.0 < float(compact[2]) <= 1.0
            bust = conn.execute(
                "SELECT cache_bust_tokens FROM compactions WHERE event_kind = ?",
                (EVENT_CACHE_BUST,),
            ).fetchone()
            assert bust[0] > 0
            assert bust[0] == summary.cache_bust_tokens
        finally:
            conn.close()

        payload = history_compaction_payload(state_dir, "compact-test")
        assert payload["history_compaction_ran"] is True
        assert payload["history_cache_bust_tokens"] > 0
        assert "history_compaction_cost_usd" not in payload
