from __future__ import annotations

"""Busy-lifecycle mixin for ConversationalSession.

Extracted mechanically from harness/conversation.py to continue decomposing the
ConversationalSession god-object, matching ToolDispatchMixin / ReviewMemoryMixin
contract: these methods operate through `self` (``_busy``, ``_busy_since``,
``_busy_gen``, ``_busy_meta``, ``_interrupt_requested``, ``_stop_holds_idle``,
``_cancel``, …) provided by the concrete class -- the mixin defines no state
and no __init__.

``send`` / ``_send_locked_inner`` live on SendLoopMixin (full turn loop body).
Busy lock semantics, generation guards, and stop_holds_idle behavior are
unchanged -- only the method definitions move.

Method Resolution Order keeps behavior identical: ``is_turn_busy``,
``interrupt``, ``_mark_busy_acquired``, etc. still resolve via inheritance.
"""

import os
import sys


class BusyControlMixin:
    """Mixin holding busy-lock lifecycle and interrupt helpers.

    The concrete class (ConversationalSession) supplies the state these
    methods read/write via `self`. This mixin defines no __init__ and no
    instance state of its own.
    """

    def is_turn_busy(self) -> bool:
        """True while a pilot turn holds the single-writer busy lock.

        After an explicit Stop, report not-busy so the runners poll and UI Stop
        chrome settle even if the abandoned generator has not released ``_busy``
        yet (blocked in a subprocess / provider call).
        """
        if getattr(self, "_stop_holds_idle", False):
            return False
        try:
            if self._cancel.is_set() and self._interrupt_requested:
                return False
        except Exception:
            pass
        try:
            return bool(self._busy.locked())
        except Exception:
            return False

    def interrupt(self) -> None:
        """Hard Stop: cooperatively cancel the turn + jobs, report idle to the UI.

        Cooperative only — Python threads are never force-killed. A turn blocked
        in run_command or a local implement thread keeps ``_busy`` locked, so
        /api/session/state would still report runners=running and the UI would
        re-arm "thinking" after Stop. We:
        1. set the cancel flag,
        2. cancel every in-process local job,
        3. trip PM cancel flags + dual-store mark for session-dispatched jobs
           (harness and CLI durable stores — same seam as /api/swarm/cancel),
        4. hold an idle status surface until the next user send,
        5. mark interrupt_requested so a follow-up send can force-recover the lock,
        6. drop any queued steers with a durable/streamed notice (S2 boundary —
           never inject into the abandoned generator or a later unrelated send).
        """
        self.cancel()
        self._interrupt_requested = True
        self._stop_holds_idle = True
        # Only an actually abandoned generation (busy still held) should force
        # acquire-time steer drops. Idle-session Stop must not wipe later
        # ready-session steers on the next legitimate send.
        try:
            self._steer_boundary_drop_on_acquire = bool(self._busy.locked())
        except Exception:
            self._steer_boundary_drop_on_acquire = False
        # Surface idle immediately so the runners poll stops flipping the
        # composer back to thinking while the abandoned generator unwinds.
        try:
            self._state = "idle"
        except Exception:
            pass
        try:
            with self._local_jobs_lock:
                running_ids = [
                    jid for jid, job in self._local_jobs.items()
                    if (job or {}).get("status") == "running"
                ]
            for jid in running_ids:
                try:
                    self.cancel_local_job(jid)
                except Exception:
                    pass
        except Exception:
            pass
        # Best-effort: trip Puppetmaster cancel flags for session-dispatched jobs
        # so workers halt instead of finishing and kicking keep-alive resume.
        # Then drain both durable stores so an actionable job cannot strand
        # in harness-only or CLI-only membership.
        session_job_ids = [jid for jid in list(self._session_job_ids or []) if jid]
        try:
            from puppetmaster.cancellation import request_cancel
            for jid in session_job_ids:
                try:
                    request_cancel(jid)
                except Exception:
                    pass
                try:
                    self.cancel_local_job(jid)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self._drain_session_jobs_dual_store(session_job_ids)
        except Exception:
            pass
        # S2 Stop↔steer boundary: drop any queued steers so they cannot inject
        # into the abandoned generator or contaminate a later unrelated send.
        # Cooperative only — never force-kills threads.
        try:
            drop = getattr(self, "drop_queued_steers", None)
            dropped = drop() if callable(drop) else []
            if dropped:
                record = getattr(self, "_record_steer_drop_notice", None)
                if callable(record):
                    record(dropped)
        except Exception:
            pass
        # S3 Windows hygiene: reap the owned warm ACP child so a Stop cannot
        # leave orphan ``agent acp`` processes after a blocked prompt.
        try:
            release = getattr(self, "release_warm_acp", None)
            if callable(release):
                release(reason="interrupt")
        except Exception:
            pass

    def _drain_session_jobs_dual_store(self, job_ids: list | None = None) -> list:
        """Mark session-tracked jobs cancelled in harness + CLI stores.

        Cooperative dual-store drain shared conceptually with
        ``post_swarm_cancel``. Never raises to callers of interrupt; never
        force-kills threads.
        """
        ids = [
            jid for jid in list(
                job_ids if job_ids is not None
                else getattr(self, "_session_job_ids", []) or []
            ) if jid
        ]
        if not ids:
            return []
        harness_store = None
        try:
            durable = self.durable  # ConversationalSession @property
            harness_store = getattr(durable, "store", None)
        except Exception:
            harness_store = None
        repo = ""
        try:
            repo = getattr(getattr(self, "config", None), "repo", "") or ""
        except Exception:
            repo = ""
        from .job_cancel import drain_job_ids_dual_store
        return drain_job_ids_dual_store(
            ids, harness_store=harness_store, repo_root=repo,
        )

    def _mark_busy_acquired(self) -> int:
        """Record that the caller now holds _busy and return this turn's
        generation token. The token is what the finally passes to _release_busy
        so a reaped turn never releases a lock a later turn owns."""
        import time as _t
        with self._busy_meta:
            self._busy_gen += 1
            self._busy_since = _t.monotonic()
            # A new turn owns the lock now; clear any stale interrupt / Stop-hold
            # so they can't spuriously force-recover or suppress this healthy turn.
            was_stopped = bool(self._stop_holds_idle)
            drop_abandoned_steers = bool(
                getattr(self, "_steer_boundary_drop_on_acquire", False)
            )
            self._interrupt_requested = False
            self._stop_holds_idle = False
            self._interrupted_swarms = False
            self._steer_boundary_drop_on_acquire = False
            gen = self._busy_gen
        # After Stop of an abandoned generation: never carry raced late steers
        # into this new unrelated send. (interrupt already drops; this covers
        # races only.) Idle-session Stop leaves drop_abandoned_steers False so
        # ready-session enqueue/drain stays standard.
        # Record the same durable drop notice — never silently discard.
        if was_stopped and drop_abandoned_steers:
            try:
                drop = getattr(self, "drop_queued_steers", None)
                dropped = drop() if callable(drop) else []
                if dropped:
                    record = getattr(self, "_record_steer_drop_notice", None)
                    if callable(record):
                        record(dropped)
            except Exception:
                pass
        return gen

    def _release_busy(self, gen: int) -> None:
        """Release _busy only if this turn (identified by gen) still owns it. If a
        watchdog reaped the turn, the generation advanced and this is a no-op --
        preventing a double-release that would corrupt the single-writer lock.

        When an abandoned generation finally unwinds here, clear the
        generation-scoped steer-drop boundary so intentional post-unwind
        ``enqueue_steer`` survives the next turn. Acquire-time drop remains for
        true pre-unwind races (force-recover / reap paths that free the lock
        without this finally).
        """
        with self._busy_meta:
            if gen != self._busy_gen or not self._busy_since:
                return  # reaped (or already released) -- not ours to release
            self._busy_since = 0.0
            # Abandoned-generation Stop sets this while _busy is held; once the
            # abandoned owner releases, post-unwind steers are legitimate again.
            self._steer_boundary_drop_on_acquire = False
            try:
                self._busy.release()
            except RuntimeError:
                pass

    def _turn_deadline_seconds(self) -> float:
        """Hard wall-clock ceiling after which a still-held _busy is assumed
        wedged and reaped. Generous by default so a legitimately long turn is
        never clobbered; 0 disables reaping."""
        try:
            v = float(os.environ.get("HARNESS_TURN_DEADLINE_SECONDS", "").strip() or 600)
        except ValueError:
            v = 600.0
        return v if v > 0 else 0.0

    def _reap_stuck_turn(self) -> bool:
        """Force-recover a wedged turn: if _busy has been held past the hard turn
        deadline, a step-boundary budget check cannot help (the turn is stuck
        mid-call), so we advance the generation, force-release _busy, and reset
        state. Queued worker patches can then surface and new turns proceed. The
        generous deadline keeps this from ever reaping a healthy long turn (audit
        finding #6). Returns True if a reap happened."""
        deadline = self._turn_deadline_seconds()
        if not deadline:
            return False
        import time as _t
        with self._busy_meta:
            if not self._busy_since:
                return False
            held = _t.monotonic() - self._busy_since
            if held <= deadline:
                return False
            # Reap: bump the generation so the stale holder's _release_busy is a
            # no-op, then free the lock and reset visible state. Clear
            # ``_busy_since`` only after a successful release — clearing first
            # then failing RuntimeError left the lock held with since=0 so
            # later reaps could never fire again.
            self._busy_gen += 1
            try:
                self._busy.release()
            except RuntimeError:
                return False
            self._busy_since = 0.0
        try:
            self._state = "idle"
        except Exception:
            pass
        print(f"reaped wedged turn: _busy held {held:.0f}s past {deadline:.0f}s deadline", file=sys.stderr)
        return True
