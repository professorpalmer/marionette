"""Registry of spilled tool outputs, addressable as spill:// internal URIs.

`maybe_persist_result` writes oversized tool outputs to
{state_dir}/pmharness-results/. This module indexes those files in
{state_dir}/spill_index.sqlite so agents can address them as
``spill://{session_id}/{tool_call_id}`` (resolved in harness/internal_uri.py)
and find them via search_state, instead of memorizing raw filesystem paths.

Windows lesson from the savings ledger applies: every call opens, writes, and
CLOSES the connection so TemporaryDirectory cleanup never hits a held handle.
Registration failures never break the spill path.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from typing import Any, Optional

DB_FILENAME = "spill_index.sqlite"

# Mirrors _SAFE_SEGMENT in internal_uri.py: ids outside this set cannot be
# expressed as URI segments, so they are indexed but get no spill:// address.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    path TEXT NOT NULL,
    chars INTEGER NOT NULL,
    content_hash TEXT,
    ts REAL NOT NULL,
    UNIQUE(session_id, tool_call_id)
);
"""


def _db_path(state_dir: str) -> str:
    return os.path.join(os.path.abspath(state_dir), DB_FILENAME)


def _connect(state_dir: str) -> sqlite3.Connection:
    os.makedirs(os.path.abspath(state_dir), exist_ok=True)
    conn = sqlite3.connect(_db_path(state_dir), timeout=5.0)
    conn.execute(_SCHEMA)
    return conn


def spill_uri(session_id: str, tool_call_id: str) -> Optional[str]:
    """URI for a spill, or None when either id cannot be a URI segment."""
    if _SAFE_ID.match(session_id or "") and _SAFE_ID.match(tool_call_id or ""):
        return f"spill://{session_id}/{tool_call_id}"
    return None


def register_spill(
    state_dir: str,
    session_id: str,
    tool_call_id: str,
    path: str,
    chars: int,
    content_hash: str = "",
) -> bool:
    """Index a spilled output file. Re-spills of the same tool call replace
    the previous row (the file path may change when content changes).
    Never raises; returns False on any failure."""
    if not state_dir or not session_id or not tool_call_id or not path:
        return False
    try:
        conn = _connect(state_dir)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO spills"
                " (session_id, tool_call_id, path, chars, content_hash, ts)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, tool_call_id, path, int(chars), content_hash or "", time.time()),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def resolve_spill(state_dir: str, session_id: str, tool_call_id: str) -> Optional[dict[str, Any]]:
    """Look up one spill row. Returns None when missing or on any failure."""
    if not state_dir or not os.path.exists(_db_path(state_dir)):
        return None
    try:
        conn = _connect(state_dir)
        try:
            row = conn.execute(
                "SELECT session_id, tool_call_id, path, chars, content_hash, ts"
                " FROM spills WHERE session_id = ? AND tool_call_id = ?",
                (session_id, tool_call_id),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if row is None:
        return None
    return _row_to_dict(row)


def list_spills(state_dir: str, session_id: Optional[str] = None) -> list[dict[str, Any]]:
    """All spill rows, newest first, optionally filtered by session."""
    if not state_dir or not os.path.exists(_db_path(state_dir)):
        return []
    try:
        conn = _connect(state_dir)
        try:
            if session_id:
                rows = conn.execute(
                    "SELECT session_id, tool_call_id, path, chars, content_hash, ts"
                    " FROM spills WHERE session_id = ? ORDER BY ts DESC",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT session_id, tool_call_id, path, chars, content_hash, ts"
                    " FROM spills ORDER BY ts DESC",
                ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    return [_row_to_dict(row) for row in rows]


def _row_to_dict(row) -> dict[str, Any]:
    return {
        "session_id": row[0],
        "tool_call_id": row[1],
        "path": row[2],
        "chars": row[3],
        "content_hash": row[4],
        "ts": row[5],
    }
