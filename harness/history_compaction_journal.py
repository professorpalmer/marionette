"""History compaction journal for conversation summarization events.

Records when the pilot replaces a block of messages with a compressed summary,
mirroring the tool-output savings ledger pattern. Stdlib-only; never raises on
the hot path.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .tool_output_savings import tokens_avoided

DB_FILENAME = "history_compaction.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS compactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    messages_compacted INTEGER NOT NULL,
    chars_before INTEGER NOT NULL,
    chars_after INTEGER NOT NULL,
    summary_preview TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_history_compactions_session
    ON compactions(session_id);
"""


@dataclass(frozen=True)
class HistoryCompactionSummary:
    record_count: int = 0
    chars_before: int = 0
    chars_after: int = 0
    tokens_saved: int = 0


def _db_path(state_dir: str) -> Path:
    return Path(state_dir) / DB_FILENAME


def record_history_compaction(
    state_dir: str,
    session_id: str,
    messages_compacted: int,
    chars_before: int,
    chars_after: int,
    summary_preview: str,
) -> None:
    """Append one compaction journal row. Failures are swallowed."""
    if not state_dir or messages_compacted <= 0:
        return
    preview = (summary_preview or "")[:400]
    sid = session_id or "default"
    try:
        path = _db_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO compactions "
                "(session_id, ts, messages_compacted, chars_before, chars_after, summary_preview) "
                "VALUES (?,?,?,?,?,?)",
                (
                    sid,
                    time.time(),
                    int(messages_compacted),
                    int(chars_before),
                    int(chars_after),
                    preview,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def summarize_history_compactions(
    state_dir: str,
    session_id: Optional[str] = None,
) -> HistoryCompactionSummary:
    """Aggregate journal rows, optionally scoped to one session."""
    if not state_dir:
        return HistoryCompactionSummary()
    try:
        path = _db_path(state_dir)
        if not path.is_file():
            return HistoryCompactionSummary()
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            if session_id:
                rows = conn.execute(
                    "SELECT messages_compacted, chars_before, chars_after "
                    "FROM compactions WHERE session_id = ?",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT messages_compacted, chars_before, chars_after FROM compactions"
                ).fetchall()
        finally:
            conn.close()
    except Exception:
        return HistoryCompactionSummary()

    chars_before = 0
    chars_after = 0
    tokens_saved = 0
    for _msgs, before, after in rows:
        chars_before += int(before)
        chars_after += int(after)
        tokens_saved += tokens_avoided(int(before), int(after))
    return HistoryCompactionSummary(
        record_count=len(rows),
        chars_before=chars_before,
        chars_after=chars_after,
        tokens_saved=tokens_saved,
    )


def history_compaction_payload(
    state_dir: str,
    session_id: str,
) -> dict:
    """Compact fields for usage/session APIs."""
    summary = summarize_history_compactions(state_dir, session_id or None)
    return {
        "history_compactions": summary.record_count,
        "history_tokens_saved": summary.tokens_saved,
    }
