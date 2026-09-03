"""History compaction journal for conversation summarization events.

Records when the pilot replaces a block of messages with a compressed summary,
mirroring the tool-output savings ledger pattern. Also stores conservative
cache-read / cache-bust telemetry and USD-ready slots (never fabricated cost).
Session-state rows persist the shared fail-until deadline and a cheap
transcript fingerprint (count + role/content lengths, never full bodies).
Stdlib-only; never raises on the hot path.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .tool_output_savings import tokens_avoided

DB_FILENAME = "history_compaction.sqlite"

EVENT_COMPACT = "compact"
EVENT_CACHE_BUST = "cache_bust"
EVENT_CACHE_READ = "cache_read"
EVENT_COMPACT_DEFERRED = "compact_deferred"
EVENT_COMPACT_REFREEZE = "compact_refreeze"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS compactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    messages_compacted INTEGER NOT NULL,
    chars_before INTEGER NOT NULL,
    chars_after INTEGER NOT NULL,
    summary_preview TEXT NOT NULL DEFAULT '',
    event_kind TEXT NOT NULL DEFAULT 'compact',
    tokens_before INTEGER NOT NULL DEFAULT 0,
    tokens_after INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_bust_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL,
    thrash_strikes INTEGER NOT NULL DEFAULT 0,
    savings_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_history_compactions_session
    ON compactions(session_id);
"""

# Session-scoped cooldown / idle-ungrown state (one row per session_id).
_SESSION_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_state (
    session_id TEXT PRIMARY KEY,
    fail_until REAL NOT NULL DEFAULT 0,
    transcript_fp TEXT NOT NULL DEFAULT '',
    transcript_len INTEGER NOT NULL DEFAULT 0
);
"""

# Best-effort additive columns for DBs created before session-state fields landed.
_SESSION_STATE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("fail_until", "REAL NOT NULL DEFAULT 0"),
    ("transcript_fp", "TEXT NOT NULL DEFAULT ''"),
    ("transcript_len", "INTEGER NOT NULL DEFAULT 0"),
)

# Best-effort additive columns for DBs created before telemetry fields landed.
_TELEMETRY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("event_kind", "TEXT NOT NULL DEFAULT 'compact'"),
    ("tokens_before", "INTEGER NOT NULL DEFAULT 0"),
    ("tokens_after", "INTEGER NOT NULL DEFAULT 0"),
    ("cache_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("cache_bust_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("estimated_cost_usd", "REAL"),
    ("thrash_strikes", "INTEGER NOT NULL DEFAULT 0"),
    ("savings_pct", "REAL"),
    ("compact_policy", "TEXT"),
)


@dataclass(frozen=True)
class CompactionSessionState:
    fail_until: float = 0.0
    transcript_fp: str = ""
    transcript_len: int = 0


@dataclass(frozen=True)
class HistoryCompactionSummary:
    record_count: int = 0
    chars_before: int = 0
    chars_after: int = 0
    tokens_saved: int = 0
    cache_read_tokens: int = 0
    cache_bust_tokens: int = 0
    thrash_events: int = 0
    deferred_count: int = 0
    refreeze_count: int = 0
    # Sum of measured USD only; None means no measured cost was journaled.
    estimated_cost_usd: Optional[float] = None


def _db_path(state_dir: str) -> Path:
    return Path(state_dir) / DB_FILENAME


def _table_columns(conn: sqlite3.Connection) -> set[str]:
    """Return current ``compactions`` column names (empty on failure)."""
    try:
        return {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(compactions)").fetchall()
        }
    except Exception:
        return set()


def _session_state_columns(conn: sqlite3.Connection) -> set[str]:
    """Return current ``session_state`` column names (empty on failure)."""
    try:
        return {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(session_state)").fetchall()
        }
    except Exception:
        return set()


def _ensure_session_state_schema(conn: sqlite3.Connection) -> set[str]:
    """Create session_state and add any missing columns (never raises)."""
    try:
        conn.executescript(_SESSION_STATE_SCHEMA)
    except Exception:
        pass
    existing = _session_state_columns(conn)
    if not existing:
        return existing
    for name, decl in _SESSION_STATE_COLUMNS:
        if name in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE session_state ADD COLUMN {name} {decl}")
            existing.add(name)
        except Exception:
            pass
    return existing


def _ensure_schema(conn: sqlite3.Connection) -> set[str]:
    """Create table and add any missing telemetry columns (never raises).

    Returns the column set actually present after best-effort migration so
    callers can INSERT only columns that exist (a partial ALTER must not leave
    a later INSERT requiring columns that were never added).
    """
    try:
        conn.executescript(_SCHEMA)
    except Exception:
        pass
    try:
        _ensure_session_state_schema(conn)
    except Exception:
        pass
    existing = _table_columns(conn)
    if not existing:
        return existing
    for name, decl in _TELEMETRY_COLUMNS:
        if name in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE compactions ADD COLUMN {name} {decl}")
            existing.add(name)
        except Exception:
            # Leave ``existing`` unchanged for this column; INSERT will omit it.
            pass
    return existing


def _content_length(content: object) -> int:
    """Length of a history ``content`` field without reading body text."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    try:
        return len(content)  # type: ignore[arg-type]
    except Exception:
        return 0


