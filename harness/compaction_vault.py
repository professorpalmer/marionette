from __future__ import annotations

"""SQLite FTS vault of history elided by compaction.

The JSON peek sidecar stays a bounded addressable log. This vault is the
dump-and-query plane: compact writes the middle here, later turns retrieve
matching slices the way wiki grounding retrieves pages. Never raises.
"""

import hashlib
import os
import re
import sqlite3
import time
from typing import Any, List

from harness.api.redaction import redact_secret_text
from harness.session_fts import extract_chunks

DB_FILENAME = "compaction_vault.sqlite"
VAULT_HEADING = "### Compacted history (auto-retrieved)"

_SCHEMA_VERSION = "1"
_MAX_CHUNKS_PER_SESSION = 4000
_MAX_SESSION_CHARS = 8 * 1024 * 1024
_DEFAULT_HIT_LIMIT = 8
_DEFAULT_CHAR_BUDGET = 4000
_SAFE_SID = re.compile(r"^[A-Za-z0-9_-]+$")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,}", re.UNICODE)
_STOP = frozenset({
    "the", "and", "for", "what", "was", "were", "this", "that", "with",
    "from", "only", "when", "how", "why", "who", "are", "you", "your",
})
PLAN_CHUNK_PREFIX = "Earlier decisions and plans. Recap of what we decided:"
_RECAP_ASK = re.compile(
    r"\b(remind me|recap|what we decided|what did we decide|"
    r"decided earlier|what did we agree)\b",
    re.IGNORECASE,
)
_ACK_PREFIX = re.compile(
    r"^(noted|recorded|acknowledged|got it|ok|thanks)\b",
    re.IGNORECASE,
)
_ACK_ONLY = re.compile(
    r"^(noted|recorded|acknowledged|reversed|got it|ok|thanks|done|"
    r"understood)\.?$",
    re.IGNORECASE,
)
_PLAN_MATCH = '"Earlier" AND "decisions" AND "plans"'
_STORY_LINE_CAP = 12
_STORY_CHAR_BUDGET = 1800
_FILLER_DOCS_ONLY = frozenset({"docs", "only", "pad", "ack", "please", "keep"})
_HANDLE_SHAPED = re.compile(
    r"(?:/|\\|\.py\b|\.ts\b|\.js\b|\.tsx\b|\.md\b|\.json\b|://)",
    re.IGNORECASE,
)
_ACK_SUFFIX = re.compile(
    r":\s*(noted|recorded|acknowledged)\.?\s*$",
    re.IGNORECASE,
)
_DOCS_PASS_CONTINUE = re.compile(
    r"\b(please continue|continuing)\b.*\bdocs pass\b|"
    r"\bwithout restating\b",
    re.IGNORECASE,
)
_WORD4 = re.compile(r"[a-z0-9]{4,}")
_TOPIC_DROP = frozenset({
    "dont", "never", "only", "ahead", "please", "keep", "now",
    "retired", "instead", "reversed", "replacement", "noted",
    "recorded", "acknowledged",
})
_TOPIC_LAST_WINS = 0.6

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS vault_chunks USING fts5(
    session_id UNINDEXED,
    body,
    tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS vault_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def vault_enabled() -> bool:
    raw = (os.environ.get("HARNESS_COMPACTION_VAULT") or "1").strip().lower()
    return raw not in ("0", "false", "off", "no")


def _db_path(state_dir: str) -> str:
    return os.path.join(os.path.abspath(state_dir), DB_FILENAME)


def _safe_session_id(session_id: str) -> str:
    sid = (session_id or "").strip() or "default"
    if not _SAFE_SID.match(sid):
        return ""
    return sid


def _connect(state_dir: str) -> sqlite3.Connection:
    os.makedirs(os.path.abspath(state_dir), exist_ok=True)
    conn = sqlite3.connect(_db_path(state_dir), timeout=5.0)
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO vault_meta(key, value) VALUES (?, ?)",
        ("schema_version", _SCHEMA_VERSION),
    )
    return conn


def vault_match_query(query: str) -> str:
    """OR of distinctive tokens so a later ask can hit buried prose."""
    tokens = []
    seen = set()
    for raw in _TOKEN.findall(query or ""):
        token = raw.replace('"', "")
        key = token.lower()
        if key in _STOP or len(token) < 3 or key in seen:
            continue
        seen.add(key)
        tokens.append(token)
        if len(tokens) >= 24:
            break
    if not tokens:
        return ""
    return " OR ".join('"' + t + '"' for t in tokens)


