"""Hermetic tests for stdlib resource-pressure admission (WAVE 2 S16)."""
from __future__ import annotations

import json
import sys
from typing import Any, Callable, List, Tuple

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.resource_pressure import (
    ResourcePressureSnapshot,
    ResourcePressureThresholds,
    admit_resource_pressure,
    capture_resource_pressure_snapshot,
    evaluate_resource_pressure,
    format_resource_pressure_message,
    thresholds_from_config,
    wait_for_resource_capacity,
)


class _FakeFuture:
    def __init__(self) -> None:
        self._callbacks: List[Callable[["_FakeFuture"], None]] = []

    def add_done_callback(self, cb: Callable[["_FakeFuture"], None]) -> None:
        self._callbacks.append(cb)

    def fire_done(self) -> None:
        for cb in list(self._callbacks):
            cb(self)


class _FakePool:
    def __init__(self) -> None:
        self.calls: List[Tuple[Callable[..., Any], tuple]] = []

    def submit(self, fn: Callable[..., Any], *args: Any) -> _FakeFuture:
        self.calls.append((fn, args))
        return _FakeFuture()


def _noop() -> None:
    return None


def _fresh_session(monkeypatch: pytest.MonkeyPatch) -> ConversationalSession:
    sess = ConversationalSession(HarnessConfig())
    fake = _FakePool()
    sess._swarm_pool = fake  # type: ignore[assignment]
    with sess._swarm_futures_lock:
        sess._swarm_futures.clear()
    return sess


def _snap(
    *,
    load_1m: float | None = None,
    rss_bytes: int | None = None,
    open_fds: int | None = None,
    cpu_count: int | None = 4,
) -> ResourcePressureSnapshot:
    return ResourcePressureSnapshot(
        load_1m=load_1m,
        rss_bytes=rss_bytes,
        open_fds=open_fds,
        cpu_count=cpu_count,
    )


def test_thresholds_default_off():
    cfg = HarnessConfig()
    th = thresholds_from_config(cfg)
    assert th.enabled is False
    decision = evaluate_resource_pressure(_snap(rss_bytes=999_999_999_999), th)
    assert decision.admitted is True
    assert decision.action == "allow"


def test_config_max_workers_default_and_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_CONFIG", str(tmp_path / "absent.json"))
    monkeypatch.delenv("HARNESS_MAX_WORKERS", raising=False)
    assert HarnessConfig().max_workers == 4
    assert HarnessConfig.from_env().max_workers == 4

    monkeypatch.setenv("HARNESS_MAX_WORKERS", "8")
    assert HarnessConfig.from_env().max_workers == 8

    cfgfile = tmp_path / "h.json"
    cfgfile.write_text(json.dumps({"max_workers": 6}))
    monkeypatch.setenv("HARNESS_CONFIG", str(cfgfile))
    monkeypatch.delenv("HARNESS_MAX_WORKERS", raising=False)
    assert HarnessConfig.from_env().max_workers == 6


