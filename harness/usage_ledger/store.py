"""Append-only SQLite ledger: one row per model call, never updated.

Every dollar Marionette shows is a query over these rows. A row freezes the
facts of one call (who, which account, which model, tokens by class) and the
money derived from them at call time (the rate card in force, the provider's
reported cost). Nothing reprices a row later: a price change, a model switch
or a new plan only affects calls made after it.

``cash_usd`` is money that left the user's wallet for this call:
- metered: the provider's reported cost, else tokens x rate card, else NULL
  (unpriced: shown as unknown, never as $0);
- plan: 0, unless the provider reported a charge (overage);
- local: 0.
``list_usd`` is the same call at the provider's list rate, whatever the
account (the value a plan is delivering), NULL when no rate is published.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    event_id        TEXT PRIMARY KEY,
    recorded_at     REAL NOT NULL,
    session_id      TEXT,
    turn            INTEGER,
    purpose         TEXT NOT NULL,
    job_id          TEXT,
    provider        TEXT NOT NULL,
    account         TEXT NOT NULL,
    billing         TEXT NOT NULL CHECK (billing IN ('metered', 'plan', 'local')),
    model           TEXT NOT NULL,
    served_model    TEXT,
    input_uncached  INTEGER NOT NULL,
    cache_read      INTEGER NOT NULL,
    cache_write_5m  INTEGER NOT NULL,
    cache_write_1h  INTEGER NOT NULL,
    output          INTEGER NOT NULL,
    reasoning       INTEGER NOT NULL,
    token_basis     TEXT NOT NULL,
    cash_usd        REAL,
    list_usd        REAL,
    cost_basis      TEXT NOT NULL CHECK (cost_basis IN ('reported', 'computed', 'unpriced', 'included', 'local')),
    provider_cost_usd REAL,
    rate_source     TEXT,
    rate_version    REAL,
    rates           TEXT,
    error           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS usage_events_session ON usage_events (session_id, recorded_at);
CREATE INDEX IF NOT EXISTS usage_events_account ON usage_events (account, recorded_at);
"""


@dataclass(frozen=True)
class UsageEvent:
    event_id: str
    recorded_at: float
    session_id: Optional[str]
    turn: Optional[int]
    purpose: str
    job_id: Optional[str]
    provider: str
    account: str
    billing: str
    model: str
    served_model: Optional[str]
    input_uncached: int
    cache_read: int
    cache_write_5m: int
    cache_write_1h: int
    output: int
    reasoning: int
    token_basis: str
    cash_usd: Optional[float]
    list_usd: Optional[float]
    cost_basis: str
    provider_cost_usd: Optional[float]
    rate_source: Optional[str]
    rate_version: Optional[float]
    rates: Optional[dict]
    error: bool = False

    @property
    def tokens(self) -> int:
        return self.input_uncached + self.cache_read + self.cache_write_5m + self.cache_write_1h + self.output


_COLUMNS = tuple(UsageEvent.__dataclass_fields__)


class UsageLedger:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            from harness.sqlite_journal import configure_sqlite_connection

            configure_sqlite_connection(conn, self.path)
        except Exception:
            pass
        if not self._ready:
            with self._lock:
                if not self._ready:
                    conn.executescript(_SCHEMA)
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    conn.commit()
                    self._ready = True
        return conn

    def append(self, event: UsageEvent) -> bool:
        """Insert one row. False on an exact replay of the same event_id."""
        row = asdict(event)
        row["rates"] = json.dumps(event.rates, sort_keys=True) if event.rates is not None else None
        row["error"] = 1 if event.error else 0
        placeholders = ", ".join("?" for _ in _COLUMNS)
        with closing(self._connect()) as conn:
            cur = conn.execute(
                f"INSERT OR IGNORE INTO usage_events ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
                [row[c] for c in _COLUMNS],
            )
            conn.commit()
            return cur.rowcount == 1

    def events(self, *, session_id: Optional[str] = None, account: Optional[str] = None,
               since: Optional[float] = None) -> list[UsageEvent]:
        where, args = [], []
        if session_id is not None:
            where.append("session_id = ?")
            args.append(session_id)
        if account is not None:
            where.append("account = ?")
            args.append(account)
        if since is not None:
            where.append("recorded_at >= ?")
            args.append(since)
        sql = f"SELECT {', '.join(_COLUMNS)} FROM usage_events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY recorded_at, event_id"
        with closing(self._connect()) as conn:
            return [_row_to_event(r) for r in conn.execute(sql, args)]

    def fingerprint(self, session_id: str) -> tuple:
        """Changes whenever a row is added for the session (cache keys)."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*), MAX(recorded_at) FROM usage_events WHERE session_id = ?", (session_id,)
            ).fetchone()
        return (int(row[0] or 0), float(row[1] or 0.0))

    def session_ids(self) -> set[str]:
        with closing(self._connect()) as conn:
            return {r[0] for r in conn.execute("SELECT DISTINCT session_id FROM usage_events WHERE session_id IS NOT NULL")}


def _row_to_event(row: Iterable[Any]) -> UsageEvent:
    values = dict(zip(_COLUMNS, row))
    values["rates"] = json.loads(values["rates"]) if values["rates"] else None
    values["error"] = bool(values["error"])
    return UsageEvent(**values)


_LEDGERS: dict[str, UsageLedger] = {}
_LEDGERS_LOCK = threading.Lock()


def ledger_for(state_dir: str) -> UsageLedger:
    path = os.path.join(state_dir, "usage_ledger.sqlite")
    with _LEDGERS_LOCK:
        ledger = _LEDGERS.get(path)
        if ledger is None:
            os.makedirs(state_dir, exist_ok=True)
            ledger = _LEDGERS[path] = UsageLedger(path)
        return ledger
