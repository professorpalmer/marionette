from __future__ import annotations

"""schedule CLI: manage and run scheduled unattended objectives.

  harness schedule add --name nightly --cron "0 2 * * *" --objective "..."
  harness schedule list
  harness schedule edit <id> --cron "0 3 * * *"
  harness schedule remove <id>
  harness schedule enable <id>
  harness schedule disable <id>
  harness schedule run-now <id>
  harness schedule history <id>
  harness schedule daemon [--tick 30]

This is the harness-coupled front end for the pure schedule_core plus the
ScheduleStore and scheduler daemon. It mirrors the manual-dispatch + ANSI _c
style of cli.py. Cron expressions are validated at `add`/`edit` time (parsed
and the next three fire times printed); an invalid expression exits 1.

Cron times are host-local naive datetimes. Per-schedule IANA zones are
deferred. Next-fire previews are labeled ``host-local``. A repeated local
minute fires at most once (minute-stable ``last_fire_at``); missed windows
coalesce to a single catch-up run. Delivery is at-least-once: a superseded
worker that already performed tool side effects cannot rewrite durable
claim/run state, but those side effects are not rolled back.

HTTP/UI control exists for list/mutate/run-now/history (poll-first). Unattended
cron fire still requires this CLI's local daemon process. SSE for schedules
is deferred.
"""

import argparse
import sys
from datetime import datetime
from typing import List, Optional

from .schedule_core import (
    CronExpr,
    Schedule,
    next_real_fire_after,
    timezone_mode,
)
from .schedule_store import (
    REMOVE_CANCEL_REQUESTED,
    REMOVE_REMOVED,
    REMOVE_STALE_RECOVERED,
    ScheduleStore,
)


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s


def _next_fires(cron: CronExpr, count: int = 3) -> List[datetime]:
    out: List[datetime] = []
    cur = datetime.now()
    for _ in range(count):
        cur = next_real_fire_after(cron, cur)
        out.append(cur)
    return out


def _print_next_fires(cron: CronExpr) -> None:
    print("next fires (host-local):")
    for dt in _next_fires(cron):
        print(f"  {dt.isoformat(sep=' ', timespec='minutes')} host-local")


def _reject_negative_ceilings(max_tokens, max_seconds, max_swarms) -> Optional[str]:
    """Return an error message when any ceiling is negative, else None."""
    for label, value in (
        ("max-tokens", max_tokens),
        ("max-seconds", max_seconds),
        ("max-swarms", max_swarms),
    ):
        if value is not None and int(value) < 0:
            return f"{label} must be non-negative, got {value}"
    return None


def _cmd_add(args) -> int:
    ceiling_err = _reject_negative_ceilings(
        args.max_tokens, args.max_seconds, args.max_swarms,
    )
    if ceiling_err:
        print(_c("31", ceiling_err))
        return 1
    store = ScheduleStore(args.db)
    try:
        cron = CronExpr.parse(args.cron)
    except ValueError as exc:
        print(_c("31", f"invalid schedule: {exc}"))
        return 1
    sched = Schedule(
        id="",
        name=args.name,
        objective=args.objective,
        cron=args.cron,
        repo=args.repo or "",
        swarm_adapter=args.swarm_adapter,
        driver=args.driver or "",
        max_tokens=args.max_tokens,
        max_seconds=args.max_seconds,
        max_swarms=args.max_swarms,
        timezone="",
    )
    try:
        store.add(sched)
    except ValueError as exc:
        print(_c("31", f"invalid schedule: {exc}"))
        return 1
    print(_c("32", f"added schedule {sched.id} ({sched.name})"))
    _print_next_fires(cron)
    return 0


