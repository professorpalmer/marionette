from __future__ import annotations

"""Schedule core: the PURE, PM-free cron engine and Schedule record.

WHY this layer exists: the scheduler subsystem has two very different concerns.
One is time math (does a cron expression fire at this minute? when is the next
fire?) and the shape of a persisted schedule. That concern is deterministic,
has no side effects, and must be trivially unit-testable without touching
Puppetmaster, sqlite, or the network. The other concern -- actually driving a
run_auto session, persisting rows, notifying a gateway -- is coupled to the
harness. We keep those apart so the fiddly, edge-case-heavy cron math can be
proven hermetically and fast.

This module therefore imports ONLY the standard library (datetime, calendar,
dataclasses) and MUST NOT import harness.* or puppetmaster.* -- that invariant
is what keeps tests/test_schedule_core.py hermetic.

Cron semantics implemented (standard 5-field crontab):
    minute hour day-of-month month day-of-week
Supported per field: '*', comma lists (0,30), ranges (9-17), step on wildcard
(*/15) and step on range (0-30/10). Day-of-week accepts 0 and 7 as Sunday.
When BOTH day-of-month and day-of-week are restricted (neither is '*'), a
minute matches if EITHER the DOM or the DOW matches -- the well-known Vixie
cron OR-rule -- because that is what real crontabs expect.

Timezone / DST (host-local, no IANA field):
    All cron evaluation uses naive ``datetime`` values in the host's local
    timezone (``datetime.now()`` / ``datetime.fromtimestamp``). There is no
    per-schedule IANA zone. Across DST spring-forward, a fire minute that
    does not exist is skipped (next_after lands on the next real local
    minute that matches). Across fall-back, a repeated local minute fires
    at most once per wall-clock tick because due_fire_at / last_fire_at use
    a minute-stable identity. Missed windows coalesce to a single catch-up
    fire (latest missed minute <= now), never one run per gap.
"""

import calendar
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional


# Field bounds as (low, high) inclusive, in cron field order.
_FIELD_BOUNDS = [
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day of month
    (1, 12),   # month
    (0, 7),    # day of week (0 and 7 both Sunday)
]
_FIELD_NAMES = ["minute", "hour", "day-of-month", "month", "day-of-week"]

# Cap next_after search so a pathological expression cannot loop forever.
# With field jumping this bounds *candidate advances*, not wall-clock minutes.
# 4 years of day-steps still covers a Feb-29-only schedule safely.
_MAX_SEARCH_STEPS = 4 * 366 * 24 * 60


def _parse_field(spec: str, low: int, high: int, name: str) -> frozenset:
    """Expand one cron field into the concrete set of ints it matches.

    Raises ValueError with a clear message on any malformed token.
    """
    spec = spec.strip()
    if not spec:
        raise ValueError(f"empty {name} field")
    values: set = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty term in {name} field: {spec!r}")
        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError:
                raise ValueError(f"bad step {step_s!r} in {name} field")
            if step <= 0:
                raise ValueError(f"step must be positive in {name} field: {part!r}")
        else:
            base = part

        if base == "*":
            start, end = low, high
        elif "-" in base:
            lo_s, _, hi_s = base.partition("-")
            try:
                start, end = int(lo_s), int(hi_s)
            except ValueError:
                raise ValueError(f"bad range {base!r} in {name} field")
            if start > end:
                raise ValueError(f"inverted range {base!r} in {name} field")
        else:
            try:
                start = end = int(base)
            except ValueError:
                raise ValueError(f"bad value {base!r} in {name} field")

        if start < low or end > high:
            raise ValueError(
                f"{name} value out of range {low}-{high}: {base!r}")
        values.update(range(start, end + 1, step))

    if not values:
        raise ValueError(f"no values matched in {name} field: {spec!r}")
    return frozenset(values)


