from __future__ import annotations

"""Scheduler: the daemon that drives due schedules through Fully-Auto mode.

WHY this shape: the scheduler must DRIVE the existing unattended entry point
(ConversationalSession.run_auto), never reimplement autonomy or ceilings. Each
due schedule becomes one bounded run_auto session governed by an AutoBudget
built from the schedule's ceilings (0 -> governor default). We consume the
event generator to the terminal 'auto_halt', extract the final reason and the
last governor snapshot (cycles/tokens/swarms), persist a run row, update the
schedule's last_run, and hand a concise summary to a pluggable Notifier.

Two design rules make this safe and testable:
  1. Isolation: every schedule's run is wrapped in try/except so one failing
     job never kills the tick or the daemon. Failures are recorded as
     status='error' with the exception text.
  2. Injection seams: session_factory and budget_factory exist ONLY so tests
     can substitute deterministic stubs. In production they default to the real
     ConversationalSession and AutoBudget -- no network or Puppetmaster is
     touched here beyond what run_auto itself does.

Claim lease / at-least-once delivery:
  Claims are fenced in ScheduleStore (try_claim / renew_claim / complete_claim).
  A background lease heartbeat renews the claim while run_auto is blocked on a
  provider or tool call, so a long stall does not hand the schedule to a
  successor mid-flight. If ownership is still lost (process crash, expired
  lease before heartbeat, forced recovery), the superseded worker detects
  renew/complete failure and must not rewrite durable state — but side effects
  already performed by that worker (tool calls, writes) are at-least-once.
  Successor fencing remains intact: a late complete_claim from a superseded
  owner is a no-op.

This subsystem is CLI-daemon-only: there is no HTTP/UI/SSE schedule surface.
"""

import os
import tempfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable, List, Optional

from .schedule_core import (
    CronExpr,
    Schedule,
    due_fire_at,
    fire_at_timestamp,
    status_from_halt_reason,
)
from .schedule_store import ScheduleStore, claim_lease_seconds

# Heartbeat wakes often enough to renew before lease expiry, but is capped so
# a multi-hour ceiling does not create a once-per-hour renew cadence that
# leaves a long blocked tool call unprotected near the end of the lease.
_HEARTBEAT_INTERVAL_MIN = 1.0
_HEARTBEAT_INTERVAL_CAP = 60.0


def _heartbeat_interval(lease_seconds: int) -> float:
    """Bounded renew cadence: lease/3, clamped to [1s, 60s]."""
    third = max(1, int(lease_seconds)) / 3.0
    return max(_HEARTBEAT_INTERVAL_MIN, min(third, _HEARTBEAT_INTERVAL_CAP))


class _ClaimLeaseHeartbeat:
    """Independent renew loop so blocked provider/tool calls still extend lease.

    Cooperative: when renew_claim returns False the loop stops and
    ``ownership_lost`` becomes True. stop() joins the worker briefly.
    """

    def __init__(
        self,
        store: ScheduleStore,
        schedule_id: str,
        run_id: str,
        lease_seconds: int,
        interval: Optional[float] = None,
    ) -> None:
        self._store = store
        self._schedule_id = schedule_id
        self._run_id = run_id
        self._lease_seconds = max(1, int(lease_seconds))
        self._interval = (
            float(interval) if interval is not None
            else _heartbeat_interval(self._lease_seconds)
        )
        self._stop = threading.Event()
        self._ownership_lost = False
        self._thread: Optional[threading.Thread] = None

    @property
    def ownership_lost(self) -> bool:
        return self._ownership_lost

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop,
            name=f"schedule-lease-{self._schedule_id}",
            daemon=True,
        )
        self._thread.start()

    def _loop(self) -> None:
        # Wait first so a fast run does not pay an immediate renew round-trip.
        while not self._stop.wait(self._interval):
            try:
                ok = self._store.renew_claim(
                    self._schedule_id, self._run_id, self._lease_seconds,
                )
            except Exception:
                # Transient store errors: retry on the next tick rather than
                # declaring ownership lost (complete_claim is the hard fence).
                continue
            if not ok:
                self._ownership_lost = True
                return

    def stop(self) -> bool:
        """Stop the loop; return True when a renew observed ownership loss."""
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=min(2.0, self._interval + 0.5))
        return self._ownership_lost


