"""OMP-inspired token savings ledger for compacted/truncated tool outputs.

Deterministic accounting: tokens avoided = chars//4(original) - chars//4(compact),
matching the harness context-meter heuristic. Records are append-only, deduped
by (session_id, tool_call_id), and stored under the session state dir.

Primary backend: SQLite (UNIQUE constraint, WAL, threaded lock). Optional JSONL
audit mirror when HARNESS_TOOL_OUTPUT_SAVINGS_JSONL=1.

Never raises from the hot-path record helpers — a failed write must not block
tool execution or model turns.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# Same crude chars→tokens ratio used by ConversationalSession context estimates.
CHARS_PER_TOKEN = 4

DB_FILENAME = "tool_output_savings.sqlite"
JSONL_FILENAME = "tool_output_savings.jsonl"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_output_savings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    original_chars INTEGER NOT NULL,
    compact_chars INTEGER NOT NULL,
    tokens_saved INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    job_id TEXT,
    UNIQUE(session_id, tool_call_id)
);
CREATE INDEX IF NOT EXISTS idx_tool_output_savings_session
    ON tool_output_savings(session_id);
"""


@dataclass(frozen=True)
class ToolOutputSavingsSummary:
    tokens_saved: int = 0
    chars_saved: int = 0
    record_count: int = 0
    by_reason: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.by_reason is None:
            object.__setattr__(self, "by_reason", {})