def _cmd_list(args) -> int:
    store = ScheduleStore(args.db)
    scheds = store.list()
    if not scheds:
        print("no schedules")
        return 0
    for s in scheds:
        state = "enabled" if s.enabled else "disabled"
        status = s.display_status()
        mode = timezone_mode(s)
        print(_c("36", f"{s.id}") + f"  {s.name}  [{state}]")
        print(
            f"    cron={s.cron!r} tz=host-local ({mode}) "
            f"adapter={s.swarm_adapter} status={status}"
        )
        print(f"    objective: {s.objective}")
        if s.repo:
            print(f"    repo: {s.repo}")
    return 0


def _cmd_edit(args) -> int:
    ceiling_err = _reject_negative_ceilings(
        args.max_tokens, args.max_seconds, args.max_swarms,
    )
    if ceiling_err:
        print(_c("31", ceiling_err))
        return 1
    store = ScheduleStore(args.db)
    if store.get(args.id) is None:
        print(_c("31", f"no such schedule: {args.id}"))
        return 1
    fields = {}
    if args.name is not None:
        fields["name"] = args.name
    if args.objective is not None:
        fields["objective"] = args.objective
    if args.cron is not None:
        fields["cron"] = args.cron
    if args.repo is not None:
        fields["repo"] = args.repo
    if args.driver is not None:
        fields["driver"] = args.driver
    if args.swarm_adapter is not None:
        fields["swarm_adapter"] = args.swarm_adapter
    if args.max_tokens is not None:
        fields["max_tokens"] = args.max_tokens
    if args.max_seconds is not None:
        fields["max_seconds"] = args.max_seconds
    if args.max_swarms is not None:
        fields["max_swarms"] = args.max_swarms
    if not fields:
        print(_c("31", "nothing to update; pass at least one field"))
        return 1
    try:
        updated = store.update_fields(args.id, **fields)
    except ValueError as exc:
        print(_c("31", f"invalid update: {exc}"))
        return 1
    if updated is None:
        print(_c("31", f"no such schedule: {args.id}"))
        return 1
    print(_c("32", f"updated {updated.id} ({updated.name})"))
    if args.cron is not None:
        cron = CronExpr.parse(updated.cron)
        _print_next_fires(cron)
    return 0


def _cmd_remove(args) -> int:
    store = ScheduleStore(args.db)
    outcome = store.remove(args.id)
    if outcome == REMOVE_REMOVED:
        print(_c("32", f"removed {args.id}"))
        return 0
    if outcome == REMOVE_STALE_RECOVERED:
        print(
            _c(
                "33",
                f"stale claim recovered for {args.id} "
                "(history preserved; remove again to purge)",
            )
        )
        return 0
    if outcome == REMOVE_CANCEL_REQUESTED:
        print(
            _c(
                "33",
                f"cancel requested for {args.id} "
                "(schedule disabled; run history preserved until idle, then remove again)",
            )
        )
        return 0
    print(_c("31", f"no such schedule: {args.id}"))
    return 1


def _cmd_enable(args) -> int:
    store = ScheduleStore(args.db)
    if store.set_enabled(args.id, True):
        print(_c("32", f"enabled {args.id}"))
        return 0
    print(_c("31", f"no such schedule: {args.id}"))
    return 1


def _cmd_disable(args) -> int:
    store = ScheduleStore(args.db)
    if store.set_enabled(args.id, False):
        print(_c("32", f"disabled {args.id}"))
        return 0
    print(_c("31", f"no such schedule: {args.id}"))
    return 1


def _cmd_run_now(args) -> int:
    from .scheduler import run_one_now

    store = ScheduleStore(args.db)
    run = run_one_now(store, args.id)
    if run is None:
        print(_c("31", f"no such schedule: {args.id}"))
        return 1
    status = run.get("status") or ""
    if status != "ok":
        print(_c("31", f"run {status}: {run.get('halt_reason')}"))
        return 1
    print(_c("32", f"run complete: {run.get('halt_reason')}"))
    return 0


