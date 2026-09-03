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
dataclasses, hashlib) and MUST NOT import harness.* or puppetmaster.* -- that
invariant is what keeps tests/test_schedule_core.py hermetic.

Cron semantics implemented (standard 5-field crontab):
    minute hour day-of-month month day-of-week
Supported per field: '*', comma lists (0,30), ranges (9-17), step on wildcard
(*/15) and step on range (0-30/10). Day-of-week accepts 0 and 7 as Sunday.
When BOTH day-of-month and day-of-week are restricted (neither is '*'), a
minute matches if EITHER the DOM or the DOW matches -- the well-known Vixie
cron OR-rule -- because that is what real crontabs expect.

Timezone / DST:
    Evaluation is always host-local naive (``timezone_mode`` =
    ``"host_local"``): cron math uses naive ``datetime`` values from
    ``datetime.now()`` / ``datetime.fromtimestamp``. Per-schedule IANA
    zones (``zoneinfo.ZoneInfo``) are deferred — ``Schedule.timezone`` may
    exist as an unused store column for forward compatibility but is ignored
    for evaluation (always empty on write).     Host-local DST: spring-forward
    skips non-existent wall minutes (epoch round-trip differs); fall-back
    fires a repeated local minute at most once via minute-stable
    ``last_fire_at`` identity. Missed windows follow ``Schedule.missed_policy``:
    ``once`` (default) coalesces to the latest missed minute; ``skip`` drops
    strictly-past slots and only fires the current matching minute; ``all``
    walks every real missed minute (oldest first, capped at ``STAMPEDE_CAP``).
"""

import calendar
import hashlib
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple


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

# Missed-window policies. Default ``once`` preserves today's coalesce.
MISSED_POLICY_SKIP = "skip"
MISSED_POLICY_ONCE = "once"
MISSED_POLICY_ALL = "all"
STAMPEDE_CAP = 100

# Failure notice routing. Default ``route`` keeps today's Notifier behavior.
FAILURE_DELIVER_ROUTE = "route"
FAILURE_DELIVER_SUPPRESS = "suppress"

# Persisted notepad cap (small note, not a transcript).
NOTEPAD_MAX_CHARS = 4096
_DIGEST_HASH_LEN = 16
_DIGEST_SNIPPET_LEN = 80


def parse_missed_policy(value: object) -> str:
    """Normalize a missed-window policy; unknown or empty becomes once."""
    raw = str(value or "").strip().lower()
    if raw in (MISSED_POLICY_SKIP, MISSED_POLICY_ONCE, MISSED_POLICY_ALL):
        return raw
    return MISSED_POLICY_ONCE


def parse_failure_deliver(value: object) -> str:
    """Normalize failure notice routing; unknown or empty becomes route."""
    raw = str(value or "").strip().lower()
    if raw in (FAILURE_DELIVER_ROUTE, FAILURE_DELIVER_SUPPRESS):
        return raw
    return FAILURE_DELIVER_ROUTE


def clip_notepad(value: object, max_chars: int = NOTEPAD_MAX_CHARS) -> str:
    """Bound a persisted notepad to ``max_chars`` (default 4k)."""
    text = str(value or "")
    limit = int(max_chars)
    if limit <= 0:
        return ""
    if len(text) > limit:
        return text[:limit]
    return text


def fire_continuity_digest(result_text: str) -> str:
    """Short hash plus snippet of a successful fire result.

    Shape matches SessionLoop.last_response_digest (sha256 of the text) but
    stays short enough to persist on the schedule row and inject as context.
    """
    raw = (result_text or "").strip()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_DIGEST_HASH_LEN]
    snippet = " ".join(raw.split())
    if len(snippet) > _DIGEST_SNIPPET_LEN:
        snippet = snippet[:_DIGEST_SNIPPET_LEN]
    if snippet:
        return "%s %s" % (digest, snippet)
    return digest


def should_deliver_notice(schedule: "Schedule", run: Optional[dict] = None) -> bool:
    """False when failure_deliver=suppress and the run is not a success."""
    policy = parse_failure_deliver(getattr(schedule, "failure_deliver", None))
    if policy != FAILURE_DELIVER_SUPPRESS:
        return True
    payload = run or {}
    status = str(payload.get("status") or "").strip().lower()
    return status == "ok"


def should_prepend_continuity(schedule: "Schedule") -> bool:
    """True when the next fire should inject last digest and/or notepad."""
    digest = str(getattr(schedule, "continuity_digest", "") or "").strip()
    notepad = str(getattr(schedule, "notepad", "") or "").strip()
    monitor = bool(getattr(schedule, "monitor_mode", False))
    if not (monitor or digest):
        return False
    return bool(digest or notepad)


def continuity_block(schedule: "Schedule") -> str:
    """Context block prepended to the next fire's prompt, or empty."""
    if not should_prepend_continuity(schedule):
        return ""
    parts = ["[schedule continuity]"]
    digest = str(getattr(schedule, "continuity_digest", "") or "").strip()
    if digest:
        parts.append("last_digest: %s" % digest)
    note = clip_notepad(getattr(schedule, "notepad", "")).strip()
    if note:
        parts.append("notepad:\n%s" % note)
    return "\n".join(parts)


