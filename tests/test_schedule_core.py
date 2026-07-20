"""Cron engine + Schedule record proofs -- pure, hermetic, no harness imports.

These lock down the fiddly cron edge cases (steps, ranges, DOM/DOW OR-rule,
Sunday-as-0-or-7, rollovers) so the daemon can trust the time math.
"""
import time
from datetime import datetime

import pytest

from harness.schedule_core import (
    CronExpr,
    Schedule,
    due_fire_at,
    fire_at_timestamp,
    floor_minute,
    next_real_fire_after,
    status_from_halt_reason,
    timezone_mode,
    validate_timezone,
)


def _dt(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi)


def test_all_wildcards_matches_everything():
    c = CronExpr.parse("* * * * *")
    assert c.matches(_dt(2024, 1, 1, 0, 0))
    assert c.matches(_dt(2024, 6, 15, 13, 37))


def test_exact_minute_hour():
    c = CronExpr.parse("30 9 * * *")
    assert c.matches(_dt(2024, 3, 4, 9, 30))
    assert not c.matches(_dt(2024, 3, 4, 9, 31))
    assert not c.matches(_dt(2024, 3, 4, 10, 30))


def test_comma_list():
    c = CronExpr.parse("0,30 * * * *")
    assert c.matches(_dt(2024, 1, 1, 5, 0))
    assert c.matches(_dt(2024, 1, 1, 5, 30))
    assert not c.matches(_dt(2024, 1, 1, 5, 15))


def test_range():
    c = CronExpr.parse("0 9-17 * * *")
    assert c.matches(_dt(2024, 1, 1, 9, 0))
    assert c.matches(_dt(2024, 1, 1, 17, 0))
    assert not c.matches(_dt(2024, 1, 1, 8, 0))
    assert not c.matches(_dt(2024, 1, 1, 18, 0))


def test_step_on_wildcard():
    c = CronExpr.parse("*/15 * * * *")
    for m in (0, 15, 30, 45):
        assert c.matches(_dt(2024, 1, 1, 0, m))
    assert not c.matches(_dt(2024, 1, 1, 0, 10))


def test_step_on_range():
    c = CronExpr.parse("0-30/10 * * * *")
    for m in (0, 10, 20, 30):
        assert c.matches(_dt(2024, 1, 1, 0, m))
    assert not c.matches(_dt(2024, 1, 1, 0, 40))


def test_sunday_zero_and_seven_both_match():
    # 2024-01-07 is a Sunday.
    sun = _dt(2024, 1, 7, 12, 0)
    c0 = CronExpr.parse("0 12 * * 0")
    c7 = CronExpr.parse("0 12 * * 7")
    assert c0.matches(sun)
    assert c7.matches(sun)
    # Monday 2024-01-08 must not match a Sunday schedule.
    assert not c0.matches(_dt(2024, 1, 8, 12, 0))


def test_dom_dow_or_semantics():
    # Both DOM and DOW restricted -> match if EITHER. day 15 OR Monday(dow 1).
    c = CronExpr.parse("0 0 15 * 1")
    # 2024-01-15 is a Monday: both true.
    assert c.matches(_dt(2024, 1, 15, 0, 0))
    # 2024-01-08 is a Monday, not the 15th: still matches (DOW).
    assert c.matches(_dt(2024, 1, 8, 0, 0))
    # 2024-02-15 is a Thursday, not Monday: still matches (DOM).
    assert c.matches(_dt(2024, 2, 15, 0, 0))
    # 2024-01-09 is a Tuesday, not the 15th: no match.
    assert not c.matches(_dt(2024, 1, 9, 0, 0))


def test_dom_only_restricted():
    c = CronExpr.parse("0 0 1 * *")
    assert c.matches(_dt(2024, 5, 1, 0, 0))
    assert not c.matches(_dt(2024, 5, 2, 0, 0))


