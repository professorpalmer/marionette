from __future__ import annotations

"""ScheduleStore: durable sqlite persistence for schedules and their run log.

WHY sqlite (not JSON like memory_store): schedules accumulate a run history and
are queried by predicates (enabled-only, runs-for-a-schedule, most-recent-N).
A relational store gives us cheap indexed reads and atomic row updates without
rewriting a whole file on every tick. We follow the exact house pattern from
memory_store.py / command_store.py: a thin class wrapping a sqlite path, tables
created idempotently in __init__, simple typed methods, and a stable default
path under the harness state dir with an explicit-path override for tests.

Default path honors HARNESS_STATE_DIR, else ~/.pmharness/state/schedules.sqlite.
Under pytest (PYTEST_CURRENT_TEST) with no HARNESS_STATE_DIR, the path is a
deterministic per-test directory under the OS temp dir — never Home.
A one-time SQLite backup migrates a legacy ~/.harness/schedules.sqlite when the
new default is absent (WAL-aware, atomic replace). Explicit constructor paths
are never rewritten.
"""

import hashlib
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional, Union

from .schedule_core import SCHEDULE_FIELDS, Schedule

DEFAULT_LEASE_SECONDS = 3600
# Headroom beyond an explicit run ceiling so a short max_seconds cannot shrink
# the claim fence to the ceiling alone (cooperative renewals need room).
LEASE_HEADROOM_SECONDS = 300


def claim_lease_seconds(max_seconds: int = 0) -> int:
    """Compute claim lease lifetime from an optional run ceiling.

    Never returns a bare short ``max_seconds`` value. Always at least
    ``DEFAULT_LEASE_SECONDS``; when a positive ceiling is set, use
    ``max(DEFAULT_LEASE_SECONDS, ceiling + LEASE_HEADROOM_SECONDS)``.
    """
    ceiling = int(max_seconds or 0)
    if ceiling > 0:
        return max(DEFAULT_LEASE_SECONDS, ceiling + LEASE_HEADROOM_SECONDS)
    return DEFAULT_LEASE_SECONDS


def _pytest_test_db_path(pytest_current_test: str) -> Path:
    """Deterministic per-test schedules DB under the OS temp directory."""
    # Strip pytest phase suffix: "nodeid (call)" / "nodeid (setup)" / ...
    # Use rsplit so node ids / paths containing spaces do not collide.
    test_id = pytest_current_test.strip().rsplit(" (", 1)[0]
    digest = hashlib.sha256(test_id.encode("utf-8")).hexdigest()[:16]
    return (
        Path(tempfile.gettempdir())
        / "pmharness-schedule-tests"
        / digest
        / "schedules.sqlite"
    )