def schedule_fire_prompt(schedule: "Schedule") -> str:
    """Objective the session receives, with an optional continuity prefix."""
    objective = str(getattr(schedule, "objective", "") or "")
    block = continuity_block(schedule)
    if not block:
        return objective
    return "%s\n\n%s" % (block, objective)


@dataclass(frozen=True)
class MissedFireOutcome:
    """How a tick treated missed cron slots for one schedule."""

    policy: str
    slots_considered: int
    slots_fired: int
    skipped: bool


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


def validate_timezone(name: str) -> str:
    """Accept only empty timezone (host-local). Non-empty IANA is deferred.

    Returns an empty string. Raises ValueError when a non-empty name is
    supplied so writers (CLI/store/HTTP) cannot persist a per-schedule IANA
    zone.
    """
    cleaned = (name or "").strip()
    if cleaned:
        raise ValueError(
            "IANA timezone deferred; use host-local (empty timezone)"
        )
    return ""


def timezone_mode(schedule: "Schedule") -> str:
    """Always ``host_local``; per-schedule IANA zones are deferred."""
    return "host_local"


def _as_naive_wall(dt: datetime) -> datetime:
    """Minute-floored host-local naive wall time."""
    if dt.tzinfo is not None:
        return floor_minute(dt.astimezone().replace(tzinfo=None))
    return floor_minute(dt)


def _wall_from_epoch(ts: float) -> datetime:
    """Epoch seconds -> minute-floored host-local naive wall."""
    return floor_minute(datetime.fromtimestamp(ts))


def _host_wall_exists(dt: datetime) -> bool:
    """True when *dt*'s minute exists in the host local timezone.

    Spring-forward gaps: a naive wall that is not a real local minute
    round-trips through ``.timestamp()`` / ``fromtimestamp`` to a different
    minute (or raises). No IANA / ``ZoneInfo`` — host OS rules only.
    """
    floored = floor_minute(dt)
    try:
        return _wall_from_epoch(floored.timestamp()) == floored
    except (OSError, OverflowError, ValueError):
        return False


def fire_at_timestamp(dt: datetime) -> float:
    """Stable float identity for a cron fire minute."""
    return floor_minute(dt).timestamp()


def next_real_fire_after(cron: CronExpr, wall: datetime) -> datetime:
    """Next cron match after *wall* that exists as a host-local minute.

    Spring-forward: candidates in a DST gap are skipped until the next real
    local match. Per-schedule IANA zones remain deferred.
    """
    cur = _as_naive_wall(wall)
    for _ in range(_MAX_SEARCH_STEPS):
        candidate = floor_minute(cron.next_after(cur))
        if _host_wall_exists(candidate):
            return candidate
        cur = candidate
    raise ValueError(
        f"no real host-local cron match within "
        f"{_MAX_SEARCH_STEPS // (24 * 60)} days for {cron.raw!r}"
    )


