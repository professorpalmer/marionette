"""FTS5 session transcript recall over durable state.

Indexes per-session transcript text under ``{state_dir}/session_fts.sqlite``
so agents and the UI can search across sessions without scanning every JSON
file. Best-effort only: indexing failures never break transcript persist.

Windows: every call opens, uses, and closes the connection so temp-dir
cleanup never hits a held handle (same pattern as spill_registry).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Any, Iterable, List, Optional

DB_FILENAME = "session_fts.sqlite"
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_MAX_CHUNK_CHARS = 8000

_SAFE_SID = re.compile(r"^[A-Za-z0-9_-]+$")
_FTS_TOKEN = re.compile(r"[A-Za-z0-9_]+", re.UNICODE)

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS session_chunks USING fts5(
    session_id UNINDEXED,
    chunk,
    tokenize = 'porter unicode61'
);
"""


def _db_path(state_dir: str) -> str:
    return os.path.join(os.path.abspath(state_dir), DB_FILENAME)


def _safe_session_id(session_id: str) -> str:
    sid = (session_id or "").strip()
    if not sid or not _SAFE_SID.match(sid):
        return ""
    return sid


def _connect(state_dir: str) -> sqlite3.Connection:
    os.makedirs(os.path.abspath(state_dir), exist_ok=True)
    conn = sqlite3.connect(_db_path(state_dir), timeout=5.0)
    conn.execute(_SCHEMA)
    return conn


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if isinstance(content.get("content"), str):
            return content["content"]
    return str(content)


def _message_text(msg: Any) -> str:
    if not isinstance(msg, dict):
        return ""
    for key in ("text", "content", "summary"):
        raw = msg.get(key)
        text = _content_to_text(raw).strip()
        if text:
            role = (msg.get("role") or "").strip()
            return f"{role}: {text}" if role else text
    return ""


def extract_chunks(transcript_or_messages: Any) -> List[str]:
    """Pull searchable text chunks from a transcript payload or message list."""
    messages: Iterable[Any]
    if isinstance(transcript_or_messages, dict):
        history = transcript_or_messages.get("history") or []
        display = transcript_or_messages.get("display") or []
        messages = list(history) + list(display)
    elif isinstance(transcript_or_messages, list):
        messages = transcript_or_messages
    else:
        return []

    chunks: List[str] = []
    seen: set[str] = set()
    for msg in messages:
        text = _message_text(msg)
        if not text:
            continue
        if len(text) > _MAX_CHUNK_CHARS:
            text = text[:_MAX_CHUNK_CHARS]
        if text in seen:
            continue
        seen.add(text)
        chunks.append(text)
    return chunks


def index_session_transcript(
    state_dir: str,
    session_id: str,
    transcript_or_messages: Any,
) -> bool:
    """Replace FTS rows for one session. Never raises; returns False on failure."""
    if not state_dir:
        return False
    sid = _safe_session_id(session_id)
    if not sid:
        return False
    try:
        chunks = extract_chunks(transcript_or_messages)
        conn = _connect(state_dir)
        try:
            conn.execute(
                "DELETE FROM session_chunks WHERE session_id = ?",
                (sid,),
            )
            for chunk in chunks:
                conn.execute(
                    "INSERT INTO session_chunks(session_id, chunk) VALUES (?, ?)",
                    (sid, chunk),
                )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def _fts_match_query(query: str) -> str:
    """Build a safe FTS5 MATCH expression from free-text user input."""
    tokens = _FTS_TOKEN.findall(query or "")
    if not tokens:
        return ""
    # Quote each token so FTS5 operators in user input cannot alter the plan.
    return " AND ".join('"' + t.replace('"', "") + '"' for t in tokens[:32])


def search_sessions(
    state_dir: str,
    query: str,
    limit: int = _DEFAULT_LIMIT,
) -> List[dict]:
    """Search indexed session chunks. Empty/whitespace query returns [].

    Each hit: ``{"session_id", "snippet", "rank"}``. Rank is bm25 (lower is
    better). One best hit per session_id.
    """
    q = (query or "").strip()
    if not state_dir or not q:
        return []
    match = _fts_match_query(q)
    if not match:
        return []
    if not os.path.exists(_db_path(state_dir)):
        return []
    try:
        cap = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_LIMIT))
    except (TypeError, ValueError):
        cap = _DEFAULT_LIMIT
    try:
        conn = _connect(state_dir)
        try:
            rows = conn.execute(
                "SELECT session_id,"
                " snippet(session_chunks, 1, '', '', '...', 32) AS snip,"
                " bm25(session_chunks) AS rank"
                " FROM session_chunks"
                " WHERE session_chunks MATCH ?"
                " ORDER BY rank"
                " LIMIT ?",
                (match, cap * 8),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []

    best: dict[str, dict] = {}
    order: List[str] = []
    for session_id, snip, rank in rows:
        sid = str(session_id or "")
        if not sid:
            continue
        try:
            rank_f = float(rank)
        except (TypeError, ValueError):
            rank_f = 0.0
        hit = {
            "session_id": sid,
            "snippet": (snip or "").strip(),
            "rank": rank_f,
        }
        prev = best.get(sid)
        if prev is None:
            best[sid] = hit
            order.append(sid)
        elif rank_f < float(prev.get("rank", 0.0)):
            best[sid] = hit
    return [best[sid] for sid in order[:cap]]


def reindex_transcripts(state_dir: str) -> dict:
    """Walk ``transcripts/*.json`` and rebuild the FTS index.

    Best-effort: corrupt or unreadable files are skipped. Returns counts.
    """
    stats = {"indexed": 0, "skipped": 0, "errors": 0}
    if not state_dir:
        return stats
    trans_dir = os.path.join(os.path.abspath(state_dir), "transcripts")
    if not os.path.isdir(trans_dir):
        return stats
    try:
        names = sorted(os.listdir(trans_dir))
    except OSError:
        return stats

    for name in names:
        if not name.endswith(".json"):
            continue
        sid = name[:-5]
        if not _safe_session_id(sid):
            stats["skipped"] += 1
            continue
        path = os.path.join(trans_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            stats["skipped"] += 1
            continue
        if index_session_transcript(state_dir, sid, payload):
            stats["indexed"] += 1
        else:
            stats["errors"] += 1
    return stats


def remove_session_from_index(state_dir: str, session_id: str) -> bool:
    """Drop FTS rows for a deleted session. Never raises."""
    if not state_dir:
        return False
    sid = _safe_session_id(session_id)
    if not sid or not os.path.exists(_db_path(state_dir)):
        return False
    try:
        conn = _connect(state_dir)
        try:
            conn.execute(
                "DELETE FROM session_chunks WHERE session_id = ?",
                (sid,),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False