@pytest.mark.parametrize("bad", [
    "",
    "* * * *",            # 4 fields
    "* * * * * *",        # 6 fields
    "60 * * * *",         # minute out of range
    "* 24 * * *",         # hour out of range
    "* * 0 * *",          # dom below range
    "* * * 13 *",         # month out of range
    "* * * * 8",          # dow out of range
    "5-1 * * * *",        # inverted range
    "*/0 * * * *",        # zero step
    "*/x * * * *",        # bad step
    "a * * * *",          # bad value
    "1,,2 * * * *",       # empty term
])
def test_malformed_raises(bad):
    with pytest.raises(ValueError):
        CronExpr.parse(bad)


def test_next_after_hour_rollover():
    c = CronExpr.parse("0 * * * *")
    assert c.next_after(_dt(2024, 1, 1, 9, 30)) == _dt(2024, 1, 1, 10, 0)


def test_next_after_day_rollover():
    c = CronExpr.parse("30 2 * * *")
    assert c.next_after(_dt(2024, 1, 1, 3, 0)) == _dt(2024, 1, 2, 2, 30)


def test_next_after_month_rollover():
    c = CronExpr.parse("0 0 1 * *")
    assert c.next_after(_dt(2024, 1, 15, 12, 0)) == _dt(2024, 2, 1, 0, 0)


def test_next_after_year_rollover():
    c = CronExpr.parse("0 0 1 1 *")
    assert c.next_after(_dt(2024, 6, 1, 0, 0)) == _dt(2025, 1, 1, 0, 0)


def test_next_after_leap_day():
    # Feb 29 exists in 2024 (leap); next after 2023 lands on 2024-02-29.
    c = CronExpr.parse("0 0 29 2 *")
    assert c.next_after(_dt(2023, 3, 1, 0, 0)) == _dt(2024, 2, 29, 0, 0)


def test_next_after_rare_cron_jumps_within_tight_bound():
    """Annual/leap schedules must not minute-scan the hot path (~4y)."""
    c = CronExpr.parse("0 0 29 2 *")
    # Just after 2024 leap day -> next real fire is 2028-02-29.
    t0 = time.perf_counter()
    got = c.next_after(_dt(2024, 3, 1, 0, 0))
    elapsed = time.perf_counter() - t0
    assert got == _dt(2028, 2, 29, 0, 0)
    # Jumping months/days should finish well under a tight wall-time cap
    # (minute-walking ~2M steps would be orders of magnitude slower).
    assert elapsed < 0.05


def test_next_after_rare_dom_month_from_midyear():
    c = CronExpr.parse("30 4 1 1 *")  # Jan 1 04:30 annually
    assert c.next_after(_dt(2024, 6, 15, 12, 0)) == _dt(2025, 1, 1, 4, 30)


def test_next_after_strictly_after():
    c = CronExpr.parse("* * * * *")
    # Exactly on a matching minute -> the NEXT minute.
    assert c.next_after(_dt(2024, 1, 1, 0, 0)) == _dt(2024, 1, 1, 0, 1)


def test_schedule_row_roundtrip():
    s = Schedule(
        id="abc123", name="nightly", objective="audit repo",
        cron="0 2 * * *", repo="/tmp/x", swarm_adapter="openai",
        driver="qwen", enabled=False, max_tokens=5000, max_seconds=600,
        max_swarms=3, created_at=1234.5, enabled_at=1234.5,
        last_run_at=99.0, last_fire_at=99.0, last_status="ok")
    row = s.to_row()
    assert row["enabled"] == 0  # bool flattened to int for sqlite
    back = Schedule.from_row(row)
    assert back == s


def test_schedule_from_row_defaults():
    row = {"id": "x", "name": "n", "objective": "o", "cron": "* * * * *"}
    s = Schedule.from_row(row)
    assert s.enabled is True
    assert s.swarm_adapter == "demo"
    assert s.max_tokens == 0
    assert s.last_fire_at == 0.0