class Notifier(ABC):
    """Delivery seam for run summaries. Pluggable so a messaging gateway can be
    injected later without editing the daemon."""

    @abstractmethod
    def notify(self, schedule: Schedule, run: dict) -> None:  # pragma: no cover
        raise NotImplementedError


class LogNotifier(Notifier):
    """Default notifier: prints a concise plain-text summary (no emoji)."""

    def notify(self, schedule: Schedule, run: dict) -> None:
        print(
            "schedule {id} ({name}): {status}"
            " reason={reason} cycles={cycles}"
            " tokens={tokens} swarms={swarms}".format(
                id=schedule.id,
                name=schedule.name,
                status=run.get("status", ""),
                reason=run.get("halt_reason", ""),
                cycles=run.get("cycles", 0),
                tokens=run.get("tokens_used", 0),
                swarms=run.get("swarms_used", 0),
            )
        )


def _default_budget_factory(schedule: Schedule):
    """Build an AutoBudget from the schedule ceilings, filling 0s with the
    governor's from_env defaults so a partially-specified schedule is still
    fully bounded."""
    from .autobudget import AutoBudget

    base = AutoBudget.from_env()
    return AutoBudget(
        max_tokens=schedule.max_tokens or base.max_tokens,
        max_seconds=schedule.max_seconds or base.max_seconds,
        max_swarms=schedule.max_swarms or base.max_swarms,
        max_idle_steps=base.max_idle_steps,
        killswitch_path=base.killswitch_path,
    )


def _default_session_factory(schedule: Schedule):
    """Build a real ConversationalSession configured for this schedule."""
    from .config import HarnessConfig
    from .conversation import ConversationalSession

    cfg = HarnessConfig.from_env()
    if schedule.repo:
        cfg.repo = schedule.repo
    if schedule.swarm_adapter:
        cfg.swarm_adapter = schedule.swarm_adapter
    if schedule.driver:
        cfg.driver = schedule.driver
    return ConversationalSession(cfg)


def _claim_owner() -> str:
    return f"pid-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def resolve_schedule_repo(schedule: Schedule) -> str:
    """Effective workspace for an unattended schedule dispatch."""
    if (schedule.repo or "").strip():
        return schedule.repo.strip()
    try:
        from .config import HarnessConfig
        return (HarnessConfig.from_env().repo or "").strip()
    except Exception:
        return ""


def check_schedule_workspace(repo: str) -> Optional[str]:
    """Refuse unattended dispatch for home / temp / missing / non-git roots.

    Reuses Marionette implement/ephemeral guards. Nested git workspaces are
    accepted via the same ``_git_work_tree`` check as run_implement.
    Returns a REFUSED reason string, or None when the workspace is safe.
    """
    repo = (repo or "").strip()
    if not repo:
        return "REFUSED: scheduled run requires a git workspace (set --repo)"

    try:
        abs_repo = os.path.abspath(repo)
    except Exception:
        return f"REFUSED: invalid workspace path {repo!r}"

    if not os.path.isdir(abs_repo):
        return (
            f"REFUSED: workspace {abs_repo} is not an existing directory"
        )

    try:
        home = os.path.abspath(os.path.expanduser("~"))
        if os.path.normcase(abs_repo) == os.path.normcase(home):
            return f"REFUSED: refusing unattended schedule in user home ({abs_repo})"
    except Exception:
        pass

    # Ephemeral/temp: same skip-under-pytest convention as sessions._is_ephemeral_root.
    try:
        from .sessions import _is_ephemeral_root
        if _is_ephemeral_root(abs_repo):
            return (
                f"REFUSED: refusing unattended schedule in temp/ephemeral "
                f"path ({abs_repo})"
            )
    except Exception:
        try:
            from .paths import path_within
            if "PYTEST_CURRENT_TEST" not in os.environ and path_within(
                abs_repo, tempfile.gettempdir(), allow_equal=True
            ):
                return (
                    f"REFUSED: refusing unattended schedule in temp/ephemeral "
                    f"path ({abs_repo})"
                )
        except Exception:
            pass

    try:
        from .implement_guards import check_implement_workspace, _git_work_tree
        msg = check_implement_workspace(abs_repo)
        if msg:
            return msg if msg.upper().startswith("REFUSED") else f"REFUSED: {msg}"
        # Defense in depth: implement guard can be env-disabled; still require git.
        if not _git_work_tree(abs_repo):
            return f"REFUSED: {abs_repo} is not a git repository"
    except Exception:
        try:
            from .implement_guards import _git_work_tree
            if not _git_work_tree(abs_repo):
                return f"REFUSED: {abs_repo} is not a git repository"
        except Exception:
            return f"REFUSED: {abs_repo} is not a git repository"

    return None