@dataclass(frozen=True)
class CronExpr:
    """A parsed, evaluatable 5-field cron expression.

    Fields are stored as concrete integer sets so matching is a cheap membership
    test. Day-of-week Sunday is normalized so both 0 and 7 are present.
    """

    minutes: frozenset
    hours: frozenset
    doms: frozenset
    months: frozenset
    dows: frozenset
    dom_restricted: bool
    dow_restricted: bool
    raw: str = ""

    @classmethod
    def parse(cls, expr: str) -> "CronExpr":
        if expr is None or not str(expr).strip():
            raise ValueError("empty cron expression")
        fields = str(expr).split()
        if len(fields) != 5:
            raise ValueError(
                f"cron expression must have 5 fields, got {len(fields)}: {expr!r}")
        sets = [
            _parse_field(fields[i], *_FIELD_BOUNDS[i], _FIELD_NAMES[i])
            for i in range(5)
        ]
        dows = set(sets[4])
        if 7 in dows:
            dows.add(0)
        if 0 in dows:
            dows.add(7)
        return cls(
            minutes=sets[0],
            hours=sets[1],
            doms=sets[2],
            months=sets[3],
            dows=frozenset(dows),
            dom_restricted=(fields[2].strip() != "*"),
            dow_restricted=(fields[4].strip() != "*"),
            raw=str(expr).strip(),
        )

    def _day_matches(self, dt: datetime) -> bool:
        # Python weekday(): Monday=0..Sunday=6. Cron dow: Sunday=0.
        cron_dow = (dt.weekday() + 1) % 7
        dom_ok = dt.day in self.doms
        dow_ok = cron_dow in self.dows
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        if self.dom_restricted:
            return dom_ok
        if self.dow_restricted:
            return dow_ok
        return True  # both wildcard

    def matches(self, dt: datetime) -> bool:
        """True if the given datetime (at minute resolution) fires this cron."""
        return (
            dt.minute in self.minutes
            and dt.hour in self.hours
            and dt.month in self.months
            and self._day_matches(dt)
        )

    def _next_allowed(self, sorted_vals: List[int], current: int) -> Optional[int]:
        """Smallest value in sorted_vals strictly greater than current, else None."""
        for v in sorted_vals:
            if v > current:
                return v
        return None

    def _jump_month(self, cur: datetime, months: List[int]) -> datetime:
        """Advance to 00:00 on day 1 of the next allowed month (may cross years)."""
        nxt_m = self._next_allowed(months, cur.month)
        if nxt_m is not None:
            return datetime(cur.year, nxt_m, 1, 0, 0)
        return datetime(cur.year + 1, months[0], 1, 0, 0)

    def next_after(self, dt: datetime) -> datetime:
        """Next fire time strictly after dt, at minute resolution.

        Jumps across disallowed months/days/hours/minutes so rare expressions
        (e.g. Feb 29 annually) stay cheap on the daemon hot path. Search is
        capped at ~4 years of candidate advances; raise ValueError if nothing
        matches (which should only happen for an impossible date like Feb 30).
        """
        # Round up to the next whole minute strictly after dt.
        cur = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        months = sorted(self.months)
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)
        if not months or not hours or not minutes:
            raise ValueError(f"empty cron field set for {self.raw!r}")

        for _ in range(_MAX_SEARCH_STEPS):
            if cur.month not in self.months:
                cur = self._jump_month(cur, months)
                continue

            # Invalid calendar day for this month (e.g. Apr 31) — skip day.
            last_dom = calendar.monthrange(cur.year, cur.month)[1]
            if cur.day > last_dom:
                cur = (cur.replace(day=1, hour=0, minute=0)
                       + timedelta(days=last_dom))
                continue

            if not self._day_matches(cur):
                cur = (cur + timedelta(days=1)).replace(hour=0, minute=0)
                continue

            if cur.hour not in self.hours:
                nxt_h = self._next_allowed(hours, cur.hour)
                if nxt_h is None:
                    # Next day at first allowed hour/minute.
                    cur = (cur + timedelta(days=1)).replace(
                        hour=hours[0], minute=minutes[0],
                    )
                else:
                    cur = cur.replace(hour=nxt_h, minute=minutes[0])
                continue

            if cur.minute not in self.minutes:
                nxt_mi = self._next_allowed(minutes, cur.minute)
                if nxt_mi is None:
                    # Roll to next allowed hour (or next day).
                    nxt_h = self._next_allowed(hours, cur.hour)
                    if nxt_h is None:
                        cur = (cur + timedelta(days=1)).replace(
                            hour=hours[0], minute=minutes[0],
                        )
                    else:
                        cur = cur.replace(hour=nxt_h, minute=minutes[0])
                else:
                    cur = cur.replace(minute=nxt_mi)
                continue

            # Month/day/hour/minute all allowed — matches() is definitive.
            if self.matches(cur):
                return cur
            # Defensive: day OR-rule edge; advance one minute.
            cur += timedelta(minutes=1)

        raise ValueError(
            f"no cron match within {_MAX_SEARCH_STEPS // (24 * 60)} days "
            f"for {self.raw!r}")


