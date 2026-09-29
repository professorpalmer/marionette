"""The savings summary aggregates in SQLite with the same results as before."""
from __future__ import annotations

import random
import sqlite3

from harness.tool_output_savings import DB_FILENAME, ToolOutputSavingsLedger


def _python_summary(rows):
    tokens = chars = 0
    by_reason: dict = {}
    for saved, orig, compact, reason in rows:
        tokens += saved
        chars += max(0, orig - compact)
        r = str(reason or "unknown")
        by_reason[r] = by_reason.get(r, 0) + saved
    return tokens, chars, len(rows), by_reason


def test_sql_aggregate_matches_row_aggregate(tmp_path):
    ledger = ToolOutputSavingsLedger(str(tmp_path))
    ledger._ensure_db()
    rng = random.Random(7)
    rows = []
    for i in range(300):
        rec = (rng.randint(0, 900), rng.randint(0, 5000), rng.randint(0, 6000), rng.choice(["spill", "", "elide", "dedupe"]))
        sid = rng.choice(["s1", "s2"])
        job = rng.choice([None, "job_a", "job_b"])
        rows.append((sid, job, rec))
        ledger._conn.execute(
            "INSERT INTO tool_output_savings (ts, session_id, tool_call_id, original_chars, compact_chars, tokens_saved, reason, job_id) VALUES (?,?,?,?,?,?,?,?)",
            (1.0, sid, f"c{i}", rec[1], rec[2], rec[0], rec[3], job),
        )
    ledger._conn.commit()
    ledger.close()
    for sid, job in ((None, None), ("s1", None), (None, "job_a"), ("s2", "job_b")):
        want = _python_summary([r for s, j, r in rows if (sid is None or s == sid) and (job is None or j == job)])
        got = ToolOutputSavingsLedger(str(tmp_path)).summarize(session_id=sid, job_id=job)
        assert (got.tokens_saved, got.chars_saved, got.record_count, got.by_reason) == want


def test_legacy_ledger_without_job_id_still_opens_and_gets_the_index(tmp_path):
    conn = sqlite3.connect(tmp_path / DB_FILENAME)
    conn.execute("CREATE TABLE tool_output_savings (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, session_id TEXT NOT NULL, tool_call_id TEXT NOT NULL, original_chars INTEGER NOT NULL, compact_chars INTEGER NOT NULL, tokens_saved INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT '', UNIQUE(session_id, tool_call_id))")
    conn.execute("INSERT INTO tool_output_savings (ts, session_id, tool_call_id, original_chars, compact_chars, tokens_saved, reason) VALUES (1, 's', 'c', 100, 40, 15, 'spill')")
    conn.commit()
    conn.close()
    got = ToolOutputSavingsLedger(str(tmp_path)).summarize()
    assert (got.tokens_saved, got.chars_saved, got.record_count) == (15, 60, 1)
    names = {r[0] for r in sqlite3.connect(tmp_path / DB_FILENAME).execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_tool_output_savings_job" in names