def _is_due(schedule: Schedule, now: datetime) -> bool:
    """True when due_fire_at returns a fire minute (enabled + valid cron)."""
    return due_fire_at(schedule, now) is not None


def _mark_invalid_cron(store: ScheduleStore, schedule: Schedule) -> None:
    try:
        CronExpr.parse(schedule.cron)
    except ValueError:
        if schedule.last_status != "invalid_cron":
            store.update_status(schedule.id, "invalid_cron")


def run_due(
    store: ScheduleStore,
    now: Optional[datetime] = None,
    *,
    notifier: Optional[Notifier] = None,
    session_factory: Optional[Callable[[Schedule], object]] = None,
    budget_factory: Optional[Callable[[Schedule], object]] = None,
    owner: Optional[str] = None,
    active_schedule_holder: Optional[dict] = None,
) -> List[dict]:
    """Run every due-and-enabled schedule ONCE. Returns the list of run dicts.

    Each due schedule is claimed before run_auto. Failures are isolated: one
    schedule raising never aborts the others. Same-minute double ticks share a
    fire identity so at most one claim wins.
    """
    now = now or datetime.now()
    notifier = notifier or LogNotifier()
    session_factory = session_factory or _default_session_factory
    budget_factory = budget_factory or _default_budget_factory
    owner = owner or _claim_owner()

    results: List[dict] = []
    for schedule in store.list(enabled_only=True):
        _mark_invalid_cron(store, schedule)
        fire = due_fire_at(schedule, now)
        if fire is None:
            continue
        results.append(
            _run_one(
                schedule,
                store,
                notifier,
                session_factory,
                budget_factory,
                fire_at=fire_at_timestamp(fire),
                owner=owner,
                active_schedule_holder=active_schedule_holder,
            )
        )
    return results


def run_one_now(
    store: ScheduleStore,
    schedule_id: str,
    *,
    notifier: Optional[Notifier] = None,
    session_factory: Optional[Callable[[Schedule], object]] = None,
    budget_factory: Optional[Callable[[Schedule], object]] = None,
    owner: Optional[str] = None,
    active_schedule_holder: Optional[dict] = None,
) -> Optional[dict]:
    """Run a single named schedule immediately, bypassing the due check (used by
    the `run-now` CLI). Returns the run dict, or None if the id is unknown."""
    schedule = store.get(schedule_id)
    if schedule is None:
        return None
    notifier = notifier or LogNotifier()
    session_factory = session_factory or _default_session_factory
    budget_factory = budget_factory or _default_budget_factory
    # Manual run-now uses a unique fire token; force-claim fences vs daemon
    # without advancing cron last_fire_at.
    fire_at = time.time()
    return _run_one(
        schedule,
        store,
        notifier,
        session_factory,
        budget_factory,
        fire_at=fire_at,
        owner=owner or _claim_owner(),
        active_schedule_holder=active_schedule_holder,
        force_claim=True,
    )