def _coalesce_latest_counted(
    cron: CronExpr,
    first: datetime,
    now_min: datetime,
) -> Tuple[datetime, int]:
    """Walk from first missed fire to the latest real fire at or before now_min."""
    latest = first
    count = 1
    cur = first
    for _ in range(_MAX_SEARCH_STEPS):
        try:
            nxt = next_real_fire_after(cron, cur)
        except ValueError:
            break
        if nxt > now_min:
            break
        latest = nxt
        count += 1
        cur = nxt
    return latest, count


def _coalesce_latest_fire(
    cron: CronExpr,
    first: datetime,
    now_min: datetime,
) -> datetime:
    """Walk from first missed fire to the latest real fire at or before now_min."""
    latest, _count = _coalesce_latest_counted(cron, first, now_min)
    return latest


def _collect_missed_slots(
    cron: CronExpr,
    first: datetime,
    now_min: datetime,
    cap: int,
) -> List[datetime]:
    """Real fire minutes from first through now_min, oldest first, capped."""
    slots = [first]
    cur = first
    limit = max(1, int(cap))
    while len(slots) < limit:
        try:
            nxt = next_real_fire_after(cron, cur)
        except ValueError:
            break
        if nxt > now_min:
            break
        slots.append(nxt)
        cur = nxt
    return slots


def _first_missed_fire(
    schedule: "Schedule",
    cron: CronExpr,
    now_min: datetime,
) -> Optional[datetime]:
    """Earliest real missed fire minute at or before now_min, or None.

    ``last_fire_at`` is the cron-slot identity. Never-run anchors on
    ``enabled_at`` / ``created_at``. A future anchor is ignored except that
    the current matching minute remains due (clock-skew / test inject).
    """
    if schedule.last_fire_at and schedule.last_fire_at > 0:
        anchor = _wall_from_epoch(schedule.last_fire_at)
        try:
            first_missed = next_real_fire_after(cron, anchor)
        except ValueError:
            return None
        if first_missed > now_min:
            return None
        return first_missed

    current = None
    if cron.matches(now_min) and _host_wall_exists(now_min):
        current = now_min

    # Catch up a missed first window, anchored on enable/create time.
    # Ignore anchors in the future relative to ``now`` (clock skew / test inject).
    anchor_ts = schedule.enabled_at or schedule.created_at
    if anchor_ts and anchor_ts > 0:
        anchor = _wall_from_epoch(anchor_ts)
        if floor_minute(anchor) <= now_min:
            search_from = floor_minute(anchor) - timedelta(minutes=1)
            try:
                first = next_real_fire_after(cron, search_from)
            except ValueError:
                first = None
            if first is not None and first <= now_min:
                return first
    return current


def due_fire_plan(
    schedule: "Schedule", now: datetime,
) -> Tuple[List[datetime], MissedFireOutcome]:
    """Slots to dispatch this tick (oldest first) plus the missed-window outcome.

    Skip conceptually advances past strictly-past slots and only returns the
    current matching minute (or nothing). Once coalesces to the latest missed
    minute. All returns every remaining real slot, capped at ``STAMPEDE_CAP``.
    """
    policy = parse_missed_policy(getattr(schedule, "missed_policy", None))
    empty = MissedFireOutcome(
        policy=policy, slots_considered=0, slots_fired=0, skipped=False,
    )
    if not schedule.enabled:
        return [], empty
    try:
        cron = CronExpr.parse(schedule.cron)
    except ValueError:
        return [], empty

    now_min = _as_naive_wall(now)
    first = _first_missed_fire(schedule, cron, now_min)
    if first is None:
        return [], empty

    if policy == MISSED_POLICY_SKIP:
        now_is_slot = cron.matches(now_min) and _host_wall_exists(now_min)
        raw = _collect_missed_slots(cron, first, now_min, STAMPEDE_CAP)
        if now_is_slot:
            return [now_min], MissedFireOutcome(
                policy=policy,
                slots_considered=len(raw),
                slots_fired=1,
                skipped=first < now_min,
            )
        return [], MissedFireOutcome(
            policy=policy,
            slots_considered=len(raw),
            slots_fired=0,
            skipped=True,
        )

    if policy == MISSED_POLICY_ALL:
        slots = _collect_missed_slots(cron, first, now_min, STAMPEDE_CAP)
        return slots, MissedFireOutcome(
            policy=policy,
            slots_considered=len(slots),
            slots_fired=len(slots),
            skipped=False,
        )

    latest, considered = _coalesce_latest_counted(cron, first, now_min)
    return [latest], MissedFireOutcome(
        policy=MISSED_POLICY_ONCE,
        slots_considered=considered,
        slots_fired=1,
        skipped=False,
    )