def test_resource_pressure_env_parsing(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_CONFIG", str(tmp_path / "absent.json"))
    monkeypatch.setenv("HARNESS_RESOURCE_PRESSURE_ENABLED", "1")
    monkeypatch.setenv("HARNESS_RESOURCE_PRESSURE_RSS_REJECT_MB", "512")
    monkeypatch.setenv("HARNESS_RESOURCE_PRESSURE_FD_REJECT", "900")
    monkeypatch.setenv("HARNESS_RESOURCE_PRESSURE_LOAD_REJECT", "2.5")
    monkeypatch.setenv("HARNESS_RESOURCE_PRESSURE_WAIT_TIMEOUT_SEC", "1.25")
    cfg = HarnessConfig.from_env()
    th = thresholds_from_config(cfg)
    assert th.enabled is True
    assert th.reject_rss_bytes == 512 * 1024 * 1024
    assert th.reject_open_fds == 900
    assert th.reject_load_per_cpu == 2.5
    assert th.wait_timeout_sec == 1.25


def test_advisory_passes_hard_rejects():
    th = ResourcePressureThresholds(
        enabled=True,
        advisory_rss_bytes=100,
        reject_rss_bytes=200,
    )
    advisory = evaluate_resource_pressure(_snap(rss_bytes=150), th)
    assert advisory.action == "advisory"
    assert advisory.admitted is True

    reject = evaluate_resource_pressure(_snap(rss_bytes=250), th)
    assert reject.action == "reject"
    assert reject.admitted is False


def test_bounded_wait_then_allow():
    th = ResourcePressureThresholds(
        enabled=True,
        advisory_rss_bytes=100,
        wait_timeout_sec=1.0,
        poll_interval_sec=0.01,
    )
    seq: List[int] = [250, 250, 50]

    def snap_fn():
        rss = seq.pop(0) if seq else 50
        return _snap(rss_bytes=rss)

    slept: List[float] = []

    def sleep_fn(sec: float) -> None:
        slept.append(sec)

    decision = wait_for_resource_capacity(
        th,
        snapshot_fn=snap_fn,
        sleep_fn=sleep_fn,
        monotonic_fn=lambda: 0.0,
    )
    assert decision.admitted is True
    assert decision.action == "allow"
    assert slept


def test_wait_exhausted_rejects_entire_batch():
    th = ResourcePressureThresholds(
        enabled=True,
        advisory_open_fds=10,
        wait_timeout_sec=0.05,
        poll_interval_sec=0.01,
    )
    clock = {"t": 0.0}

    def monotonic_fn() -> float:
        return clock["t"]

    def sleep_fn(sec: float) -> None:
        clock["t"] += sec

    decision = wait_for_resource_capacity(
        th,
        requested_workers=3,
        snapshot_fn=lambda: _snap(open_fds=99),
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
    )
    assert decision.action == "wait_exhausted"
    assert decision.admitted is False
    assert decision.requested_workers == 3
    msg = format_resource_pressure_message(decision)
    assert "3 worker" in msg


def test_unavailable_metrics_do_not_reject():
    th = ResourcePressureThresholds(
        enabled=True,
        reject_rss_bytes=100,
        reject_open_fds=10,
        reject_load_per_cpu=1.0,
    )
    decision = evaluate_resource_pressure(
        ResourcePressureSnapshot(None, None, None, None),
        th,
    )
    assert decision.admitted is True


def test_hard_reject_returns_immediately_without_waiting():
    th = ResourcePressureThresholds(
        enabled=True,
        reject_rss_bytes=100,
        wait_timeout_sec=5.0,
        poll_interval_sec=0.01,
    )
    slept: List[float] = []

    decision = wait_for_resource_capacity(
        th,
        snapshot_fn=lambda: _snap(rss_bytes=250),
        sleep_fn=lambda sec: slept.append(sec),
        monotonic_fn=lambda: 0.0,
    )
    assert decision.action == "reject"
    assert decision.admitted is False
    assert slept == []


def test_rss_uses_current_not_peak(monkeypatch):
    """Peak ru_maxrss must never drive reject — only current RSS readers."""
    import harness.resource_pressure as rp

    monkeypatch.setattr(rp, "_read_rss_bytes_linux_statm", lambda: 50 * 1024 * 1024)
    monkeypatch.setattr(rp, "_read_rss_bytes_ps", lambda: None)
    monkeypatch.setattr(rp.sys, "platform", "linux")

    snap = rp.capture_resource_pressure_snapshot()
    assert snap.rss_bytes == 50 * 1024 * 1024


def test_rss_peak_drops_current_drops_reject_clears(monkeypatch):
    import harness.resource_pressure as rp

    readings = [200 * 1024 * 1024, 50 * 1024 * 1024]

    def _current_rss():
        return readings.pop(0) if readings else 50 * 1024 * 1024

    th = rp.ResourcePressureThresholds(
        enabled=True,
        reject_rss_bytes=100 * 1024 * 1024,
    )
    high = rp.evaluate_resource_pressure(
        _snap(rss_bytes=_current_rss()),
        th,
    )
    assert high.action == "reject"
    low = rp.evaluate_resource_pressure(
        _snap(rss_bytes=_current_rss()),
        th,
    )
    assert low.action == "allow"


def test_rss_ps_fallback_on_macos(monkeypatch):
    import harness.resource_pressure as rp

    monkeypatch.setattr(rp.sys, "platform", "darwin")
    monkeypatch.setattr(rp, "_read_rss_bytes_linux_statm", lambda: None)
    monkeypatch.setattr(rp, "_read_rss_bytes_ps", lambda: 32 * 1024 * 1024)
    assert rp._read_rss_bytes() == 32 * 1024 * 1024


def test_rss_unavailable_on_windows(monkeypatch):
    import harness.resource_pressure as rp

    monkeypatch.setattr(rp.os, "name", "nt", raising=False)
    monkeypatch.setattr(rp.sys, "platform", "win32")
    assert rp._read_rss_bytes() is None


def test_resource_module_import_is_lazy_on_windows(monkeypatch):
    """Simulated Windows: RSS unavailable; module still loads without resource."""
    import builtins
    import importlib

    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "resource":
            raise ImportError("No module named 'resource'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    mod = importlib.reload(importlib.import_module("harness.resource_pressure"))
    monkeypatch.setattr(mod.os, "name", "nt", raising=False)
    monkeypatch.setattr(mod.sys, "platform", "win32")
    snap = mod.capture_resource_pressure_snapshot()
    assert snap.rss_bytes is None
    th = mod.ResourcePressureThresholds(enabled=False)
    assert mod.evaluate_resource_pressure(snap, th).admitted is True


def test_windows_open_fds_unavailable(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("harness.resource_pressure.os.name", "nt", raising=False)
    snap = capture_resource_pressure_snapshot()
    assert snap.open_fds is None


def test_no_duplicate_admission_probe_per_batch(monkeypatch: pytest.MonkeyPatch):
    cfg = HarnessConfig(
        resource_pressure_enabled=True,
        resource_pressure_rss_reject_mb=999999,
        resource_pressure_wait_timeout_sec=0,
    )
    sess = _fresh_session(monkeypatch)
    sess.config = cfg
    calls = {"n": 0}
    original = admit_resource_pressure

    def counting(th, **kwargs):
        calls["n"] += 1
        return original(th, **kwargs)

    monkeypatch.setattr("harness.resource_pressure.admit_resource_pressure", counting)

    group = "batch-1"
    assert sess._submit_swarm(_noop, admission_group=group, admission_size=3) is True
    assert sess._submit_swarm(_noop, admission_group=group, admission_size=3) is True
    assert calls["n"] == 1


def test_resource_pressure_reject_preserves_requested_count(
    monkeypatch: pytest.MonkeyPatch,
):
    cfg = HarnessConfig(
        resource_pressure_enabled=True,
        resource_pressure_rss_reject_mb=1,
        resource_pressure_wait_timeout_sec=0,
    )
    sess = _fresh_session(monkeypatch)
    sess.config = cfg
    fake_pool: _FakePool = sess._swarm_pool  # type: ignore[assignment]

    monkeypatch.setattr(
        "harness.resource_pressure.capture_resource_pressure_snapshot",
        lambda: _snap(rss_bytes=500 * 1024 * 1024),
    )

    ok = sess._submit_swarm(_noop, admission_group="parallel-x", admission_size=4)
    assert ok is False
    assert fake_pool.calls == []
    assert sess._last_swarm_submit_reason == "resource_pressure"
    assert sess._last_resource_pressure_decision.requested_workers == 4


def test_hard_reject_does_not_touch_pool(monkeypatch: pytest.MonkeyPatch):
    cfg = HarnessConfig(
        resource_pressure_enabled=True,
        resource_pressure_rss_reject_mb=1,
        resource_pressure_wait_timeout_sec=0,
    )
    sess = _fresh_session(monkeypatch)
    sess.config = cfg

    def always_high():
        return _snap(rss_bytes=500 * 1024 * 1024, cpu_count=4)

    monkeypatch.setattr(
        "harness.resource_pressure.capture_resource_pressure_snapshot",
        always_high,
    )
    fake_pool: _FakePool = sess._swarm_pool  # type: ignore[assignment]
    assert sess._submit_swarm(_noop) is False
    assert fake_pool.calls == []


def test_existing_swarm_capacity_still_applies(monkeypatch: pytest.MonkeyPatch):
    sess = _fresh_session(monkeypatch)
    fake_pool: _FakePool = sess._swarm_pool  # type: ignore[assignment]
    with sess._swarm_futures_lock:
        for _ in range(sess._swarm_capacity):
            sess._swarm_futures.add(_FakeFuture())
    assert sess._submit_swarm(_noop) is False
    assert sess._last_swarm_submit_reason == "capacity"
    assert fake_pool.calls == []


def test_session_uses_config_max_workers():
    cfg = HarnessConfig(max_workers=6)
    sess = ConversationalSession(cfg)
    assert sess._swarm_capacity == 24


def test_session_max_workers_fallback_without_attribute():
    """Config-like test/extension objects without max_workers keep default 4."""

    class _MinimalConfig:
        repo = ""
        driver = "stub-oracle-v2"
        reach = "local"
        state_dir = ""
        max_context_tokens = 96_000

    sess = ConversationalSession(_MinimalConfig())  # type: ignore[arg-type]
    assert sess._swarm_capacity == 16
    sess._swarm_pool.shutdown(wait=False)


@pytest.mark.resource_soak
def test_resource_snapshot_capture_does_not_leak():
    """Optional soak: repeated snapshot capture stays bounded (manual/CI opt-in)."""
    before = capture_resource_pressure_snapshot()
    for _ in range(32):
        snap = capture_resource_pressure_snapshot()
        assert snap.rss_bytes is None or snap.rss_bytes >= 0
    after = capture_resource_pressure_snapshot()
    if before.open_fds is not None and after.open_fds is not None:
        assert after.open_fds - before.open_fds < 32