def _run_one(
    schedule: Schedule,
    store: ScheduleStore,
    notifier: Notifier,
    session_factory: Callable[[Schedule], object],
    budget_factory: Callable[[Schedule], object],
    *,
    fire_at: float,
    owner: str,
    active_schedule_holder: Optional[dict] = None,
    force_claim: bool = False,
) -> dict:
    lease_seconds = claim_lease_seconds(schedule.max_seconds)
    claim = store.try_claim(
        schedule.id,
        fire_at,
        owner,
        lease_seconds=lease_seconds,
        force=force_claim,
    )
    if claim is None:
        # Overlap with another daemon / run-now, or fire already completed.
        blocked = {
            "schedule_id": schedule.id,
            "started_at": time.time(),
            "ended_at": time.time(),
            "status": "blocked",
            "halt_reason": "claim held by another owner or fire already completed",
            "cycles": 0,
            "tokens_used": 0,
            "swarms_used": 0,
            "fire_at": fire_at,
            "run_id": "",
        }
        return blocked

    run_id = claim["run_id"]
    started_at = claim["started_at"]
    if active_schedule_holder is not None:
        active_schedule_holder["schedule_id"] = schedule.id
        active_schedule_holder["run_id"] = run_id

    heartbeat: Optional[_ClaimLeaseHeartbeat] = None
    try:
        # Workspace safety before any session work.
        repo = resolve_schedule_repo(schedule)
        refusal = check_schedule_workspace(repo)
        if refusal:
            run = {
                "schedule_id": schedule.id,
                "started_at": started_at,
                "ended_at": time.time(),
                "status": "refused",
                "halt_reason": refusal,
                "cycles": 0,
                "tokens_used": 0,
                "swarms_used": 0,
                "fire_at": fire_at,
                "run_id": run_id,
            }
            if not store.complete_claim(
                schedule.id, run_id,
                status="refused", halt_reason=refusal, fire_at=fire_at,
                ended_at=run["ended_at"],
                advance_last_fire=not force_claim,
            ):
                run["status"] = "superseded"
                run["halt_reason"] = "ownership_lost"
                return run
            try:
                notifier.notify(schedule, run)
            except Exception:
                pass
            return run

        status = "ok"
        halt_reason = ""
        cycles = 0
        tokens_used = 0
        swarms_used = 0
        cancel_invoked = False
        ownership_lost = False

        # Renew while blocked inside next()/provider/tool — not only between
        # streamed events. Interval is bounded; disable only in tests via
        # heartbeat_interval=0 on the private helper (see tests).
        heartbeat = _ClaimLeaseHeartbeat(
            store, schedule.id, run_id, lease_seconds,
        )
        heartbeat.start()

        try:
            session = session_factory(schedule)
            budget = budget_factory(schedule)
            last_snapshot: dict = {}
            # Explicit iterator so cancel is observed before advancing for the
            # first/next event. Heartbeat covers the blocked-next() gap.
            events = iter(session.run_auto(schedule.objective, budget))
            while True:
                if heartbeat.ownership_lost:
                    ownership_lost = True
                    if not cancel_invoked:
                        cancel_invoked = True
                        cancel = getattr(session, "cancel", None)
                        if callable(cancel):
                            try:
                                cancel()
                            except Exception:
                                pass
                    break
                if store.cancel_requested(schedule.id) and not cancel_invoked:
                    cancel_invoked = True
                    cancel = getattr(session, "cancel", None)
                    if callable(cancel):
                        try:
                            cancel()
                        except Exception:
                            pass
                try:
                    ev = next(events)
                except StopIteration:
                    break

                data = getattr(ev, "data", None) or {}
                if getattr(ev, "kind", "") == "auto_status":
                    snap = data.get("snapshot") or {}
                    if snap:
                        last_snapshot = snap
                    if "cycle" in data:
                        cycles = int(data["cycle"])
                elif getattr(ev, "kind", "") == "auto_halt":
                    halt_reason = str(data.get("reason", ""))
                    snap = data.get("snapshot") or {}
                    if snap:
                        last_snapshot = snap

                # Cooperative lease renew between events (defense in depth
                # alongside the heartbeat thread).
                if not store.renew_claim(schedule.id, run_id, lease_seconds):
                    ownership_lost = True
                    if not cancel_invoked:
                        cancel_invoked = True
                        cancel = getattr(session, "cancel", None)
                        if callable(cancel):
                            try:
                                cancel()
                            except Exception:
                                pass
                    break

            tokens_used = int(last_snapshot.get("tokens_used", 0) or 0)
            swarms_used = int(last_snapshot.get("swarms_used", 0) or 0)
            if heartbeat.ownership_lost:
                ownership_lost = True
            if ownership_lost:
                status = "superseded"
                halt_reason = "ownership_lost"
            elif cancel_invoked and (
                not halt_reason or "cancel" not in halt_reason.lower()
            ):
                halt_reason = halt_reason or "cancelled"
                status = "cancelled"
            else:
                status = status_from_halt_reason(halt_reason)
        except KeyboardInterrupt:
            # Preserve the durable running claim for stale recovery; clear only
            # the in-memory holder in finally. Do not complete_claim.
            raise
        except Exception as exc:  # isolation: never let one job kill the loop
            status = "error"
            halt_reason = f"{type(exc).__name__}: {exc}"

        ended_at = time.time()
        run = {
            "schedule_id": schedule.id,
            "started_at": started_at,
            "ended_at": ended_at,
            "status": status,
            "halt_reason": halt_reason,
            "cycles": cycles,
            "tokens_used": tokens_used,
            "swarms_used": swarms_used,
            "fire_at": fire_at,
            "run_id": run_id,
        }
        if not store.complete_claim(
            schedule.id,
            run_id,
            status=status,
            halt_reason=halt_reason,
            cycles=cycles,
            tokens_used=tokens_used,
            swarms_used=swarms_used,
            ended_at=ended_at,
            fire_at=fire_at,
            advance_last_fire=not force_claim,
        ):
            run["status"] = "superseded"
            run["halt_reason"] = "ownership_lost"
            return run
        # Ownership-loss outcomes must never look like a successful notify.
        if run["status"] == "superseded":
            return run
        try:
            notifier.notify(schedule, run)
        except Exception:  # a broken notifier must not break the run record
            pass
        return run
    finally:
        if heartbeat is not None:
            try:
                heartbeat.stop()
            except Exception:
                pass
        # Always drop the daemon's in-memory active holder; never touch the
        # durable claim here (KeyboardInterrupt must leave it visible).
        if active_schedule_holder is not None:
            active_schedule_holder.pop("schedule_id", None)
            active_schedule_holder.pop("run_id", None)