def _cmd_history(args) -> int:
    store = ScheduleStore(args.db)
    if store.get(args.id) is None:
        print(_c("31", f"no such schedule: {args.id}"))
        return 1
    runs = store.list_runs(args.id, limit=args.limit)
    if not runs:
        print("no runs")
        return 0
    for r in runs:
        print(
            f"{r.get('id')}  status={r.get('status')}  "
            f"reason={r.get('halt_reason')!r}  "
            f"cycles={r.get('cycles')} tokens={r.get('tokens_used')} "
            f"swarms={r.get('swarms_used')}"
        )
    return 0


def _cmd_daemon(args) -> int:
    from .scheduler import SchedulerDaemon

    store = ScheduleStore(args.db)
    SchedulerDaemon(store).serve(tick_seconds=args.tick)
    return 0


def _run_schedule(argv) -> int:
    ap = argparse.ArgumentParser(
        prog="harness schedule",
        description=(
            "Manage scheduled unattended objectives. Cron uses host-local "
            "time (per-schedule IANA timezone is deferred). HTTP/UI can list "
            "and mutate schedules; the local daemon is still required for "
            "unattended cron fire. Missed fires coalesce; delivery is "
            "at-least-once under lease recovery. SSE is deferred."
        ),
    )
    ap.add_argument("--db", default=None, help="schedule store path (tests/override)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="add a schedule")
    p_add.add_argument("--name", required=True)
    p_add.add_argument(
        "--cron",
        required=True,
        help=(
            "5-field cron (minute hour dom month dow) in host-local time; "
            "DST/catch-up use minute-stable fire identity — see module docstring"
        ),
    )
    p_add.add_argument("--objective", required=True)
    p_add.add_argument("--repo", default=None)
    p_add.add_argument("--driver", default=None)
    p_add.add_argument("--swarm-adapter", dest="swarm_adapter", default="demo")
    p_add.add_argument("--max-tokens", dest="max_tokens", type=int, default=0)
    p_add.add_argument("--max-seconds", dest="max_seconds", type=int, default=0)
    p_add.add_argument("--max-swarms", dest="max_swarms", type=int, default=0)
    p_add.set_defaults(func=_cmd_add)

    p_list = sub.add_parser("list", help="list schedules")
    p_list.set_defaults(func=_cmd_list)

    p_edit = sub.add_parser("edit", help="update schedule fields", aliases=["update"])
    p_edit.add_argument("id")
    p_edit.add_argument("--name", default=None)
    p_edit.add_argument("--objective", default=None)
    p_edit.add_argument("--cron", default=None)
    p_edit.add_argument("--repo", default=None)
    p_edit.add_argument("--driver", default=None)
    p_edit.add_argument("--swarm-adapter", dest="swarm_adapter", default=None)
    p_edit.add_argument("--max-tokens", dest="max_tokens", type=int, default=None)
    p_edit.add_argument("--max-seconds", dest="max_seconds", type=int, default=None)
    p_edit.add_argument("--max-swarms", dest="max_swarms", type=int, default=None)
    p_edit.set_defaults(func=_cmd_edit)

    p_rm = sub.add_parser("remove", help="remove a schedule")
    p_rm.add_argument("id")
    p_rm.set_defaults(func=_cmd_remove)

    p_en = sub.add_parser("enable", help="enable a schedule")
    p_en.add_argument("id")
    p_en.set_defaults(func=_cmd_enable)

    p_dis = sub.add_parser("disable", help="disable a schedule")
    p_dis.add_argument("id")
    p_dis.set_defaults(func=_cmd_disable)

    p_run = sub.add_parser("run-now", help="run one schedule immediately")
    p_run.add_argument("id")
    p_run.set_defaults(func=_cmd_run_now)

    p_hist = sub.add_parser("history", help="show run history", aliases=["run-history"])
    p_hist.add_argument("id")
    p_hist.add_argument("--limit", type=int, default=50)
    p_hist.set_defaults(func=_cmd_history)

    p_dae = sub.add_parser("daemon", help="run the scheduler loop")
    p_dae.add_argument("--tick", type=int, default=30, help="seconds between ticks")
    p_dae.set_defaults(func=_cmd_daemon)

    args = ap.parse_args(argv)
    return args.func(args)