def _migrate_legacy_db(legacy: Path, new_default: Path) -> None:
    """Copy legacy DB via SQLite backup + atomic replace (WAL-safe).

    Never overwrites an existing destination. Never mutates or deletes the
    legacy source. On failure, leaves the destination absent so a fresh DB
    may be created later without claiming a partial copy is authoritative.
    """
    if new_default.exists() or not legacy.is_file():
        return
    try:
        new_default.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    tmp = new_default.parent / (new_default.name + ".migrating")
    try:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                return
        # as_uri() percent-encodes spaces / # / ? so Windows paths stay valid.
        src = sqlite3.connect(f"{legacy.resolve().as_uri()}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
                dst.commit()
            finally:
                dst.close()
        finally:
            src.close()
        os.replace(str(tmp), str(new_default))
    except (OSError, sqlite3.Error):
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def default_db_path() -> Path:
    """Resolve the durable schedules DB path (never pollutes Home in tests).

    Order:
      1. HARNESS_STATE_DIR/schedules.sqlite when set
      2. Under pytest: OS-temp per-test path (never Home)
      3. ~/.pmharness/state/schedules.sqlite
      4. One-time WAL-safe backup from legacy ~/.harness/schedules.sqlite
    """
    explicit = (os.environ.get("HARNESS_STATE_DIR") or "").strip()
    if explicit:
        return Path(explicit) / "schedules.sqlite"

    pytest_test = (os.environ.get("PYTEST_CURRENT_TEST") or "").strip()
    if pytest_test:
        return _pytest_test_db_path(pytest_test)

    new_default = Path.home() / ".pmharness" / "state" / "schedules.sqlite"
    legacy = Path.home() / ".harness" / "schedules.sqlite"
    if not new_default.exists() and legacy.is_file():
        _migrate_legacy_db(legacy, new_default)
    return new_default


# Backward-compatible name; computed at call time via default_db_path().
DEFAULT_DB_PATH = Path(os.path.expanduser("~/.pmharness/state/schedules.sqlite"))

# remove() outcomes (truthy when the schedule was found).
REMOVE_REMOVED = "removed"
REMOVE_CANCEL_REQUESTED = "cancel_requested"
REMOVE_STALE_RECOVERED = "stale_recovered"


def _validate_ceilings(values: dict) -> None:
    """Reject negative resource ceilings at the persistence boundary."""
    for field in ("max_tokens", "max_seconds", "max_swarms"):
        if field in values and int(values[field] or 0) < 0:
            raise ValueError(f"{field} must be non-negative")


class ScheduleStore:
    def __init__(self, path: Optional[str] = None) -> None:
        if path is not None:
            self.path = Path(path)
        else:
            self.path = default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), timeout=30.0, check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._init_db()

    def _init_db(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                objective TEXT NOT NULL,
                cron TEXT NOT NULL,
                repo TEXT NOT NULL DEFAULT '',
                swarm_adapter TEXT NOT NULL DEFAULT 'demo',
                driver TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                max_tokens INTEGER NOT NULL DEFAULT 0,
                max_seconds INTEGER NOT NULL DEFAULT 0,
                max_swarms INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT 0,
                enabled_at REAL NOT NULL DEFAULT 0,
                last_run_at REAL NOT NULL DEFAULT 0,
                last_fire_at REAL NOT NULL DEFAULT 0,
                last_status TEXT NOT NULL DEFAULT '',
                claim_owner TEXT NOT NULL DEFAULT '',
                claim_at REAL NOT NULL DEFAULT 0,
                claim_lease_until REAL NOT NULL DEFAULT 0,
                claim_fire_at REAL NOT NULL DEFAULT 0,
                claim_run_id TEXT NOT NULL DEFAULT '',
                cancel_requested INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schedule_runs (
                id TEXT PRIMARY KEY,
                schedule_id TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                halt_reason TEXT NOT NULL DEFAULT '',
                cycles INTEGER NOT NULL DEFAULT 0,
                tokens_used INTEGER NOT NULL DEFAULT 0,
                swarms_used INTEGER NOT NULL DEFAULT 0,
                fire_at REAL NOT NULL DEFAULT 0,
                owner TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._migrate_schema()
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_sched "
            "ON schedule_runs(schedule_id, started_at)"
        )
        self._conn.commit()

    def _migrate_schema(self) -> None:
        """Non-destructive column adds for older schedules.sqlite files."""
        sched_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(schedules)")
        }
        for col, decl in (
            ("enabled_at", "REAL NOT NULL DEFAULT 0"),
            ("last_fire_at", "REAL NOT NULL DEFAULT 0"),
            ("claim_owner", "TEXT NOT NULL DEFAULT ''"),
            ("claim_at", "REAL NOT NULL DEFAULT 0"),
            ("claim_lease_until", "REAL NOT NULL DEFAULT 0"),
            ("claim_fire_at", "REAL NOT NULL DEFAULT 0"),
            ("claim_run_id", "TEXT NOT NULL DEFAULT ''"),
            ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in sched_cols:
                self._conn.execute(
                    f"ALTER TABLE schedules ADD COLUMN {col} {decl}"
                )

        run_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(schedule_runs)")
        }
        for col, decl in (
            ("fire_at", "REAL NOT NULL DEFAULT 0"),
            ("owner", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in run_cols:
                self._conn.execute(
                    f"ALTER TABLE schedule_runs ADD COLUMN {col} {decl}"
                )

        # Backfill fire identity from wall-clock last_run when absent.
        self._conn.execute(
            "UPDATE schedules SET last_fire_at = last_run_at "
            "WHERE last_fire_at = 0 AND last_run_at > 0"
        )
        self._conn.execute(
            "UPDATE schedules SET enabled_at = created_at "
            "WHERE enabled_at = 0 AND created_at > 0 AND enabled = 1"
        )

    def add(self, schedule: Schedule) -> Schedule:
        """Insert a schedule. If id/created_at are unset, they are generated."""
        _validate_ceilings(schedule.to_row())
        now = time.time()
        if not schedule.id:
            schedule.id = uuid.uuid4().hex[:8]
        if not schedule.created_at:
            schedule.created_at = now
        if not schedule.enabled_at and schedule.enabled:
            schedule.enabled_at = schedule.created_at
        row = schedule.to_row()
        cols = ",".join(SCHEDULE_FIELDS)
        placeholders = ",".join("?" for _ in SCHEDULE_FIELDS)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO schedules ({cols}) VALUES ({placeholders})",
                [row[f] for f in SCHEDULE_FIELDS],
            )
            self._conn.commit()
        return schedule

    def list(self, enabled_only: bool = False) -> List[Schedule]:
        sql = "SELECT * FROM schedules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY created_at ASC"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [Schedule.from_row(dict(r)) for r in rows]

    def get(self, schedule_id: str) -> Optional[Schedule]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        return Schedule.from_row(dict(row)) if row else None

    def remove(self, schedule_id: str) -> Union[str, bool]:
        """Remove a schedule, or defer / recover when a claim is attached.

        Fresh claim (lease still valid): atomically disable, set
        ``cancel_requested``, preserve row + run history, return
        ``"cancel_requested"``. Stale claim (``claim_lease_until <= now``):
        in the same transaction mark the still-running row interrupted,
        clear claim fields, disable, preserve history, set truthful
        ``last_status``, and return ``"stale_recovered"`` so a later remove
        can purge (disabled rows are excluded from ``run_due``, so production
        never recovers via claim otherwise). Idle: atomically delete the
        schedule and its runs and return ``"removed"``. Returns ``False``
        when the id is unknown.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
                ).fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return False
                sched = Schedule.from_row(dict(row))
                if sched.claim_owner:
                    now_ts = time.time()
                    if sched.claim_lease_until <= now_ts:
                        # Stale claim: recover locally — do not leave claim_owner
                        # set on a disabled row (run_due would never reclaim it).
                        if sched.claim_run_id:
                            self._conn.execute(
                                """
                                UPDATE schedule_runs
                                SET status = 'interrupted',
                                    ended_at = ?,
                                    halt_reason = 'stale claim recovered'
                                WHERE id = ? AND status = 'running'
                                """,
                                (now_ts, sched.claim_run_id),
                            )
                        self._conn.execute(
                            """
                            UPDATE schedules SET
                                enabled = 0,
                                cancel_requested = 0,
                                claim_owner = '',
                                claim_at = 0,
                                claim_lease_until = 0,
                                claim_fire_at = 0,
                                claim_run_id = '',
                                last_status = 'interrupted',
                                last_run_at = ?
                            WHERE id = ?
                            """,
                            (now_ts, schedule_id),
                        )
                        self._conn.execute("COMMIT")
                        return REMOVE_STALE_RECOVERED
                    self._conn.execute(
                        """
                        UPDATE schedules SET
                            enabled = 0,
                            cancel_requested = 1
                        WHERE id = ?
                        """,
                        (schedule_id,),
                    )
                    self._conn.execute("COMMIT")
                    return REMOVE_CANCEL_REQUESTED
                self._conn.execute(
                    "DELETE FROM schedule_runs WHERE schedule_id = ?",
                    (schedule_id,),
                )
                cur = self._conn.execute(
                    "DELETE FROM schedules WHERE id = ?", (schedule_id,)
                )
                self._conn.execute("COMMIT")
                return REMOVE_REMOVED if cur.rowcount > 0 else False
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def set_enabled(self, schedule_id: str, enabled: bool) -> bool:
        with self._lock:
            if not enabled:
                self._conn.execute(
                    """
                    UPDATE schedules SET cancel_requested = 1
                    WHERE id = ? AND claim_owner != ''
                    """,
                    (schedule_id,),
                )
            now = time.time()
            if enabled:
                cur = self._conn.execute(
                    "UPDATE schedules SET enabled = 1, enabled_at = ? WHERE id = ?",
                    (now, schedule_id),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE schedules SET enabled = 0 WHERE id = ?",
                    (schedule_id,),
                )
            self._conn.commit()
            return cur.rowcount > 0

    def update_fields(self, schedule_id: str, **fields) -> Optional[Schedule]:
        """Update editable schedule fields. Returns the row, or None if missing."""
        allowed = {
            "name", "objective", "cron", "repo", "driver", "swarm_adapter",
            "max_tokens", "max_seconds", "max_swarms",
        }
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return self.get(schedule_id)
        _validate_ceilings(updates)
        if "cron" in updates:
            from .schedule_core import CronExpr
            CronExpr.parse(str(updates["cron"]))  # validate; raises ValueError
        cols = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [schedule_id]
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE schedules SET {cols} WHERE id = ?", vals
            )
            self._conn.commit()
            if cur.rowcount <= 0:
                return None
            row = self._conn.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        return Schedule.from_row(dict(row)) if row else None

    def update_last_run(self, schedule_id: str, status: str, ts: float) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE schedules SET last_status = ?, last_run_at = ? WHERE id = ?",
                (status, ts, schedule_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_status(self, schedule_id: str, status: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE schedules SET last_status = ? WHERE id = ?",
                (status, schedule_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def record_run(
        self,
        schedule_id: str,
        started_at: float,
        ended_at: float,
        status: str,
        halt_reason: str = "",
        cycles: int = 0,
        tokens_used: int = 0,
        swarms_used: int = 0,
        fire_at: float = 0.0,
        owner: str = "",
    ) -> str:
        """Legacy insert of a finished run (tests / tools). Prefer claim APIs."""
        run_id = uuid.uuid4().hex[:8]
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO schedule_runs
                    (id, schedule_id, started_at, ended_at, status,
                     halt_reason, cycles, tokens_used, swarms_used, fire_at, owner)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, schedule_id, started_at, ended_at, status,
                 halt_reason, int(cycles), int(tokens_used), int(swarms_used),
                 float(fire_at), owner),
            )
            self._conn.commit()
        return run_id

    def try_claim(
        self,
        schedule_id: str,
        fire_at: float,
        owner: str,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: Optional[float] = None,
        force: bool = False,
    ) -> Optional[dict]:
        """Transactionally claim a schedule before run_auto.

        Returns a claim dict with run_id on success, or None when a fresh claim
        already holds the schedule (or this fire was already completed).
        Stale claims are recovered: the prior running row becomes ``interrupted``.
        ``force=True`` skips the last_fire_at gate (manual run-now) but still
        fences against a live claim.
        """
        now_ts = time.time() if now is None else float(now)
        lease_until = now_ts + max(1, int(lease_seconds))
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
                ).fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return None
                sched = Schedule.from_row(dict(row))

                if (
                    not force
                    and sched.last_fire_at
                    and fire_at <= sched.last_fire_at
                ):
                    self._conn.execute("ROLLBACK")
                    return None

                if sched.claim_owner and sched.claim_lease_until > now_ts:
                    self._conn.execute("ROLLBACK")
                    return None

                if sched.claim_owner and sched.claim_run_id:
                    # Stale claim: prior run was interrupted.
                    self._conn.execute(
                        """
                        UPDATE schedule_runs
                        SET status = 'interrupted',
                            ended_at = ?,
                            halt_reason = 'stale claim recovered'
                        WHERE id = ? AND status = 'running'
                        """,
                        (now_ts, sched.claim_run_id),
                    )
                    if sched.last_status == "running":
                        self._conn.execute(
                            "UPDATE schedules SET last_status = ? WHERE id = ?",
                            ("interrupted", schedule_id),
                        )

                run_id = uuid.uuid4().hex[:8]
                self._conn.execute(
                    """
                    INSERT INTO schedule_runs
                        (id, schedule_id, started_at, ended_at, status,
                         halt_reason, cycles, tokens_used, swarms_used, fire_at, owner)
                    VALUES (?, ?, ?, 0, 'running', '', 0, 0, 0, ?, ?)
                    """,
                    (run_id, schedule_id, now_ts, float(fire_at), owner),
                )
                self._conn.execute(
                    """
                    UPDATE schedules SET
                        claim_owner = ?,
                        claim_at = ?,
                        claim_lease_until = ?,
                        claim_fire_at = ?,
                        claim_run_id = ?,
                        cancel_requested = 0,
                        last_status = 'running'
                    WHERE id = ?
                    """,
                    (owner, now_ts, lease_until, float(fire_at), run_id, schedule_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

        return {
            "run_id": run_id,
            "schedule_id": schedule_id,
            "fire_at": float(fire_at),
            "owner": owner,
            "claimed_at": now_ts,
            "lease_until": lease_until,
            "started_at": now_ts,
            "status": "running",
        }

    def renew_claim(
        self,
        schedule_id: str,
        run_id: str,
        lease_seconds: int,
        now: Optional[float] = None,
    ) -> bool:
        """Extend the claim lease when this run_id still owns the schedule.

        Cooperative only: cannot interrupt a blocked provider call. Returns
        False without mutation when the claim fence does not match.
        """
        now_ts = time.time() if now is None else float(now)
        lease_until = now_ts + max(1, int(lease_seconds))
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT claim_run_id FROM schedules WHERE id = ?",
                    (schedule_id,),
                ).fetchone()
                if row is None or str(row[0] or "") != run_id:
                    self._conn.execute("ROLLBACK")
                    return False
                cur = self._conn.execute(
                    """
                    UPDATE schedules SET claim_lease_until = ?
                    WHERE id = ? AND claim_run_id = ?
                    """,
                    (lease_until, schedule_id, run_id),
                )
                if cur.rowcount != 1:
                    self._conn.execute("ROLLBACK")
                    return False
                self._conn.execute("COMMIT")
                return True
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def complete_claim(
        self,
        schedule_id: str,
        run_id: str,
        *,
        status: str,
        halt_reason: str = "",
        cycles: int = 0,
        tokens_used: int = 0,
        swarms_used: int = 0,
        ended_at: Optional[float] = None,
        fire_at: Optional[float] = None,
        advance_last_fire: bool = True,
    ) -> bool:
        """Atomically finish a run, release the claim, and update last_*.

        Fenced: returns False with no mutation unless ``claim_run_id`` equals
        ``run_id``. The run row is updated only when still ``status='running'``
        (rowcount must be 1). A stale owner completing late cannot rewrite an
        interrupted row, clear a successor claim, or advance last_fire_at.
        """
        end_ts = time.time() if ended_at is None else float(ended_at)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
                ).fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return False
                sched = Schedule.from_row(dict(row))
                if sched.claim_run_id != run_id:
                    self._conn.execute("ROLLBACK")
                    return False
                fire_ts = float(
                    fire_at if fire_at is not None else (sched.claim_fire_at or 0.0)
                )
                cur = self._conn.execute(
                    """
                    UPDATE schedule_runs SET
                        ended_at = ?, status = ?, halt_reason = ?,
                        cycles = ?, tokens_used = ?, swarms_used = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (end_ts, status, halt_reason, int(cycles), int(tokens_used),
                     int(swarms_used), run_id),
                )
                if cur.rowcount != 1:
                    self._conn.execute("ROLLBACK")
                    return False
                if advance_last_fire:
                    cur = self._conn.execute(
                        """
                        UPDATE schedules SET
                            claim_owner = '',
                            claim_at = 0,
                            claim_lease_until = 0,
                            claim_fire_at = 0,
                            claim_run_id = '',
                            cancel_requested = 0,
                            last_status = ?,
                            last_run_at = ?,
                            last_fire_at = CASE
                                WHEN ? > last_fire_at THEN ? ELSE last_fire_at END
                        WHERE id = ? AND claim_run_id = ?
                        """,
                        (status, end_ts, fire_ts, fire_ts, schedule_id, run_id),
                    )
                else:
                    cur = self._conn.execute(
                        """
                        UPDATE schedules SET
                            claim_owner = '',
                            claim_at = 0,
                            claim_lease_until = 0,
                            claim_fire_at = 0,
                            claim_run_id = '',
                            cancel_requested = 0,
                            last_status = ?,
                            last_run_at = ?
                        WHERE id = ? AND claim_run_id = ?
                        """,
                        (status, end_ts, schedule_id, run_id),
                    )
                # Defense-in-depth claim fence: matching schedule row required.
                if cur.rowcount != 1:
                    self._conn.execute("ROLLBACK")
                    return False
                self._conn.execute("COMMIT")
                return True
            except Exception:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def request_cancel(self, schedule_id: str) -> bool:
        """Ask an active claim to cancel cooperatively."""
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE schedules SET cancel_requested = 1
                WHERE id = ? AND claim_owner != ''
                """,
                (schedule_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def cancel_requested(self, schedule_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT cancel_requested FROM schedules WHERE id = ?",
                (schedule_id,),
            ).fetchone()
        if row is None:
            return True  # removed => treat as cancel
        return bool(row[0])

    def list_runs(self, schedule_id: str, limit: int = 50) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM schedule_runs WHERE schedule_id = ? "
                "ORDER BY started_at DESC LIMIT ?",
                (schedule_id, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