def is_recap_ask(query: str) -> bool:
    """True when the ask is anaphoric recap, not a content key."""
    return bool(_RECAP_ASK.search(query or ""))


def _filler_like(text: str) -> bool:
    low = (text or "").lower()
    if "pad pad pad" in low or "ack ack ack" in low:
        return True
    if _DOCS_PASS_CONTINUE.search(low):
        return True
    words = [w for w in re.findall(r"[a-z0-9]+", low) if w not in _STOP]
    if "docs only" in low:
        distinctive = [w for w in words if w not in _FILLER_DOCS_ONLY]
        if len(set(distinctive)) <= 4:
            return True
    return len(words) > 8 and len(set(words)) <= 3


def _ack_like(text: str) -> bool:
    stripped = (text or "").strip()
    if _ACK_ONLY.match(stripped):
        return True
    if len(stripped) >= 100:
        return False
    return bool(_ACK_PREFIX.match(stripped) or _ACK_SUFFIX.search(stripped))


def _handle_shaped(text: str) -> bool:
    """Paths and URIs belong in the handle index, not the selected story."""
    return bool(_HANDLE_SHAPED.search(text or ""))


def _line_tokens(text: str) -> set:
    return {m.group(0) for m in _WORD4.finditer((text or "").lower())} - _STOP


def topic_tokens(text: str) -> set:
    """Content nouns left after stripping polarity and cue words."""
    return _line_tokens(text) - _TOPIC_DROP


def same_topic(left: str, right: str) -> bool:
    """True when two lines are about the same obligation or entity."""
    older = topic_tokens(left)
    newer = topic_tokens(right)
    if len(older) < 2 or len(newer) < 2:
        return False
    overlap = len(older & newer)
    return (overlap / float(min(len(older), len(newer)))) >= _TOPIC_LAST_WINS


def topic_last_wins_receipt(lines: List[str]):
    """Return (kept, dropped) using the same same_topic rule as last-wins."""
    kept: List[str] = []
    dropped: List[str] = []
    for text in lines:
        evicted = [row for row in kept if same_topic(row, text)]
        kept = [row for row in kept if not same_topic(row, text)]
        dropped.extend(evicted)
        kept.append(text)
    return kept, dropped


def apply_topic_last_wins(lines: List[str]) -> List[str]:
    """Drop earlier lines that share a topic with a later one."""
    kept, _dropped = topic_last_wins_receipt(lines)
    return kept


def _novel_enough(text: str, accepted: List[str]) -> bool:
    words = _line_tokens(text)
    if not words:
        return True
    for prior in accepted:
        prior_w = _line_tokens(prior)
        if words and prior_w and (len(words & prior_w) / len(words)) >= 0.7:
            return False
    return True


def _fit_newest_lines(lines: List[str], char_budget: int) -> List[str]:
    kept: List[str] = []
    used = 0
    for text in reversed(lines):
        add = len(text) + 3
        if kept and used + add > char_budget:
            break
        kept.append(text)
        used += add
    kept.reverse()
    return kept


def select_story_lines(messages: Any) -> List[str]:
    """Newest distinctive prose, skipping residuals, acks, and handle chatter."""
    lines = []
    seen = set()
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        if msg.get("_compressed_summary"):
            continue
        text = redact_secret_text(str(msg.get("content") or "")).strip()
        if text.startswith("[Earlier conversation summarized"):
            continue
        if (
            not text
            or _filler_like(text)
            or _ack_like(text)
            or _handle_shaped(text)
            or text in seen
        ):
            continue
        seen.add(text)
        lines.append(text)
    lines = apply_topic_last_wins(lines)
    newest = lines[-_STORY_LINE_CAP:]
    novel: List[str] = []
    for text in reversed(newest):
        if _novel_enough(text, novel):
            novel.append(text)
    novel.reverse()
    return _fit_newest_lines(novel, _STORY_CHAR_BUDGET)


def drop_ack_like_messages(messages: Any) -> List[dict]:
    """Drop one-word assistant acks so they cannot rewrite later policy."""
    kept: List[dict] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        text = str(msg.get("content") or "").strip()
        if role == "assistant" and _ack_like(text) and not msg.get("tool_calls"):
            continue
        kept.append(msg)
    return kept