def floor_minute(dt: datetime) -> datetime:
    """Truncate to minute resolution (seconds/microseconds cleared)."""
    return dt.replace(second=0, microsecond=0)


def fire_at_timestamp(dt: datetime) -> float:
    """Stable float identity for a cron fire minute."""
    return floor_minute(dt).timestamp()


def _coalesce_latest_fire(cron: CronExpr, first: datetime, now_min: datetime) -> datetime:
    """Walk from first missed fire to the latest fire at or before now_min."""
    latest = first
    cur = first
    # Cap iterations to avoid pathological loops; now_min - first is enough.
    for _ in range(_MAX_SEARCH_STEPS):
        try:
            nxt = cron.next_after(cur)
        except ValueError:
            break
        if nxt > now_min:
            break
        latest = nxt
        cur = nxt
    return latest


def due_fire_at(schedule: "Schedule", now: datetime) -> Optional[datetime]:
    """Return the minute-stable fire identity to dispatch, or None if not due.

    Same-minute correctness: once ``last_fire_at`` records a fire minute, a
    later tick in that same minute is not due (``next_after`` moves forward).

    Catch-up: when one or more fire windows were missed, return a single
    coalesced fire (the latest missed minute <= now), never one run per gap.

    Never-run: anchor on ``enabled_at`` or ``created_at`` so a schedule that
    missed its first window still catches up once.
    """
    if not schedule.enabled:
        return None
    try:
        cron = CronExpr.parse(schedule.cron)
    except ValueError:
        return None

    now_min = floor_minute(now)

    if schedule.last_fire_at and schedule.last_fire_at > 0:
        anchor = datetime.fromtimestamp(schedule.last_fire_at)
        try:
            first_missed = cron.next_after(anchor)
        except ValueError:
            return None
        if first_missed > now_min:
            return None
        return _coalesce_latest_fire(cron, first_missed, now_min)

    # Never-run: the current matching minute is always due.
    if cron.matches(now_min):
        return now_min

    # Catch up a missed first window once, anchored on enable/create time.
    # Ignore anchors in the future relative to ``now`` (clock skew / test inject).
    anchor_ts = schedule.enabled_at or schedule.created_at
    if anchor_ts and anchor_ts > 0:
        anchor = datetime.fromtimestamp(anchor_ts)
        if floor_minute(anchor) > now_min:
            return None
        search_from = floor_minute(anchor) - timedelta(minutes=1)
        try:
            first = cron.next_after(search_from)
        except ValueError:
            return None
        if first > now_min:
            return None
        return _coalesce_latest_fire(cron, first, now_min)

    return None


# Production successful auto_halt reasons (exact prefix, case-insensitive).
# Substring matching is intentionally rejected so negative phrases that merely
# contain "objective met" cannot be recorded as ok.
_OK_HALT_PREFIXES = (
    "objective met and verified",
    "pilot reports objective met",
)