def due_fire_slots(schedule: "Schedule", now: datetime) -> List[datetime]:
    """Due fire minutes for this tick, oldest first (empty if not due)."""
    slots, _outcome = due_fire_plan(schedule, now)
    return slots


def due_fire_at(schedule: "Schedule", now: datetime) -> Optional[datetime]:
    """Return one minute-stable fire identity to dispatch, or None if not due.

    Same-minute correctness: once ``last_fire_at`` records a fire minute, a
    later tick in that same minute is not due (``next_after`` moves forward).

    Policy (see ``due_fire_slots``):
      * ``once`` (default): coalesce to the latest missed minute <= now.
      * ``skip``: None when the only due slots are strictly in the past;
        the current matching minute still fires.
      * ``all``: the earliest remaining slot so a single-fire caller can
        walk catch-up one tick at a time.

    Never-run: anchor on ``enabled_at`` or ``created_at`` so a schedule that
    missed its first window still catches up (Once/All) or waits (Skip).

    Always host-local naive; ``schedule.timezone`` is ignored (IANA deferred).
    """
    slots = due_fire_slots(schedule, now)
    if not slots:
        return None
    return slots[0]


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
    # Prefer cancelled/canceled word forms; reject "failure to cancel" style
    # negatives that merely contain the substring "cancel".
    if (
        ("cancelled" in low or "canceled" in low or low.startswith("cancel "))
        and not any(
            neg in low
            for neg in (
                "failure",
                "failed to",
                "could not",
                "cannot",
                "can't",
                "unable",
            )
        )
    ):
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
    "timezone",
    # Opt-in busy-session inject (auto|steer|follow_up). Empty = legacy spawn.
    "delivery_mode",
    "missed_policy",
    # Cron continuity across fires (digest + notepad) and failure notices.
    "continuity_digest",
    "notepad",
    "monitor_mode",
    "failure_deliver",
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
    swarm_adapter: str = "agentic"
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
    # Unused store column (IANA deferred); always empty on write, ignored for eval.
    timezone: str = ""
    # Opt-in DeliveryMode for busy target sessions. Empty keeps spawn+run_auto.
    delivery_mode: str = ""
    # Missed-window policy: skip | once (default) | all. last_fire_at is the
    # cron-slot identity; last_run_at is wall-clock completion.
    missed_policy: str = MISSED_POLICY_ONCE
    # Last successful fire digest (short hash + snippet). Fresh session per
    # fire; this is injected context, not a long-lived ConversationalSession.
    continuity_digest: str = ""
    # Small persisted note (capped at NOTEPAD_MAX_CHARS).
    notepad: str = ""
    # When true, the next fire injects last digest + notepad as context.
    monitor_mode: bool = False
    # Failure notice routing: route (default) | suppress.
    failure_deliver: str = FAILURE_DELIVER_ROUTE
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
        d["timezone"] = (self.timezone or "").strip()
        d["missed_policy"] = parse_missed_policy(self.missed_policy)
        d["monitor_mode"] = 1 if self.monitor_mode else 0
        d["notepad"] = clip_notepad(self.notepad)
        d["continuity_digest"] = str(self.continuity_digest or "")
        d["failure_deliver"] = parse_failure_deliver(self.failure_deliver)
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
            swarm_adapter=str(row.get("swarm_adapter") or "agentic"),
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
            timezone=str(row.get("timezone") or ""),
            delivery_mode=str(row.get("delivery_mode") or ""),
            missed_policy=parse_missed_policy(row.get("missed_policy")),
            continuity_digest=str(row.get("continuity_digest") or ""),
            notepad=clip_notepad(row.get("notepad")),
            monitor_mode=bool(row.get("monitor_mode", 0)),
            failure_deliver=parse_failure_deliver(row.get("failure_deliver")),
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