def build_plan_recap_chunk(messages: Any) -> str:
    """Compact-time selector: last-N non-filler user/assistant lines."""
    lines = select_story_lines(messages)
    if not lines:
        return ""
    body = PLAN_CHUNK_PREFIX + "\n- " + "\n- ".join(lines)
    return body[:2000]


def index_elided_messages(
    state_dir: str,
    session_id: str,
    messages: Any,
) -> int:
    """Append searchable chunks for one compact. Returns rows written."""
    if not state_dir or not vault_enabled():
        return 0
    sid = _safe_session_id(session_id)
    if not sid:
        return 0
    chunks = []
    for chunk in extract_chunks(messages):
        text = redact_secret_text(chunk or "").strip()
        if text:
            chunks.append(text)
    plan = build_plan_recap_chunk(messages)
    if plan:
        chunks.append(plan)
    if not chunks:
        return 0
    try:
        conn = _connect(state_dir)
        try:
            written = 0
            for body in chunks:
                conn.execute(
                    "INSERT INTO vault_chunks(session_id, body) VALUES (?, ?)",
                    (sid, body),
                )
                written += 1
            _evict_over_cap(conn, sid)
            conn.commit()
            return written
        finally:
            conn.close()
    except Exception:
        return 0


def _evict_over_cap(conn: sqlite3.Connection, sid: str) -> None:
    rows = conn.execute(
        "SELECT rowid, length(body) FROM vault_chunks WHERE session_id = ? "
        "ORDER BY rowid ASC",
        (sid,),
    ).fetchall()
    total_chars = sum(int(length or 0) for _rowid, length in rows)
    drop: list[int] = []
    keep = len(rows)
    while rows and (
        keep > _MAX_CHUNKS_PER_SESSION or total_chars > _MAX_SESSION_CHARS
    ):
        rowid, length = rows.pop(0)
        drop.append(int(rowid))
        keep -= 1
        total_chars -= int(length or 0)
    for rowid in drop:
        conn.execute("DELETE FROM vault_chunks WHERE rowid = ?", (rowid,))


def _fts_bodies(
    conn: sqlite3.Connection,
    sid: str,
    match: str,
    cap: int,
) -> List[str]:
    if not match:
        return []
    fetch = max(int(cap) * 2, 12)
    try:
        rows = conn.execute(
            "SELECT rowid, body "
            "FROM vault_chunks "
            "WHERE vault_chunks MATCH ? AND session_id = ? "
            "ORDER BY bm25(vault_chunks) LIMIT ?",
            (match, sid, fetch),
        ).fetchall()
    except Exception:
        return []
    ordered = []
    seen = set()
    for rowid, body in sorted(rows, key=lambda item: int(item[0])):
        text = redact_secret_text(str(body or "")).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    won = apply_topic_last_wins(ordered)
    won.reverse()
    return won[: max(1, int(cap))]


def _fit_budget(hits: List[str], budget: int) -> List[str]:
    out = []
    used = 0
    for text in hits:
        room = budget - used
        if room <= 40:
            break
        if len(text) > room:
            text = text[:room].rstrip() + "…"
        out.append(text)
        used += len(text)
    return out


def retrieve_vault_chunks(
    state_dir: str,
    session_id: str,
    query: str,
    *,
    limit: int = _DEFAULT_HIT_LIMIT,
    char_budget: int = _DEFAULT_CHAR_BUDGET,
) -> List[str]:
    """Return ranked vault bodies for this session. Empty on miss or error."""
    return list(
        (retrieve_vault_result(
            state_dir, session_id, query, limit=limit, char_budget=char_budget
        ).get("hits") or [])
    )


def retrieve_vault_result(
    state_dir: str,
    session_id: str,
    query: str,
    *,
    limit: int = _DEFAULT_HIT_LIMIT,
    char_budget: int = _DEFAULT_CHAR_BUDGET,
) -> dict:
    """Retrieve plus the route that selected the hits. Never raises."""
    empty = {"hits": [], "route": "empty"}
    if not state_dir or not vault_enabled():
        return empty
    sid = _safe_session_id(session_id)
    match = vault_match_query(query)
    if not sid:
        return empty
    cap = max(1, min(int(limit or _DEFAULT_HIT_LIMIT), 16))
    budget = max(240, int(char_budget or _DEFAULT_CHAR_BUDGET))
    try:
        conn = _connect(state_dir)
        try:
            fts_hits = _fts_bodies(conn, sid, match, cap)
            plan_hits = _fts_bodies(conn, sid, _PLAN_MATCH, 2)
        finally:
            conn.close()
    except Exception:
        return empty
    recap = is_recap_ask(query)
    if recap and plan_hits:
        return {"hits": _fit_budget(plan_hits, budget), "route": "recap_plan"}
    if fts_hits:
        return {"hits": _fit_budget(fts_hits, budget), "route": "fts"}
    return empty