def estimate_tokens(char_count: int) -> int:
    """Deterministic token estimate from character count."""
    return max(0, int(char_count) // CHARS_PER_TOKEN)


def tokens_avoided(original_chars: int, compact_chars: int) -> int:
    """Tokens avoided by compacting ``original_chars`` down to ``compact_chars``."""
    return max(0, estimate_tokens(original_chars) - estimate_tokens(compact_chars))


def savings_usd(tokens_saved: int, price_in_per_mtok: float) -> float:
    """USD value of avoided input-context tokens at the given input price."""
    return (float(tokens_saved) / 1.0e6) * float(price_in_per_mtok)


def _jsonl_enabled() -> bool:
    return os.environ.get("HARNESS_TOOL_OUTPUT_SAVINGS_JSONL", "0").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def parse_jsonl_records(path: str | Path) -> list[dict]:
    """Load savings records from a JSONL file, skipping blank/malformed lines."""
    p = Path(path)
    if not p.is_file():
        return []
    out: list[dict] = []
    try:
        with p.open(encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return []
    return out


def aggregate_jsonl_records(
    records: list[dict],
    *,
    session_id: Optional[str] = None,
) -> ToolOutputSavingsSummary:
    """Aggregate JSONL records with dedupe by (session_id, tool_call_id).

    When duplicate keys appear, the first record wins (append-only semantics).
    """
    seen: set[tuple[str, str]] = set()
    tokens = 0
    chars = 0
    count = 0
    by_reason: dict[str, int] = {}
    for rec in records:
        sid = str(rec.get("session_id") or "")
        tcid = str(rec.get("tool_call_id") or "")
        if session_id is not None and sid != session_id:
            continue
        key = (sid, tcid)
        if not tcid or key in seen:
            continue
        seen.add(key)
        orig = int(rec.get("original_chars") or 0)
        compact = int(rec.get("compact_chars") or 0)
        saved = int(rec.get("tokens_saved") or tokens_avoided(orig, compact))
        if saved <= 0:
            continue
        tokens += saved
        chars += max(0, orig - compact)
        count += 1
        reason = str(rec.get("reason") or "unknown")
        by_reason[reason] = by_reason.get(reason, 0) + saved
    return ToolOutputSavingsSummary(
        tokens_saved=tokens,
        chars_saved=chars,
        record_count=count,
        by_reason=by_reason,
    )


CompactionCallback = Callable[[int, int, str], None]


def make_compaction_callback(
    *,
    state_dir: str,
    session_id: str,
    tool_call_id: str,
    job_id: Optional[str] = None,
) -> CompactionCallback:
    """Build a callback for context_budget hooks."""

    def _cb(original_chars: int, compact_chars: int, reason: str) -> None:
        try_record(
            state_dir=state_dir,
            session_id=session_id,
            tool_call_id=tool_call_id,
            original_chars=original_chars,
            compact_chars=compact_chars,
            reason=reason,
            job_id=job_id,
        )

    return _cb


class ToolOutputSavingsLedger:
    """SQLite-backed savings ledger under ``state_dir``."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = os.path.abspath(state_dir)
        self._db_path = os.path.join(self.state_dir, DB_FILENAME)
        self._jsonl_path = os.path.join(self.state_dir, JSONL_FILENAME)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def _ensure_db(self) -> None:
        if self._conn is not None:
            return
        os.makedirs(self.state_dir, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, timeout=30.0, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate_schema()
        self._conn.commit()

    def _migrate_schema(self) -> None:
        assert self._conn is not None
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(tool_output_savings)")}
        if "job_id" not in cols:
            self._conn.execute("ALTER TABLE tool_output_savings ADD COLUMN job_id TEXT")

    def _append_jsonl(self, rec: dict) -> None:
        if not _jsonl_enabled():
            return
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(self._jsonl_path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def record(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        original_chars: int,
        compact_chars: int,
        reason: str = "compact",
        job_id: Optional[str] = None,
    ) -> bool:
        """Append one savings record. Returns True when a new row was inserted."""
        saved = tokens_avoided(original_chars, compact_chars)
        if saved <= 0 or not tool_call_id:
            return False
        sid = session_id or "default"
        ts = time.time()
        rec = {
            "ts": ts,
            "session_id": sid,
            "tool_call_id": tool_call_id,
            "original_chars": int(original_chars),
            "compact_chars": int(compact_chars),
            "tokens_saved": saved,
            "reason": reason or "compact",
            "job_id": job_id or "",
        }
        inserted = False
        try:
            with self._lock:
                self._ensure_db()
                assert self._conn is not None
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO tool_output_savings "
                    "(ts, session_id, tool_call_id, original_chars, compact_chars, "
                    "tokens_saved, reason, job_id) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        ts,
                        sid,
                        tool_call_id,
                        int(original_chars),
                        int(compact_chars),
                        saved,
                        reason or "compact",
                        job_id or None,
                    ),
                )
                self._conn.commit()
                inserted = cur.rowcount > 0
        except Exception:
            return False
        finally:
            self.close()
        if inserted:
            self._append_jsonl(rec)
        return inserted

    def summarize(
        self,
        *,
        session_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> ToolOutputSavingsSummary:
        """Aggregate stored records, optionally scoped to one session or job."""
        try:
            with self._lock:
                self._ensure_db()
                assert self._conn is not None
                if session_id and job_id:
                    rows = self._conn.execute(
                        "SELECT tokens_saved, original_chars, compact_chars, reason "
                        "FROM tool_output_savings WHERE session_id = ? AND job_id = ?",
                        (session_id, job_id),
                    ).fetchall()
                elif session_id:
                    rows = self._conn.execute(
                        "SELECT tokens_saved, original_chars, compact_chars, reason "
                        "FROM tool_output_savings WHERE session_id = ?",
                        (session_id,),
                    ).fetchall()
                elif job_id:
                    rows = self._conn.execute(
                        "SELECT tokens_saved, original_chars, compact_chars, reason "
                        "FROM tool_output_savings WHERE job_id = ?",
                        (job_id,),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        "SELECT tokens_saved, original_chars, compact_chars, reason "
                        "FROM tool_output_savings"
                    ).fetchall()
        except Exception:
            # Fall back to JSONL aggregate when SQLite is unreadable.
            records = parse_jsonl_records(self._jsonl_path)
            return aggregate_jsonl_records(records, session_id=session_id)
        finally:
            self.close()

        tokens = 0
        chars = 0
        by_reason: dict[str, int] = {}
        for saved, orig, compact, reason in rows:
            tokens += int(saved)
            chars += max(0, int(orig) - int(compact))
            r = str(reason or "unknown")
            by_reason[r] = by_reason.get(r, 0) + int(saved)
        return ToolOutputSavingsSummary(
            tokens_saved=tokens,
            chars_saved=chars,
            record_count=len(rows),
            by_reason=by_reason,
        )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None


# Process-wide ledger cache keyed by normalized state_dir path.
_LEDGER_CACHE: dict[str, ToolOutputSavingsLedger] = {}
_LEDGER_CACHE_LOCK = threading.Lock()


def get_ledger(state_dir: str) -> ToolOutputSavingsLedger:
    key = os.path.abspath(state_dir)
    with _LEDGER_CACHE_LOCK:
        ledger = _LEDGER_CACHE.get(key)
        if ledger is None:
            ledger = ToolOutputSavingsLedger(key)
            _LEDGER_CACHE[key] = ledger
        return ledger


def try_record(
    *,
    state_dir: str,
    session_id: str,
    tool_call_id: str,
    original_chars: int,
    compact_chars: int,
    reason: str = "compact",
    job_id: Optional[str] = None,
) -> None:
    """Hot-path helper: record savings, swallowing all errors."""
    if tokens_avoided(original_chars, compact_chars) <= 0:
        return
    try:
        get_ledger(state_dir).record(
            session_id=session_id or "default",
            tool_call_id=tool_call_id,
            original_chars=original_chars,
            compact_chars=compact_chars,
            reason=reason,
            job_id=job_id,
        )
    except Exception:
        pass


def session_savings_payload(
    state_dir: str,
    session_id: str,
    price_in: float,
) -> dict:
    """Build API-facing savings fields for a session."""
    try:
        summary = get_ledger(state_dir).summarize(session_id=session_id or None)
    except Exception:
        summary = ToolOutputSavingsSummary()
    usd = savings_usd(summary.tokens_saved, price_in)
    return {
        "tool_output_tokens_saved": summary.tokens_saved,
        "tool_output_savings_usd": round(usd, 6),
        "tool_output_compactions": summary.record_count,
    }


def job_savings_payload(state_dir: str, job_id: str) -> dict:
    """Build API-facing savings fields for one swarm job."""
    if not job_id:
        return {
            "tool_output_tokens_saved": 0,
            "tool_output_savings_usd": 0.0,
            "tool_output_compactions": 0,
        }
    try:
        summary = get_ledger(state_dir).summarize(job_id=job_id)
    except Exception:
        summary = ToolOutputSavingsSummary()
    return {
        "tool_output_tokens_saved": summary.tokens_saved,
        "tool_output_compactions": summary.record_count,
    }
