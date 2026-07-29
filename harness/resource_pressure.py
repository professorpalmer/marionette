"""Pure stdlib resource-pressure probes and admission decisions.

Used at Marionette concurrency choke points (``_submit_swarm``) to optionally
wait or reject an entire requested submission when CPU load, process RSS, or
open file-descriptor counts exceed configured thresholds. Defaults are off so
behavior is unchanged unless explicitly enabled via env/config.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Tuple


@dataclass(frozen=True)
class ResourcePressureSnapshot:
    """Point-in-time process/host pressure metrics (any field may be unavailable)."""

    load_1m: Optional[float] = None
    rss_bytes: Optional[int] = None
    open_fds: Optional[int] = None
    cpu_count: Optional[int] = None


@dataclass(frozen=True)
class ResourcePressureThresholds:
    """Env/configurable advisory and hard-reject ceilings."""

    enabled: bool = False
    advisory_rss_bytes: Optional[int] = None
    reject_rss_bytes: Optional[int] = None
    advisory_open_fds: Optional[int] = None
    reject_open_fds: Optional[int] = None
    advisory_load_per_cpu: Optional[float] = None
    reject_load_per_cpu: Optional[float] = None
    wait_timeout_sec: float = 5.0
    poll_interval_sec: float = 0.25


@dataclass(frozen=True)
class ResourcePressureDecision:
    """Outcome of evaluating one snapshot against thresholds."""

    action: str  # allow | advisory | reject | wait_exhausted
    reasons: Tuple[str, ...]
    snapshot: ResourcePressureSnapshot
    requested_workers: int = 1

    @property
    def admitted(self) -> bool:
        return self.action in ("allow", "advisory")


def capture_resource_pressure_snapshot() -> ResourcePressureSnapshot:
    """Collect best-effort metrics without raising."""
    return ResourcePressureSnapshot(
        load_1m=_read_load_1m(),
        rss_bytes=_read_rss_bytes(),
        open_fds=_read_open_fd_count(),
        cpu_count=_read_cpu_count(),
    )


def _read_load_1m() -> Optional[float]:
    getloadavg = getattr(os, "getloadavg", None)
    if not callable(getloadavg):
        return None
    try:
        return float(getloadavg()[0])
    except OSError:
        return None


def _page_size_bytes() -> int:
    try:
        size = os.sysconf("SC_PAGE_SIZE")
        if size and size > 0:
            return int(size)
    except (AttributeError, OSError, ValueError):
        pass
    return 4096


def _read_rss_bytes_linux_statm() -> Optional[int]:
    try:
        with open("/proc/self/statm", encoding="ascii") as fh:
            parts = fh.read().strip().split()
        if len(parts) < 2:
            return None
        rss_pages = int(parts[1])
        if rss_pages <= 0:
            return None
        return rss_pages * _page_size_bytes()
    except (OSError, ValueError):
        return None


def _read_rss_bytes_ps() -> Optional[int]:
    """Bounded ``ps -o rss=`` fallback (KiB on macOS/BSD, KiB on many Unixes)."""
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode != 0:
        return None
    raw = (result.stdout or "").strip().split()
    if not raw:
        return None
    try:
        rss_kib = int(raw[0])
    except ValueError:
        return None
    if rss_kib <= 0:
        return None
    return rss_kib * 1024


def _read_rss_bytes() -> Optional[int]:
    """Current resident set size — never ``ru_maxrss`` peak."""
    if os.name == "nt":
        return None
    if sys.platform.startswith("linux"):
        rss = _read_rss_bytes_linux_statm()
        if rss is not None:
            return rss
    return _read_rss_bytes_ps()


def _read_open_fd_count() -> Optional[int]:
    if os.name == "nt":
        return None
    fd_dir = "/proc/self/fd"
    if not os.path.isdir(fd_dir):
        return None
    try:
        return len(os.listdir(fd_dir))
    except OSError:
        return None


def _read_cpu_count() -> Optional[int]:
    try:
        count = os.cpu_count()
        return int(count) if count and count > 0 else None
    except Exception:
        return None


def _load_per_cpu(snapshot: ResourcePressureSnapshot) -> Optional[float]:
    if snapshot.load_1m is None or not snapshot.cpu_count:
        return None
    return snapshot.load_1m / float(snapshot.cpu_count)


def _threshold_violations(
    snapshot: ResourcePressureSnapshot,
    thresholds: ResourcePressureThresholds,
    *,
    reject: bool,
) -> Tuple[str, ...]:
    reasons: list[str] = []
    rss_limit = thresholds.reject_rss_bytes if reject else thresholds.advisory_rss_bytes
    if rss_limit is not None and snapshot.rss_bytes is not None and snapshot.rss_bytes >= rss_limit:
        label = "reject" if reject else "advisory"
        reasons.append(f"process RSS {snapshot.rss_bytes} bytes >= {label} {rss_limit}")

    fd_limit = thresholds.reject_open_fds if reject else thresholds.advisory_open_fds
    if fd_limit is not None and snapshot.open_fds is not None and snapshot.open_fds >= fd_limit:
        label = "reject" if reject else "advisory"
        reasons.append(f"open FDs {snapshot.open_fds} >= {label} {fd_limit}")

    load_limit = thresholds.reject_load_per_cpu if reject else thresholds.advisory_load_per_cpu
    load_ratio = _load_per_cpu(snapshot)
    if load_limit is not None and load_ratio is not None and load_ratio >= load_limit:
        label = "reject" if reject else "advisory"
        reasons.append(f"load/cpu {load_ratio:.2f} >= {label} {load_limit}")

    return tuple(reasons)


def evaluate_resource_pressure(
    snapshot: ResourcePressureSnapshot,
    thresholds: ResourcePressureThresholds,
    *,
    requested_workers: int = 1,
) -> ResourcePressureDecision:
    """Return allow/advisory/reject for a single snapshot."""
    _ = requested_workers  # reserved for future per-worker scaling
    if not thresholds.enabled:
        return ResourcePressureDecision("allow", (), snapshot, requested_workers)

    reject_reasons = _threshold_violations(snapshot, thresholds, reject=True)
    if reject_reasons:
        return ResourcePressureDecision("reject", reject_reasons, snapshot, requested_workers)

    advisory_reasons = _threshold_violations(snapshot, thresholds, reject=False)
    if advisory_reasons:
        return ResourcePressureDecision("advisory", advisory_reasons, snapshot, requested_workers)

    return ResourcePressureDecision("allow", (), snapshot, requested_workers)


def wait_for_resource_capacity(
    thresholds: ResourcePressureThresholds,
    *,
    requested_workers: int = 1,
    snapshot_fn: Callable[[], ResourcePressureSnapshot] = capture_resource_pressure_snapshot,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> ResourcePressureDecision:
    """Poll until pressure clears, timeout expires, or reject persists."""
    if not thresholds.enabled:
        snap = snapshot_fn()
        return ResourcePressureDecision("allow", (), snap, requested_workers)

    deadline = monotonic_fn() + max(0.0, float(thresholds.wait_timeout_sec))
    last_decision: Optional[ResourcePressureDecision] = None
    while True:
        snap = snapshot_fn()
        decision = evaluate_resource_pressure(
            snap, thresholds, requested_workers=requested_workers,
        )
        if decision.action == "allow":
            return decision
        if decision.action == "reject":
            return decision
        # Advisory pressure: poll until it clears or the wait budget expires.
        last_decision = decision
        if monotonic_fn() >= deadline:
            break
        sleep_fn(max(0.0, float(thresholds.poll_interval_sec)))

    assert last_decision is not None
    return ResourcePressureDecision(
        "wait_exhausted",
        last_decision.reasons,
        last_decision.snapshot,
        requested_workers,
    )


def format_resource_pressure_message(decision: ResourcePressureDecision) -> str:
    """Pilot-visible capacity reason for a rejected/exhausted admission."""
    if decision.admitted:
        return ""
    detail = "; ".join(decision.reasons) if decision.reasons else "resource pressure"
    if decision.action == "wait_exhausted":
        return (
            f"Resource capacity still constrained after waiting "
            f"({detail}); not dispatching {decision.requested_workers} worker(s) right now."
        )
    return (
        f"Resource capacity constrained ({detail}); not dispatching "
        f"{decision.requested_workers} worker(s) right now."
    )


def _parse_optional_int(raw: object) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _parse_optional_float(raw: object) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_bool(raw: object, *, default: bool = False) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _mb_to_bytes(raw: object) -> Optional[int]:
    mb = _parse_optional_int(raw)
    if mb is None:
        return None
    return max(0, mb) * 1024 * 1024


def thresholds_from_mapping(mapping: dict) -> ResourcePressureThresholds:
    """Build thresholds from HarnessConfig fields or env-derived dict."""
    enabled = _parse_bool(mapping.get("resource_pressure_enabled"), default=False)
    # Any explicit reject threshold implies enabled unless explicitly disabled.
    if not enabled:
        for key in (
            "resource_pressure_rss_reject_mb",
            "resource_pressure_fd_reject",
            "resource_pressure_load_reject",
        ):
            if mapping.get(key) not in (None, ""):
                enabled = True
                break

    return ResourcePressureThresholds(
        enabled=enabled,
        advisory_rss_bytes=_mb_to_bytes(mapping.get("resource_pressure_rss_advisory_mb")),
        reject_rss_bytes=_mb_to_bytes(mapping.get("resource_pressure_rss_reject_mb")),
        advisory_open_fds=_parse_optional_int(mapping.get("resource_pressure_fd_advisory")),
        reject_open_fds=_parse_optional_int(mapping.get("resource_pressure_fd_reject")),
        advisory_load_per_cpu=_parse_optional_float(mapping.get("resource_pressure_load_advisory")),
        reject_load_per_cpu=_parse_optional_float(mapping.get("resource_pressure_load_reject")),
        wait_timeout_sec=float(
            _parse_optional_float(mapping.get("resource_pressure_wait_timeout_sec")) or 5.0
        ),
        poll_interval_sec=float(
            _parse_optional_float(mapping.get("resource_pressure_poll_interval_sec")) or 0.25
        ),
    )


def thresholds_from_config(config: object) -> ResourcePressureThresholds:
    """Read resource-pressure settings from a HarnessConfig-like object."""
    fields = (
        "resource_pressure_enabled",
        "resource_pressure_rss_advisory_mb",
        "resource_pressure_rss_reject_mb",
        "resource_pressure_fd_advisory",
        "resource_pressure_fd_reject",
        "resource_pressure_load_advisory",
        "resource_pressure_load_reject",
        "resource_pressure_wait_timeout_sec",
        "resource_pressure_poll_interval_sec",
    )
    mapping = {name: getattr(config, name, None) for name in fields}
    return thresholds_from_mapping(mapping)


def admit_resource_pressure(
    thresholds: ResourcePressureThresholds,
    *,
    requested_workers: int = 1,
    snapshot_fn: Callable[[], ResourcePressureSnapshot] = capture_resource_pressure_snapshot,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> ResourcePressureDecision:
    """Single admission probe: wait (bounded) then allow or reject."""
    requested = max(1, int(requested_workers))
    if not thresholds.enabled:
        snap = snapshot_fn()
        return ResourcePressureDecision("allow", (), snap, requested)

    if thresholds.wait_timeout_sec > 0:
        return wait_for_resource_capacity(
            thresholds,
            requested_workers=requested,
            snapshot_fn=snapshot_fn,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
        )

    snap = snapshot_fn()
    return evaluate_resource_pressure(snap, thresholds, requested_workers=requested)