def format_vault_section(hits: List[str]) -> str:
    if not hits:
        return ""
    lines = [
        "COMPACTION VAULT HAS ALREADY BEEN QUERIED FOR THIS TURN. Elided "
        "session history matching this ask is below. Use it as primary recall "
        "for earlier decisions, files, and tokens. Do not peek_history for "
        "rows already here.",
        "",
        VAULT_HEADING,
    ]
    for hit in hits:
        lines.append("- " + hit.replace("\n", " ").strip())
    return "\n".join(lines)


_CITE_SNIPPET_CHARS = 120


def _clip_cite_snippet(text: str, limit: int = _CITE_SNIPPET_CHARS) -> str:
    raw = " ".join(str(text or "").split())
    if raw.startswith(PLAN_CHUNK_PREFIX):
        raw = raw[len(PLAN_CHUNK_PREFIX):].strip()
    if len(raw) <= limit:
        return raw
    return raw[:limit].rstrip()


def build_turn_vault_cite(
    state_dir: str,
    session_id: str,
    user_message: str,
) -> dict:
    """Section text plus cite payload for the current ask. Never raises."""
    empty = {"section": "", "route": "empty", "snippets": []}
    try:
        if not (user_message or "").strip():
            return empty
        result = retrieve_vault_result(state_dir, session_id, user_message)
        hits = list(result.get("hits") or [])
        route = str(result.get("route") or "empty")
        snippets = [
            _clip_cite_snippet(hit) for hit in hits if str(hit or "").strip()
        ]
        return {
            "section": format_vault_section(hits),
            "route": route,
            "snippets": snippets,
        }
    except Exception:
        return empty


def build_turn_vault_section(
    state_dir: str,
    session_id: str,
    user_message: str,
) -> str:
    """Wiki-style inject for the current user ask. Never raises."""
    return str(build_turn_vault_cite(state_dir, session_id, user_message).get("section") or "")


def snap_compact(
    state_dir: str,
    session_id: str,
    messages: Any,
    *,
    snapshot_id: str = "",
) -> dict:
    """One-shot compact-to-snapshot via archive + vault + journal. Never raises.

    Folds hot context into the existing compaction archive sidecar, FTS vault,
    and history journal. Does not invent a second engine or rewrite live
    history — Compact Now still owns the LLM residual path.
    """
    empty = {
        "ok": False,
        "snapshot_id": "",
        "session_id": "",
        "archived": False,
        "archived_messages": 0,
        "vault_chunks": 0,
        "chars_before": 0,
        "reason": "no_compactable_history",
    }
    try:
        sid = _safe_session_id(session_id)
        copied = [m for m in (messages or []) if isinstance(m, dict)]
        if not state_dir or not sid or not copied:
            empty["session_id"] = sid
            return empty
        chars_before = sum(len(str(m.get("content") or "")) for m in copied)
        if not snapshot_id:
            digest = hashlib.sha256()
            digest.update(sid.encode("utf-8"))
            digest.update(str(len(copied)).encode("ascii"))
            digest.update(str(time.time()).encode("ascii"))
            snapshot_id = "snap-" + digest.hexdigest()[:16]
        from harness.compaction_archive import (
            append_compaction_archive,
            load_compaction_archive_messages,
        )
        from harness.history_compaction_journal import (
            EVENT_COMPACT,
            record_history_compaction,
        )

        archived = bool(append_compaction_archive(state_dir, sid, copied))
        vault_chunks = int(index_elided_messages(state_dir, sid, copied) or 0)
        record_history_compaction(
            state_dir,
            sid,
            len(copied),
            chars_before,
            0,
            f"snapcompact {snapshot_id}",
            event_kind=EVENT_COMPACT,
            compact_policy="snap",
        )
        archived_messages = (
            len(load_compaction_archive_messages(state_dir, sid)) if archived else 0
        )
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "session_id": sid,
            "archived": archived,
            "archived_messages": archived_messages,
            "vault_chunks": vault_chunks,
            "chars_before": chars_before,
            "reason": "ok",
        }
    except Exception:
        return empty

