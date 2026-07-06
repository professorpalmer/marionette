"""Persistent eval history for declarative check outcomes (round 6, v1).

Declarative check results were computed per worker run and then lost. This
journal makes them durable in {state_dir}/eval_history.sqlite so pass rates
are visible across runs. Mirrors the history-compaction journal pattern:
open/write/CLOSE per call (Windows file-lock lesson), failures swallowed on
the hot path. At the worker seam there is no harness session id, so the
session_id column carries the Puppetmaster job_id (or "default").
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Optional, Tuple

DB_FILENAME = "eval_history.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    source TEXT NOT NULL,
    check_id TEXT NOT NULL,
    passed INTEGER NOT NULL,
    on_fail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_eval_history_session ON evals(session_id);
"""


def eval_history_enabled() -> bool:
    """Recording is on by default; opt out with HARNESS_EVAL_HISTORY=0."""
    return os.environ.get("HARNESS_EVAL_HISTORY", "").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )


def _db_path(state_dir: str) -> str:
    return os.path.join(os.path.abspath(state_dir), DB_FILENAME)


def record_eval_results(
    state_dir: str,
    session_id: str,
    source: str,
    results: list,
) -> None:
    """Append check-result dicts (results_to_dicts shape). Never raises."""
    if not state_dir or not results or not eval_history_enabled():
        return
    sid = session_id or "default"
    now = time.time()
    try:
        os.makedirs(os.path.abspath(state_dir), exist_ok=True)
        conn = sqlite3.connect(_db_path(state_dir), timeout=5.0)
        try:
            conn.executescript(_SCHEMA)
            for item in results:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                conn.execute(
                    "INSERT INTO evals (session_id, ts, source, check_id, passed, on_fail)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        sid,
                        now,
                        source or "declarative_check",
                        str(item.get("id")),
                        1 if item.get("passed", False) else 0,
                        str(item.get("on_fail", "") or ""),
                    ),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def summarize_eval_history(
    state_dir: str,
    session_id: Optional[str] = None,
) -> Tuple[int, int]:
    """Return (recorded, failed) counts, optionally scoped to one session."""
    if not state_dir or not os.path.exists(_db_path(state_dir)):
        return 0, 0
    try:
        conn = sqlite3.connect(_db_path(state_dir), timeout=5.0)
        try:
            if session_id:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(passed = 0), 0) FROM evals"
                    " WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(passed = 0), 0) FROM evals"
                ).fetchone()
        finally:
            conn.close()
    except Exception:
        return 0, 0
    return int(row[0] or 0), int(row[1] or 0)


def eval_history_payload(state_dir: str, session_id: str = "") -> dict:
    """Usage-API fields. State-dir wide by default: the worker seam records
    under job ids, so a session-scoped filter would hide those rows."""
    try:
        recorded, failed = summarize_eval_history(state_dir, session_id or None)
    except Exception:
        return {"evals_recorded": 0, "evals_failed": 0}
    return {"evals_recorded": recorded, "evals_failed": failed}
