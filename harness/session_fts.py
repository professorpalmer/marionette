"""FTS5 session recall over durable state.

Indexes per-session transcript text, list previews, and durable artifact
headlines under ``{state_dir}/session_fts.sqlite`` so agents and the UI can
search across sessions without scanning every JSON file or SwarmStore row.
SwarmStore/DurableState remain source of truth; FTS rows are disposable
projections keyed by stable session/job/artifact ids.

Best-effort only: indexing failures never break transcript persist or store
reads. Never indexes full artifact bodies or secrets — only capped headlines
and normalized preview text.

Windows: every call opens, uses, and closes the connection so temp-dir
cleanup never hits a held handle (same pattern as spill_registry).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from typing import Any, Iterable, List, Optional, Tuple

from harness.api.redaction import redact_api_secrets

DB_FILENAME = "session_fts.sqlite"
_SCHEMA_VERSION = "2"
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_MAX_CHUNK_CHARS = 8000
_MAX_PREVIEW_CHARS = 500
_MAX_HEADLINE_CHARS = 300

_SAFE_SID = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9_-]+$")
_FTS_TOKEN = re.compile(r"[A-Za-z0-9_]+", re.UNICODE)

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS session_chunks USING fts5(
    session_id UNINDEXED,
    chunk,
    tokenize = 'porter unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS session_previews USING fts5(
    session_id UNINDEXED,
    preview,
    tokenize = 'porter unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS artifact_headlines USING fts5(
    session_id UNINDEXED,
    job_id UNINDEXED,
    artifact_id UNINDEXED,
    headline,
    tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS fts_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _db_path(state_dir: str) -> str:
    return os.path.join(os.path.abspath(state_dir), DB_FILENAME)


def _safe_session_id(session_id: str) -> str:
    sid = (session_id or "").strip()
    if not sid or not _SAFE_SID.match(sid):
        return ""
    return sid


def _safe_ref(value: str) -> str:
    ref = (value or "").strip()
    if not ref or not _SAFE_REF.match(ref):
        return ""
    return ref


def _redact_index_text(text: str) -> str:
    """Bounded redaction before FTS indexing — not perfect secret detection."""
    if not text:
        return ""
    return str(redact_api_secrets(text) or "")


def _normalize_index_text(text: str, cap: int) -> str:
    cleaned = " ".join(_redact_index_text(str(text or "")).split())
    if not cleaned:
        return ""
    if len(cleaned) > cap:
        return cleaned[:cap]
    return cleaned


def _connect(state_dir: str) -> sqlite3.Connection:
    os.makedirs(os.path.abspath(state_dir), exist_ok=True)
    conn = sqlite3.connect(_db_path(state_dir), timeout=5.0)
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO fts_meta(key, value) VALUES (?, ?)",
        ("schema_version", _SCHEMA_VERSION),
    )
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view')"
            " AND name = ? LIMIT 1",
            (name,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


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
        text = _redact_index_text(_content_to_text(raw)).strip()
        if text:
            role = (msg.get("role") or "").strip()
            return f"{role}: {text}" if role else text
    return ""


def _preview_from_messages(messages: Any, max_chars: int = _MAX_PREVIEW_CHARS) -> str:
    history: Iterable[Any]
    if isinstance(messages, dict):
        history = messages.get("history") or []
    elif isinstance(messages, list):
        history = messages
    else:
        return ""
    for msg in history:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif isinstance(block, str):
                    parts.append(block)
            content = " ".join(parts)
        text = _normalize_index_text(str(content or ""), max_chars)
        if text:
            return text
    return ""


def _display_artifact_id(art: dict, ordinal: int) -> str:
    """Stable unique id: real artifact id when present, else type+ordinal+digest."""
    real_id = str(art.get("id") or "").strip()
    if real_id and _SAFE_REF.match(real_id):
        return real_id
    art_type = str(art.get("type") or "artifact").strip() or "artifact"
    headline = _normalize_index_text(str(art.get("headline") or ""), _MAX_HEADLINE_CHARS)
    digest = hashlib.sha256(
        f"{art_type}\0{ordinal}\0{headline}".encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    return f"display-{art_type}-{ordinal}-{digest}"


def _extract_display_artifact_headlines(transcript_or_messages: Any) -> List[Tuple[str, str, str]]:
    """Return (artifact_id, type, headline) tuples from transcript display cards."""
    if not isinstance(transcript_or_messages, dict):
        return []
    display = transcript_or_messages.get("display") or []
    out: List[Tuple[str, str, str]] = []
    seen: set[str] = set()
    ordinal = 0
    for msg in display:
        if not isinstance(msg, dict) or msg.get("type") != "card":
            continue
        result = msg.get("result")
        if not isinstance(result, dict):
            continue
        for art in result.get("artifacts") or []:
            if not isinstance(art, dict):
                continue
            art_type = str(art.get("type") or "").strip()
            headline = _normalize_index_text(
                str(art.get("headline") or ""),
                _MAX_HEADLINE_CHARS,
            )
            if not art_type or not headline:
                continue
            artifact_id = _display_artifact_id(art, ordinal)
            ordinal += 1
            key = artifact_id
            if key in seen:
                continue
            seen.add(key)
            out.append((artifact_id, art_type, headline))
    return out


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


def _replace_session_preview(conn: sqlite3.Connection, sid: str, preview: str) -> None:
    if not _table_exists(conn, "session_previews"):
        return
    conn.execute("DELETE FROM session_previews WHERE session_id = ?", (sid,))
    if preview:
        conn.execute(
            "INSERT INTO session_previews(session_id, preview) VALUES (?, ?)",
            (sid, preview),
        )


def _replace_session_display_artifacts(
    conn: sqlite3.Connection,
    sid: str,
    transcript_or_messages: Any,
) -> None:
    if not _table_exists(conn, "artifact_headlines"):
        return
    conn.execute(
        "DELETE FROM artifact_headlines WHERE session_id = ? AND job_id = ''",
        (sid,),
    )
    for artifact_id, art_type, headline in _extract_display_artifact_headlines(transcript_or_messages):
        conn.execute(
            "INSERT INTO artifact_headlines(session_id, job_id, artifact_id, headline)"
            " VALUES (?, ?, ?, ?)",
            (sid, "", artifact_id, f"{art_type}: {headline}"),
        )


def index_session_preview(
    state_dir: str,
    session_id: str,
    preview_or_messages: Any,
) -> bool:
    """Replace the FTS preview row for one session. Never raises."""
    if not state_dir:
        return False
    sid = _safe_session_id(session_id)
    if not sid:
        return False
    if isinstance(preview_or_messages, str):
        preview = _normalize_index_text(preview_or_messages, _MAX_PREVIEW_CHARS)
    else:
        preview = _preview_from_messages(preview_or_messages, _MAX_PREVIEW_CHARS)
    try:
        conn = _connect(state_dir)
        try:
            _replace_session_preview(conn, sid, preview)
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def index_session_artifacts(
    state_dir: str,
    session_id: str,
    artifacts: Iterable[Any],
    *,
    job_id: str = "",
) -> bool:
    """Replace FTS artifact headline rows for one session/job. Never raises."""
    if not state_dir:
        return False
    sid = _safe_session_id(session_id)
    if not sid:
        return False
    jid = _safe_ref(job_id)
    rows: List[Tuple[str, str]] = []
    seen: set[str] = set()
    for art in artifacts or []:
        if isinstance(art, dict):
            aid = _safe_ref(str(art.get("id") or ""))
            headline = _normalize_index_text(
                str(art.get("headline") or ""),
                _MAX_HEADLINE_CHARS,
            )
        else:
            aid = _safe_ref(str(getattr(art, "id", "") or ""))
            payload = getattr(art, "payload", {}) or {}
            headline = _normalize_index_text(
                str(
                    payload.get("claim")
                    or payload.get("decision")
                    or payload.get("risk")
                    or payload.get("check")
                    or payload.get("summary")
                    or payload.get("change")
                    or ""
                ),
                _MAX_HEADLINE_CHARS,
            )
        if not aid or not headline:
            continue
        key = f"{aid}::{headline}"
        if key in seen:
            continue
        seen.add(key)
        rows.append((aid, headline))
    try:
        conn = _connect(state_dir)
        try:
            if not _table_exists(conn, "artifact_headlines"):
                return False
            if jid:
                conn.execute(
                    "DELETE FROM artifact_headlines WHERE session_id = ? AND job_id = ?",
                    (sid, jid),
                )
            else:
                conn.execute(
                    "DELETE FROM artifact_headlines WHERE session_id = ? AND job_id = ''",
                    (sid,),
                )
            for aid, headline in rows:
                conn.execute(
                    "INSERT INTO artifact_headlines(session_id, job_id, artifact_id, headline)"
                    " VALUES (?, ?, ?, ?)",
                    (sid, jid, aid, headline),
                )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def best_effort_index_job_artifacts(
    state_dir: str,
    job_id: str,
    *,
    session_id: str = "",
    artifacts: Optional[Iterable[Any]] = None,
    durable: Any = None,
) -> bool:
    """Read-only projection hook at the jobs/artifact merge boundary."""
    if not state_dir:
        return False
    jid = _safe_ref(job_id)
    if not jid:
        return False
    sid = _safe_session_id(session_id)
    arts = list(artifacts or [])
    if not arts:
        try:
            if durable is None:
                from .state import DurableState

                durable = DurableState(state_dir)
            raw = durable.store.list_artifacts(jid)
            arts = durable.format_artifacts(raw)
        except Exception:
            return False
    if not sid:
        try:
            from .job_scoping import parse_job_session_id

            job = durable.store.get_job(jid) if durable is not None else None
            label = getattr(job, "label", None) if job is not None else None
            tasks = durable.store.list_tasks(jid) if durable is not None else []
            sid = _safe_session_id(parse_job_session_id(label, tasks))
        except Exception:
            sid = ""
    if not sid:
        return False
    try:
        return index_session_artifacts(state_dir, sid, arts, job_id=jid)
    except Exception:
        return False


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
        preview = _preview_from_messages(transcript_or_messages, _MAX_PREVIEW_CHARS)
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
            _replace_session_preview(conn, sid, preview)
            _replace_session_display_artifacts(conn, sid, transcript_or_messages)
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


def _search_table(
    conn: sqlite3.Connection,
    table: str,
    text_col: str,
    match: str,
    fetch_limit: int,
) -> List[Tuple[str, str, float, str]]:
    if not _table_exists(conn, table):
        return []
    try:
        rows = conn.execute(
            f"SELECT session_id,"
            f" snippet({table}, 1, '', '', '...', 32) AS snip,"
            f" bm25({table}) AS rank,"
            f" '{text_col}' AS kind"
            f" FROM {table}"
            f" WHERE {table} MATCH ?"
            f" ORDER BY rank"
            f" LIMIT ?",
            (match, fetch_limit),
        ).fetchall()
    except Exception:
        return []
    out: List[Tuple[str, str, float, str]] = []
    for session_id, snip, rank, kind in rows:
        sid = str(session_id or "")
        if not sid:
            continue
        try:
            rank_f = float(rank)
        except (TypeError, ValueError):
            rank_f = 0.0
        out.append((sid, (snip or "").strip(), rank_f, str(kind or text_col)))
    return out


def search_sessions(
    state_dir: str,
    query: str,
    limit: int = _DEFAULT_LIMIT,
) -> List[dict]:
    """Search indexed session projections. Empty/whitespace query returns [].

    Each hit: ``{"session_id", "snippet", "rank", "match_kind"}``. Rank is bm25
    (lower is better). One best hit per session_id across transcript, preview,
    and artifact projections.
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
            fetch_limit = cap * 8
            merged_rows: List[Tuple[str, str, float, str]] = []
            merged_rows.extend(
                _search_table(conn, "session_chunks", "transcript", match, fetch_limit)
            )
            merged_rows.extend(
                _search_table(conn, "session_previews", "preview", match, fetch_limit)
            )
            merged_rows.extend(
                _search_table(conn, "artifact_headlines", "artifact", match, fetch_limit)
            )
        finally:
            conn.close()
    except Exception:
        return []

    merged_rows.sort(key=lambda row: row[2])
    best: dict[str, dict] = {}
    order: List[str] = []
    for sid, snip, rank_f, kind in merged_rows:
        hit = {
            "session_id": sid,
            "snippet": snip,
            "rank": rank_f,
            "match_kind": kind,
        }
        prev = best.get(sid)
        if prev is None:
            best[sid] = hit
            order.append(sid)
        elif rank_f < float(prev.get("rank", 0.0)):
            best[sid] = hit
    return [best[sid] for sid in order[:cap]]