def test_due_fire_same_minute_once():
    s = Schedule(id="a", name="n", objective="o", cron="* * * * *")
    now = _dt(2024, 1, 1, 12, 0)
    fire = due_fire_at(s, now)
    assert fire == now
    s.last_fire_at = fire_at_timestamp(fire)
    # Second tick in the same minute must not be due.
    assert due_fire_at(s, now.replace(second=30)) is None


def test_due_fire_catchup_first_missed_window():
    # Created before a hourly fire; now is past that fire; never run.
    created = _dt(2024, 1, 1, 9, 15).timestamp()
    s = Schedule(
        id="a", name="n", objective="o", cron="0 * * * *",
        created_at=created, enabled_at=created,
    )
    now = _dt(2024, 1, 1, 10, 30)
    fire = due_fire_at(s, now)
    assert fire == _dt(2024, 1, 1, 10, 0)


def test_due_fire_multi_gap_coalesces_to_one():
    created = _dt(2024, 1, 1, 0, 0).timestamp()
    s = Schedule(
        id="a", name="n", objective="o", cron="0 * * * *",
        created_at=created, enabled_at=created,
        last_fire_at=_dt(2024, 1, 1, 1, 0).timestamp(),
    )
    # Missed 02:00, 03:00, 04:00 — one coalesced fire at latest.
    now = _dt(2024, 1, 1, 4, 15)
    fire = due_fire_at(s, now)
    assert fire == _dt(2024, 1, 1, 4, 0)


@pytest.mark.parametrize("reason,status", [
    ("pilot reports objective met (no further investigation)", "ok"),
    ("objective met and verified (verify_cmd passed)", "ok"),
    ("cancelled", "cancelled"),
    ("killswitch tripped (/tmp/stop)", "killswitch"),
    ("REFUSED: no .codegraph index", "refused"),
    ("token ceiling reached (100/100)", "token_ceiling"),
    ("time ceiling reached (60s/60s)", "time_ceiling"),
    ("swarm ceiling reached (5/5)", "swarm_ceiling"),
    ("idle stall (3 idle steps)", "idle_ceiling"),
    ("turn ceiling reached", "turn_ceiling"),
    ("budget exhausted", "budget"),
    ("something went wrong", "failed"),
])
def test_status_from_halt_reason(reason, status):
    assert status_from_halt_reason(reason) == status


@pytest.mark.parametrize("reason", [
    "objective met",
    " falsely claimed objective met and verified",
    "objective NOT verified after 2 retries (verify_cmd still failing)",
    "failed: objective met was a hallucination",
    "did not find that the objective met",
])
def test_status_from_halt_reason_rejects_objective_met_substring(reason):
    # Bare / negative phrases that merely contain "objective met" are never ok.
    assert status_from_halt_reason(reason) != "ok"
    assert status_from_halt_reason(reason) == "failed"


def test_display_status_invalid_cron():
    s = Schedule(id="a", name="n", objective="o", cron="not a cron")
    assert s.display_status() == "invalid_cron"


def test_validate_timezone_empty_only_iana_deferred():
    assert validate_timezone("") == ""
    assert validate_timezone("   ") == ""
    with pytest.raises(ValueError, match="IANA"):
        validate_timezone("America/New_York")
    with pytest.raises(ValueError, match="IANA"):
        validate_timezone("UTC")


def test_timezone_mode_always_host_local():
    s = Schedule(id="a", name="n", objective="o", cron="* * * * *")
    assert timezone_mode(s) == "host_local"
    # Stale non-empty column values are ignored for mode (IANA deferred).
    s.timezone = "UTC"
    assert timezone_mode(s) == "host_local"


def test_fire_at_timestamp_round_trip():
    """Host-local fire identity survives epoch round-trip (naive wall)."""
    wall = _dt(2024, 6, 1, 9, 30)
    ts = fire_at_timestamp(wall)
    assert floor_minute(datetime.fromtimestamp(ts)) == wall
    assert fire_at_timestamp(wall.replace(second=45)) == ts