class SchedulerDaemon:
    """Long-running loop: on each tick, run all due schedules, then sleep.

    Resilient by construction: an exception in a single tick is logged and the
    loop continues (a transient store or session error must not take the daemon
    down overnight). Ctrl-C exits cleanly. stop() cooperatively cancels the
    active schedule and uses interruptible waiting rather than a hard sleep.
    """

    def __init__(
        self,
        store: ScheduleStore,
        *,
        notifier: Optional[Notifier] = None,
        session_factory: Optional[Callable[[Schedule], object]] = None,
        budget_factory: Optional[Callable[[Schedule], object]] = None,
    ) -> None:
        self.store = store
        self.notifier = notifier or LogNotifier()
        self.session_factory = session_factory
        self.budget_factory = budget_factory
        self._stop = False
        self._active: dict = {}

    def stop(self) -> None:
        self._stop = True
        sid = self._active.get("schedule_id")
        if sid:
            try:
                self.store.request_cancel(sid)
            except Exception:
                pass

    def tick(self, now: Optional[datetime] = None) -> List[dict]:
        return run_due(
            self.store,
            now,
            notifier=self.notifier,
            session_factory=self.session_factory,
            budget_factory=self.budget_factory,
            active_schedule_holder=self._active,
        )

    def _interruptible_wait(self, tick_seconds: int) -> None:
        deadline = time.time() + max(1, int(tick_seconds))
        while not self._stop and time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(0.5, remaining))

    def serve(self, tick_seconds: int = 30) -> None:
        print(f"scheduler daemon started (tick={tick_seconds}s); Ctrl-C to stop")
        try:
            while not self._stop:
                try:
                    runs = self.tick()
                    # Suppress blocked/noise-only ticks in the summary.
                    real = [r for r in runs if r.get("status") != "blocked"]
                    if real:
                        print(f"tick: ran {len(real)} schedule(s)")
                except Exception as exc:  # keep the daemon alive across ticks
                    print(f"tick error (continuing): {type(exc).__name__}: {exc}")
                self._interruptible_wait(tick_seconds)
        except KeyboardInterrupt:
            self.stop()
            print("scheduler daemon stopped")