def reindex_transcripts(state_dir: str) -> dict:
    """Walk ``transcripts/*.json`` and rebuild transcript/preview FTS rows.

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


def reindex_store_artifacts(
    state_dir: str,
    *,
    workspace_root: str = "",
) -> dict:
    """Rebuild artifact headline projections from read-only store views."""
    stats = {"indexed": 0, "skipped": 0, "errors": 0}
    if not state_dir:
        return stats
    try:
        from .job_scoping import parse_job_session_id
        from .state import DurableState
    except Exception:
        stats["errors"] += 1
        return stats

    stores: List[Any] = []
    try:
        stores.append(DurableState(state_dir))
    except Exception:
        pass
    if workspace_root:
        try:
            from .cli_job_merge import open_cli_durable_state

            cli_state = open_cli_durable_state(workspace_root)
            if cli_state is not None:
                stores.append(cli_state)
        except Exception:
            pass

    seen_jobs: set[str] = set()
    for durable in stores:
        try:
            jobs = durable.list_jobs()
        except Exception:
            stats["errors"] += 1
            continue
        for job in jobs or []:
            jid = str(job.get("id") or "")
            if not jid or jid in seen_jobs:
                continue
            seen_jobs.add(jid)
            label = job.get("label")
            try:
                tasks = durable.store.list_tasks(jid)
            except Exception:
                tasks = []
            sid = _safe_session_id(parse_job_session_id(label, tasks))
            if not sid:
                stats["skipped"] += 1
                continue
            try:
                raw = durable.store.list_artifacts(jid)
                arts = durable.format_artifacts(raw)
            except Exception:
                stats["errors"] += 1
                continue
            if best_effort_index_job_artifacts(
                state_dir,
                jid,
                session_id=sid,
                artifacts=arts,
                durable=durable,
            ):
                stats["indexed"] += 1
            else:
                stats["errors"] += 1
    return stats


def remove_artifact_from_index(
    state_dir: str,
    job_id: str,
    artifact_id: str,
    *,
    session_id: str = "",
) -> bool:
    """Drop one artifact projection row. Never raises."""
    if not state_dir:
        return False
    jid = _safe_ref(job_id)
    aid = _safe_ref(artifact_id)
    if not jid or not aid or not os.path.exists(_db_path(state_dir)):
        return False
    sid = _safe_session_id(session_id)
    try:
        conn = _connect(state_dir)
        try:
            if not _table_exists(conn, "artifact_headlines"):
                return False
            if sid:
                conn.execute(
                    "DELETE FROM artifact_headlines"
                    " WHERE session_id = ? AND job_id = ? AND artifact_id = ?",
                    (sid, jid, aid),
                )
            else:
                conn.execute(
                    "DELETE FROM artifact_headlines WHERE job_id = ? AND artifact_id = ?",
                    (jid, aid),
                )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


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
            if _table_exists(conn, "session_previews"):
                conn.execute(
                    "DELETE FROM session_previews WHERE session_id = ?",
                    (sid,),
                )
            if _table_exists(conn, "artifact_headlines"):
                conn.execute(
                    "DELETE FROM artifact_headlines WHERE session_id = ?",
                    (sid,),
                )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False
