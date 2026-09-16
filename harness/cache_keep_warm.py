"""Per-runner idle cache lease. No transcript writes or tool dispatch."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from pmharness.drivers.cache_refresh import CacheRefreshDriver, RefreshAttempt, RefreshCancelled


@dataclass(frozen=True)
class KeepWarmLimits:
    max_refreshes: int = 3
    idle_seconds: int = 3600
    max_spend_usd: float = 5.0

    def __post_init__(self):
        if type(self.max_refreshes) is not int or not 1 <= self.max_refreshes <= 10:
            raise ValueError("max_refreshes must be an integer from 1 to 10")
        if type(self.idle_seconds) is not int or not 60 <= self.idle_seconds <= 7200:
            raise ValueError("idle_seconds must be an integer from 60 to 7200")
        if type(self.max_spend_usd) not in (int, float) or not 0 < self.max_spend_usd <= 10:
            raise ValueError("max_spend_usd must be greater than 0 and at most 10")


class CacheKeepWarm:
    def __init__(self, runner, *, clock=time.monotonic, schedule=True):
        self.runner = runner
        self.clock = clock
        self.schedule = schedule
        self.lock = threading.Lock()
        self.enabled = False
        self.closed = False
        self.state = "off"
        self.reason = "Keep warm is off."
        self.limits = KeepWarmLimits()
        self.count = 0
        self.reserved_usd = 0.0
        self.last_activity = clock()
        self.last_usage = None
        self.last_http_status = None
        self.attempt = None
        self._timer = None
        self._foreground = 0

    def status(self):
        with self.lock:
            snapshot = self._snapshot()
            return dict(state=self.state, enabled=self.enabled, reason=self.reason,
                        refreshes=self.count, max_refreshes=self.limits.max_refreshes,
                        idle_seconds=self.limits.idle_seconds, max_spend_usd=self.limits.max_spend_usd,
                        reserved_usd=round(self.reserved_usd, 6), usage=self.last_usage,
                        http_status=self.last_http_status,
                        ttl_seconds=snapshot.ttl_seconds if snapshot and not snapshot.reason else None,
                        ttl_guaranteed=snapshot.ttl_guaranteed if snapshot and not snapshot.reason else None)

    def _snapshot(self):
        driver = getattr(self.runner, "pilot", None)
        return driver.cache_snapshot() if isinstance(driver, CacheRefreshDriver) else None

    def _retired(self):
        return self.closed or getattr(self.runner, "_replacement_retired", False) or getattr(self.runner, "_replacement_pending", False)

    def start(self, limits=None):
        with self.lock:
            if self._retired():
                raise ValueError("This session runner has retired")
            if self.enabled:
                return  # Repeated start cannot reset a running lease's budget.
            if self.attempt is not None:
                raise ValueError("The previous refresh is still stopping")
            self.limits = limits or KeepWarmLimits()
            self.count = 0
            self.reserved_usd = 0.0
            self.last_usage = None
            self.last_http_status = None
            self.last_activity = self.clock()
            self.enabled = True
            self._update_state()
            self._schedule()

    def stop(self, reason="Keep warm is off.", *, close=False):
        with self.lock:
            self.closed = self.closed or close
            self.enabled = False
            self.state, self.reason = "off", reason
            if self._timer:
                self._timer.cancel()
                self._timer = None
            attempt = self.attempt
        if attempt:
            attempt.cancel()

    def foreground_start(self):
        with self.lock:
            self._foreground += 1
            self.last_activity = self.clock()
            attempt = self.attempt
        if attempt:
            attempt.cancel()

    def foreground_end(self):
        with self.lock:
            self._foreground = max(0, self._foreground - 1)
            self.last_activity = self.clock()
            if self.enabled:
                self._update_state()
                self._schedule()

    def _update_state(self):
        driver = getattr(self.runner, "pilot", None)
        snapshot = self._snapshot()
        if not isinstance(driver, CacheRefreshDriver):
            self.state, self.reason = "unsupported", "This provider has no bounded cache refresh capability."
        elif snapshot is None:
            self.state, self.reason = "cold", "Waiting for a successful request on this session."
        elif snapshot.reason:
            self.state, self.reason = "unsupported", snapshot.reason
        elif self.clock() >= snapshot.started + snapshot.ttl_seconds:
            self.state, self.reason = "cold", "The cache lifetime elapsed; send a new turn before refreshing."
        elif snapshot.cached_tokens:
            self.state, self.reason = "warm", "Provider reported cached input; refresh also bills input reads or writes."
        else:
            self.state, self.reason = "cold", "Cache reuse has not yet been reported by the provider."

    def _schedule(self):
        if not self.schedule or not self.enabled or self.closed or self._timer is not None:
            return
        self._timer = threading.Timer(1.0, self.tick)
        self._timer.daemon = True
        self._timer.start()

    def tick(self):
        with self.lock:
            self._timer = None
            if not self.enabled or self._retired():
                return
            if self.attempt is not None:
                return
            snapshot = self._snapshot()
            now = self.clock()
            if now - self.last_activity >= self.limits.idle_seconds:
                self.enabled = False
                self.state, self.reason = "off", "Idle limit reached."
                return
            if self._foreground or getattr(self.runner, "_input_admissions", 0) or self.runner.is_turn_busy():
                self._schedule()
                return
            self._update_state()
            if snapshot is None or snapshot.reason or now >= snapshot.started + snapshot.ttl_seconds:
                self._schedule()
                return
            # Start 30s before expiry, measured from request START, not completion.
            if now < snapshot.started + snapshot.ttl_seconds - 30:
                self._schedule()
                return
            if self.count >= self.limits.max_refreshes:
                self.enabled = False
                self.state, self.reason = "off", "Refresh count limit reached."
                return
            if self.reserved_usd + snapshot.max_cost_usd > self.limits.max_spend_usd:
                self.enabled = False
                self.state, self.reason = "off", "Refresh input/output reserve exceeds the spend limit."
                return
            self.count += 1
            self.reserved_usd += snapshot.max_cost_usd
            self.attempt = attempt = RefreshAttempt()
            self.state, self.reason = "warming", "Refreshing cached input."
            driver = self.runner.pilot
        # Admission is complete. No lifecycle/controller lock crosses I/O.
        try:
            result = driver.refresh_cache(snapshot, attempt)
            with self.lock:
                if self.enabled and not self._retired() and not attempt.cancelled.is_set():
                    driver.accept_cache_refresh(snapshot, result)
                    self.last_usage = result.usage
                    self.last_http_status = result.http_status
                    self._update_state()
        except RefreshCancelled:
            with self.lock:
                if self.enabled:
                    self._update_state()
        except Exception as exc:
            with self.lock:
                if self.enabled and not attempt.cancelled.is_set():
                    self.enabled = False
                    self.state = "error"
                    # Provider bodies and request headers may contain secrets.
                    self.reason = "Refresh failed (" + type(exc).__name__ + "); restart keep warm to retry."
        finally:
            attempt.finished.set()
            with self.lock:
                self.attempt = None
                self._schedule()


def stop_runner_cache(runner, *, reason="Session closed.", close=True):
    controller = getattr(runner, "cache_keep_warm", None)
    if controller is not None:
        controller.stop(reason, close=close)


def set_reasoning_retention(runner, enabled: bool):
    if type(enabled) is not bool:
        raise ValueError("retain_reasoning must be a boolean")
    runner.retain_reasoning = enabled
    if not enabled:
        for message in getattr(runner, "_history", []):
            message.pop("reasoning_envelope", None)