def fingerprint_transcript(messages: Optional[list] = None) -> tuple[str, int]:
    """Cheap fingerprint: message count + sha256 of role+content lengths.

    Full bodies are never hashed. Failures return ``("", 0)``.
    """
    try:
        rows = list(messages or [])
    except Exception:
        return "", 0
    try:
        count = len(rows)
        hasher = hashlib.sha256()
        hasher.update(str(count).encode("utf-8"))
        hasher.update(b"\n")
        for msg in rows:
            role = ""
            content: object = None
            if isinstance(msg, dict):
                role = str(msg.get("role") or "")
                content = msg.get("content")
            hasher.update(role.encode("utf-8", errors="replace"))
            hasher.update(b":")
            hasher.update(str(_content_length(content)).encode("utf-8"))
            hasher.update(b"\n")
        return f"{count}:{hasher.hexdigest()}", count
    except Exception:
        return "", 0


def load_compaction_session_state(
    state_dir: str,
    session_id: str,
) -> CompactionSessionState:
    """Load one session-state row. Missing/corrupt data yields defaults."""
    if not state_dir:
        return CompactionSessionState()
    sid = session_id or "default"
    try:
        path = _db_path(state_dir)
        if not path.is_file():
            return CompactionSessionState()
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            cols = _ensure_session_state_schema(conn)
            if not cols:
                return CompactionSessionState()
            select_parts = []
            for name in ("fail_until", "transcript_fp", "transcript_len"):
                if name in cols:
                    select_parts.append(name)
                else:
                    select_parts.append("NULL")
            row = conn.execute(
                f"SELECT {', '.join(select_parts)} FROM session_state "
                f"WHERE session_id = ?",
                (sid,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return CompactionSessionState()
        fail_until = 0.0
        try:
            fail_until = float(row[0] or 0.0)
        except Exception:
            fail_until = 0.0
        transcript_fp = ""
        try:
            if row[1] is not None:
                transcript_fp = str(row[1])
        except Exception:
            transcript_fp = ""
        transcript_len = 0
        try:
            transcript_len = int(row[2] or 0)
        except Exception:
            transcript_len = 0
        return CompactionSessionState(
            fail_until=fail_until,
            transcript_fp=transcript_fp,
            transcript_len=transcript_len,
        )
    except Exception:
        return CompactionSessionState()


def save_compaction_session_state(
    state_dir: str,
    session_id: str,
    *,
    fail_until: Optional[float] = None,
    transcript_fp: Optional[str] = None,
    transcript_len: Optional[int] = None,
) -> None:
    """UPSERT session-state fields. ``None`` keeps the existing value.

    Never raises. No-ops when ``state_dir`` is empty or no field is provided.
    """
    if not state_dir:
        return
    if fail_until is None and transcript_fp is None and transcript_len is None:
        return
    sid = session_id or "default"
    try:
        current = load_compaction_session_state(state_dir, sid)
        until = current.fail_until if fail_until is None else float(fail_until)
        fp = current.transcript_fp if transcript_fp is None else str(transcript_fp)
        tlen = current.transcript_len if transcript_len is None else int(transcript_len)
        path = _db_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            cols = _ensure_session_state_schema(conn)
            if not cols:
                return
            fields = ["session_id"]
            values: list = [sid]
            optional = (
                ("fail_until", until),
                ("transcript_fp", fp),
                ("transcript_len", tlen),
            )
            for name, value in optional:
                if name in cols:
                    fields.append(name)
                    values.append(value)
            placeholders = ",".join("?" * len(fields))
            updates = [f"{name}=excluded.{name}" for name in fields if name != "session_id"]
            sql = (
                f"INSERT INTO session_state ({', '.join(fields)}) "
                f"VALUES ({placeholders})"
            )
            if updates:
                sql += f" ON CONFLICT(session_id) DO UPDATE SET {', '.join(updates)}"
            conn.execute(sql, tuple(values))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _coerce_savings_ratio(savings_pct: Optional[float]) -> Optional[float]:
    """Normalize savings to a 0..1 ratio for journal / event / attempt.

    Values in ``(1, 100]`` are treated as legacy 0..100 percentage points and
    divided by 100. Anything else outside ``[0, 1]`` is clamped.
    """
    if savings_pct is None:
        return None
    try:
        value = float(savings_pct)
    except Exception:
        return None
    if value > 1.0:
        # Compat: older writers stored 0..100 percentage points.
        value = value / 100.0
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def record_history_compaction(
    state_dir: str,
    session_id: str,
    messages_compacted: int,
    chars_before: int,
    chars_after: int,
    summary_preview: str,
    *,
    event_kind: str = EVENT_COMPACT,
    tokens_before: int = 0,
    tokens_after: int = 0,
    cache_read_tokens: int = 0,
    cache_bust_tokens: int = 0,
    estimated_cost_usd: Optional[float] = None,
    thrash_strikes: int = 0,
    savings_pct: Optional[float] = None,
    compact_policy: Optional[str] = None,
) -> None:
    """Append one compaction / cache-signal journal row. Failures are swallowed.

    ``estimated_cost_usd`` is USD-ready: store only a caller-measured value.
    Pass ``None`` (default) rather than inventing a price × tokens product.
    ``savings_pct`` is stored as a 0..1 ratio (legacy 0..100 values coerced).
    """
    kind = (event_kind or EVENT_COMPACT).strip() or EVENT_COMPACT
    # Compact rows still require a positive message count; pure cache signals
    # may journal with messages_compacted=0.
    if not state_dir:
        return
    if kind == EVENT_COMPACT and messages_compacted <= 0:
        return
    _kind_discriminators = (
        EVENT_COMPACT_DEFERRED,
        EVENT_COMPACT_REFREEZE,
        EVENT_CACHE_READ,
        EVENT_CACHE_BUST,
    )
    preview = (summary_preview or "")[:400]
    sid = session_id or "default"
    cost = None
    if estimated_cost_usd is not None:
        try:
            cost = float(estimated_cost_usd)
        except Exception:
            cost = None
    pct = _coerce_savings_ratio(savings_pct)
    policy = (compact_policy or "").strip() or None
    try:
        path = _db_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            cols = _ensure_schema(conn)
            if kind in _kind_discriminators and "event_kind" not in cols:
                # Legacy/partial schema cannot store or summarize these rows
                # without masquerading as compact — decline experimental rows.
                return
            # Base columns from the original schema — always required.
            fields = [
                "session_id",
                "ts",
                "messages_compacted",
                "chars_before",
                "chars_after",
                "summary_preview",
            ]
            values: list = [
                sid,
                time.time(),
                int(messages_compacted),
                int(chars_before),
                int(chars_after),
                preview,
            ]
            # Telemetry is additive; omit any column a partial migration missed.
            optional = (
                ("event_kind", kind),
                ("tokens_before", int(tokens_before or 0)),
                ("tokens_after", int(tokens_after or 0)),
                ("cache_read_tokens", max(0, int(cache_read_tokens or 0))),
                ("cache_bust_tokens", max(0, int(cache_bust_tokens or 0))),
                ("estimated_cost_usd", cost),
                ("thrash_strikes", max(0, int(thrash_strikes or 0))),
                ("savings_pct", pct),
                ("compact_policy", policy),
            )
            for name, value in optional:
                if name in cols:
                    fields.append(name)
                    values.append(value)
            placeholders = ",".join("?" * len(fields))
            conn.execute(
                f"INSERT INTO compactions ({', '.join(fields)}) "
                f"VALUES ({placeholders})",
                tuple(values),
            )
            conn.commit()
        finally:
            conn.close()
        try:
            from .observability_export import export_history_compaction

            export_history_compaction(
                session_id=sid,
                event_kind=kind,
                messages_compacted=int(messages_compacted),
                chars_before=int(chars_before),
                chars_after=int(chars_after),
                tokens_before=int(tokens_before or 0),
                tokens_after=int(tokens_after or 0),
                cache_read_tokens=max(0, int(cache_read_tokens or 0)),
                cache_bust_tokens=max(0, int(cache_bust_tokens or 0)),
                estimated_cost_usd=cost,
                savings_pct=pct,
                compact_policy=policy,
                basis="measured" if cost is not None else "estimated",
            )
        except Exception:
            pass
    except Exception:
        pass


def record_compact_deferred(
    state_dir: str,
    session_id: str,
    *,
    cache_read_tokens: int = 0,
    compact_policy: str = "defer",
    warm_detail: Optional[dict] = None,
) -> None:
    """Journal a cache-warm compaction deferral (experiment telemetry only)."""
    preview = ""
    if warm_detail:
        try:
            warm_reason = str(warm_detail.get("warm_reason") or "")
            last_read = max(0, int(warm_detail.get("last_turn_cache_read_tokens") or 0))
            parts = [p for p in (warm_reason, f"last_turn_cache_read_tokens={last_read}" if last_read else "") if p]
            preview = "; ".join(parts)[:400]
        except Exception:
            preview = ""
    record_history_compaction(
        state_dir,
        session_id,
        messages_compacted=0,
        chars_before=0,
        chars_after=0,
        summary_preview=preview,
        event_kind=EVENT_COMPACT_DEFERRED,
        cache_read_tokens=0,
        cache_bust_tokens=0,
        estimated_cost_usd=None,
        compact_policy=compact_policy,
    )


def record_compact_refreeze(
    state_dir: str,
    session_id: str,
    *,
    tokens_before: int = 0,
    tokens_after: int = 0,
    cache_bust_tokens: int = 0,
) -> None:
    """Journal post-compaction refreeze (append-only reset; no fabricated USD)."""
    record_history_compaction(
        state_dir,
        session_id,
        messages_compacted=0,
        chars_before=0,
        chars_after=0,
        summary_preview="",
        event_kind=EVENT_COMPACT_REFREEZE,
        tokens_before=int(tokens_before or 0),
        tokens_after=int(tokens_after or 0),
        cache_read_tokens=0,
        cache_bust_tokens=max(0, int(cache_bust_tokens or 0)),
        estimated_cost_usd=None,
        compact_policy="refreeze",
    )


def record_cache_signal(
    state_dir: str,
    session_id: str,
    *,
    event_kind: str,
    cache_read_tokens: int = 0,
    cache_bust_tokens: int = 0,
    tokens_before: int = 0,
    tokens_after: int = 0,
    estimated_cost_usd: Optional[float] = None,
) -> None:
    """Journal a cache-read or cache-bust signal without a compaction body."""
    kind = (event_kind or "").strip()
    if kind not in (EVENT_CACHE_READ, EVENT_CACHE_BUST):
        return
    record_history_compaction(
        state_dir,
        session_id,
        messages_compacted=0,
        chars_before=0,
        chars_after=0,
        summary_preview="",
        event_kind=kind,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        cache_read_tokens=cache_read_tokens,
        cache_bust_tokens=cache_bust_tokens,
        estimated_cost_usd=estimated_cost_usd,
    )


def summarize_history_compactions(
    state_dir: str,
    session_id: Optional[str] = None,
) -> HistoryCompactionSummary:
    """Aggregate journal rows, optionally scoped to one session."""
    if not state_dir:
        return HistoryCompactionSummary()
    has_event_kind = False
    try:
        path = _db_path(state_dir)
        if not path.is_file():
            return HistoryCompactionSummary()
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            cols = _ensure_schema(conn)
            has_event_kind = "event_kind" in cols
            # Column-aware SELECT: each telemetry expression is built from
            # columns that actually exist. A partial migration (e.g.
            # event_kind present, cache_* absent) must still distinguish
            # compact vs cache_bust rows and must not invent aggregate values
            # for missing fields.
            cread_expr = (
                "COALESCE(cache_read_tokens, 0)"
                if "cache_read_tokens" in cols
                else "0"
            )
            cbust_expr = (
                "COALESCE(cache_bust_tokens, 0)"
                if "cache_bust_tokens" in cols
                else "0"
            )
            strikes_expr = (
                "COALESCE(thrash_strikes, 0)"
                if "thrash_strikes" in cols
                else "0"
            )
            cost_expr = (
                "estimated_cost_usd" if "estimated_cost_usd" in cols else "NULL"
            )
            kind_expr = (
                "COALESCE(event_kind, 'compact')"
                if "event_kind" in cols
                else "NULL"
            )
            select = (
                "messages_compacted, chars_before, chars_after, "
                f"{cread_expr}, {cbust_expr}, {strikes_expr}, "
                f"{cost_expr}, {kind_expr}"
            )
            if session_id:
                rows = conn.execute(
                    f"SELECT {select} FROM compactions WHERE session_id = ?",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {select} FROM compactions"
                ).fetchall()
        finally:
            conn.close()
    except Exception:
        return HistoryCompactionSummary()

    chars_before = 0
    chars_after = 0
    tokens_saved = 0
    cache_read = 0
    cache_bust = 0
    thrash_events = 0
    deferred_count = 0
    refreeze_count = 0
    cost_sum = 0.0
    cost_seen = False
    compact_rows = 0
    for msgs, before, after, cread, cbust, strikes, cost, kind in rows:
        msgs_i = int(msgs or 0)
        if has_event_kind:
            kind_s = str(kind or EVENT_COMPACT)
        elif msgs_i > 0:
            kind_s = EVENT_COMPACT
        else:
            # Without event_kind, zero-message rows are indistinguishable
            # non-compact signals — never masquerade as compact/deferred.
            kind_s = ""
        if kind_s == EVENT_COMPACT:
            compact_rows += 1
            chars_before += int(before)
            chars_after += int(after)
            tokens_saved += tokens_avoided(int(before), int(after))
        elif kind_s == EVENT_COMPACT_DEFERRED:
            deferred_count += 1
        elif kind_s == EVENT_COMPACT_REFREEZE:
            refreeze_count += 1
        if kind_s != EVENT_COMPACT_DEFERRED:
            cache_read += int(cread or 0)
            cache_bust += int(cbust or 0)
        if int(strikes or 0) > 0:
            thrash_events += 1
        if cost is not None:
            try:
                cost_sum += float(cost)
                cost_seen = True
            except Exception:
                pass
    return HistoryCompactionSummary(
        record_count=compact_rows,
        chars_before=chars_before,
        chars_after=chars_after,
        tokens_saved=tokens_saved,
        cache_read_tokens=cache_read,
        cache_bust_tokens=cache_bust,
        thrash_events=thrash_events,
        deferred_count=deferred_count,
        refreeze_count=refreeze_count,
        estimated_cost_usd=cost_sum if cost_seen else None,
    )


def history_compaction_payload(
    state_dir: str,
    session_id: str,
) -> dict:
    """Compact fields for usage/session APIs.

    When compaction has already run this session, surface a soft honesty flag
    so the UI can show that history was rewritten (OMP-style intervention cue).
    Full pressure badges still come from ``compaction_advice``.
    Cache-bust / thrash counters are USD-ready telemetry for later UI chips;
    ``history_compaction_cost_usd`` is omitted unless a measured cost exists.
    """
    summary = summarize_history_compactions(state_dir, session_id or None)
    payload: dict = {
        "history_compactions": summary.record_count,
        "history_tokens_saved": summary.tokens_saved,
        "history_cache_read_tokens": summary.cache_read_tokens,
        "history_cache_bust_tokens": summary.cache_bust_tokens,
        "history_thrash_events": summary.thrash_events,
        "history_compaction_deferred": summary.deferred_count,
        "history_compaction_refreeze": summary.refreeze_count,
    }
    if summary.record_count > 0:
        payload["history_compaction_ran"] = True
    if summary.estimated_cost_usd is not None:
        payload["history_compaction_cost_usd"] = summary.estimated_cost_usd
    return payload
