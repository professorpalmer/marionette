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
        """Hard Stop: cancel the turn, kill local workers, and report idle to the UI.

        Cooperative cancel alone is not enough -- a turn blocked in run_command or
        a local implement thread keeps ``_busy`` locked, so /api/session/state still
        reports runners=running and the UI re-arms "thinking" after Stop. We:
        1. set the cancel flag,
        2. cancel every in-process local job,
        3. hold an idle status surface until the next user send,
        4. mark interrupt_requested so a follow-up send can force-recover the lock.
        """
        self.cancel()
        self._interrupt_requested = True
        self._stop_holds_idle = True
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
        try:
            from puppetmaster.cancellation import request_cancel
            for jid in list(self._session_job_ids or []):
                if not jid:
                    continue
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
            self._interrupt_requested = False
            self._stop_holds_idle = False
            self._interrupted_swarms = False
            return self._busy_gen

    def _release_busy(self, gen: int) -> None:
        """Release _busy only if this turn (identified by gen) still owns it. If a
        watchdog reaped the turn, the generation advanced and this is a no-op --
        preventing a double-release that would corrupt the single-writer lock."""
        with self._busy_meta:
            if gen != self._busy_gen or not self._busy_since:
                return  # reaped (or already released) -- not ours to release
            self._busy_since = 0.0
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
            # no-op, then free the lock and reset visible state.
            self._busy_gen += 1
            self._busy_since = 0.0
            try:
                self._busy.release()
            except RuntimeError:
                return False
        try:
            self._state = "idle"
        except Exception:
            pass
        print(f"reaped wedged turn: _busy held {held:.0f}s past {deadline:.0f}s deadline", file=sys.stderr)
        return True