def status_from_halt_reason(reason: str) -> str:
    """Map an auto_halt reason to a truthful terminal schedule status.

    ``ok`` is reserved for genuine successful objective completion via an
    exact/prefix allowlist of production halt reasons. Ceilings, cancellation,
    killswitch, refusal, and failures stay non-ok.
    """
    raw = (reason or "").strip()
    low = raw.lower()
    if not low:
        return "failed"
    if any(low.startswith(prefix) for prefix in _OK_HALT_PREFIXES):
        return "ok"
    if "cancel" in low:
        return "cancelled"
    if "killswitch" in low:
        return "killswitch"
    if "refused" in low:
        return "refused"
    if "token ceiling" in low or ("token" in low and "ceiling" in low):
        return "token_ceiling"
    if "time ceiling" in low or ("time ceiling" in low) or (
        "seconds" in low and "ceiling" in low
    ):
        return "time_ceiling"
    if "swarm ceiling" in low or ("swarm" in low and "ceiling" in low):
        return "swarm_ceiling"
    if "idle" in low or "stall" in low:
        return "idle_ceiling"
    if "turn" in low and "ceiling" in low:
        return "turn_ceiling"
    if "budget" in low:
        return "budget"
    if "error" in low or "exception" in low:
        return "error"
    return "failed"


# Ordered field names for row round-tripping and store schema (persistent cols).
SCHEDULE_FIELDS = [
    "id", "name", "objective", "cron", "repo", "swarm_adapter", "driver",
    "enabled", "max_tokens", "max_seconds", "max_swarms",
    "created_at", "enabled_at", "last_run_at", "last_fire_at", "last_status",
]


@dataclass
class Schedule:
    """A durable scheduled objective. Zero for a ceiling means 'use the governor
    default' (resolved at run time, not stored as a magic number)."""

    id: str
    name: str
    objective: str
    cron: str
    repo: str = ""
    swarm_adapter: str = "demo"
    driver: str = ""
    enabled: bool = True
    max_tokens: int = 0
    max_seconds: int = 0
    max_swarms: int = 0
    created_at: float = 0.0
    enabled_at: float = 0.0
    last_run_at: float = 0.0
    last_fire_at: float = 0.0
    last_status: str = ""
    # Claim / fencing fields (managed by ScheduleStore; shown by list).
    claim_owner: str = ""
    claim_at: float = 0.0
    claim_lease_until: float = 0.0
    claim_fire_at: float = 0.0
    claim_run_id: str = ""
    cancel_requested: bool = False

    def to_row(self) -> Dict[str, object]:
        """Flatten to a sqlite-friendly dict (bool -> int)."""
        d = asdict(self)
        d["enabled"] = 1 if self.enabled else 0
        d["cancel_requested"] = 1 if self.cancel_requested else 0
        return d

    @classmethod
    def from_row(cls, row: Dict[str, object]) -> "Schedule":
        """Rebuild from a sqlite row (int -> bool), ignoring extra columns."""
        return cls(
            id=str(row["id"]),
            name=str(row["name"]),
            objective=str(row["objective"]),
            cron=str(row["cron"]),
            repo=str(row.get("repo") or ""),
            swarm_adapter=str(row.get("swarm_adapter") or "demo"),
            driver=str(row.get("driver") or ""),
            enabled=bool(row.get("enabled", 1)),
            max_tokens=int(row.get("max_tokens") or 0),
            max_seconds=int(row.get("max_seconds") or 0),
            max_swarms=int(row.get("max_swarms") or 0),
            created_at=float(row.get("created_at") or 0.0),
            enabled_at=float(row.get("enabled_at") or 0.0),
            last_run_at=float(row.get("last_run_at") or 0.0),
            last_fire_at=float(row.get("last_fire_at") or 0.0),
            last_status=str(row.get("last_status") or ""),
            claim_owner=str(row.get("claim_owner") or ""),
            claim_at=float(row.get("claim_at") or 0.0),
            claim_lease_until=float(row.get("claim_lease_until") or 0.0),
            claim_fire_at=float(row.get("claim_fire_at") or 0.0),
            claim_run_id=str(row.get("claim_run_id") or ""),
            cancel_requested=bool(row.get("cancel_requested", 0)),
        )

    def display_status(self, now: Optional[float] = None) -> str:
        """Truthful list status: running / stale / invalid_cron / last_status."""
        import time as _time
        now_ts = _time.time() if now is None else float(now)
        try:
            CronExpr.parse(self.cron)
        except ValueError:
            return "invalid_cron"
        if self.claim_owner:
            if self.claim_lease_until and self.claim_lease_until > now_ts:
                return "running"
            return "stale"
        return self.last_status or "never"