def test_host_local_round_trip_and_due_fire():
    """Empty timezone keeps naive host-local due/fire identity semantics."""
    s = Schedule(id="a", name="n", objective="o", cron="30 9 * * *", timezone="")
    now = _dt(2024, 6, 1, 9, 30)
    fire = due_fire_at(s, now)
    assert fire == now
    assert fire.tzinfo is None
    s.last_fire_at = fire_at_timestamp(fire)
    assert due_fire_at(s, now.replace(second=45)) is None


def _host_spring_gap_skips_0230() -> bool:
    """True when host local TZ treats 2024-03-10 02:30 as a DST gap."""
    wall = _dt(2024, 3, 10, 2, 30)
    try:
        return floor_minute(datetime.fromtimestamp(wall.timestamp())) != wall
    except (OSError, OverflowError, ValueError):
        return True


def test_host_local_spring_forward_skips_nonexistent_minute():
    """Spring-forward: cron ``30 2 * * *`` skips a non-existent 02:30 wall.

    Uses host OS epoch round-trip (no ZoneInfo / IANA). On TZ without a US
    spring gap (e.g. UTC), 02:30 remains a real minute and is matched.
    """
    cron = CronExpr.parse("30 2 * * *")
    before = _dt(2024, 3, 10, 1, 59)
    nxt = next_real_fire_after(cron, before)
    if _host_spring_gap_skips_0230():
        assert nxt == _dt(2024, 3, 11, 2, 30)
        s = Schedule(
            id="a", name="n", objective="o", cron="30 2 * * *", timezone="",
            last_fire_at=fire_at_timestamp(_dt(2024, 3, 9, 2, 30)),
        )
        # Gap minute is not due; next real fire is the following day.
        assert due_fire_at(s, _dt(2024, 3, 10, 2, 30)) is None
        assert due_fire_at(s, _dt(2024, 3, 11, 2, 30)) == _dt(2024, 3, 11, 2, 30)
        s.last_fire_at = fire_at_timestamp(_dt(2024, 3, 11, 2, 30))
        assert due_fire_at(s, _dt(2024, 3, 11, 2, 45)) is None
    else:
        assert nxt == _dt(2024, 3, 10, 2, 30)
        s = Schedule(
            id="a", name="n", objective="o", cron="30 2 * * *", timezone="",
            last_fire_at=fire_at_timestamp(_dt(2024, 3, 9, 2, 30)),
        )
        assert due_fire_at(s, _dt(2024, 3, 10, 2, 30)) == _dt(2024, 3, 10, 2, 30)
        s.last_fire_at = fire_at_timestamp(_dt(2024, 3, 10, 2, 30))
        assert due_fire_at(s, _dt(2024, 3, 10, 2, 45)) is None


def test_host_local_fall_back_fires_once_minute_identity():
    """Fall-back: repeated local minute fires at most once (epoch identity)."""
    s = Schedule(id="a", name="n", objective="o", cron="30 1 * * *", timezone="")
    wall = _dt(2024, 11, 3, 1, 30)
    fire = due_fire_at(s, wall)
    assert fire == wall
    s.last_fire_at = fire_at_timestamp(fire)
    # Same wall minute again (second fall-back occurrence / later tick).
    assert due_fire_at(s, wall.replace(second=59)) is None
    assert next_real_fire_after(
        CronExpr.parse(s.cron), wall,
    ) == _dt(2024, 11, 4, 1, 30)


def test_schedule_timezone_column_roundtrip_empty():
    """Timezone column persists empty; mode stays host_local (IANA deferred)."""
    s = Schedule(
        id="abc", name="z", objective="o", cron="0 0 * * *",
        timezone="",
    )
    back = Schedule.from_row(s.to_row())
    assert back.timezone == ""
    assert timezone_mode(back) == "host_local"
