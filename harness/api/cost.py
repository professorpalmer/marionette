"""Shared cost / usage / swarm-accounting helpers (peeled from ``harness.server``).

Owns prompt-cache multipliers, /api/usage response cache, session + swarm cost
math, boot-meter carry/persist/restore, and workspace-scoped job store merges
used by ``harness.api.usage`` and ``harness.api.jobs``. ``server.py`` re-exports
historical ``_`` names; inject live globals via :func:`bind_deps`.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple


@dataclass
class CostDeps:
    """Server-side deps cost helpers must not import from ``harness.server``."""

    diag: Callable[..., Any]
    get_cfg: Callable[[], Any]
    get_pilot: Callable[[], Any]
    get_runners: Callable[[], Any]
    get_sessions: Callable[[], Any]
    get_session: Callable[[], Any]
    jobs_snapshot: Callable[[], list]


_deps: Optional[CostDeps] = None


def bind_deps(deps: CostDeps) -> None:
    """Wire server callables once during server module import."""
    global _deps
    _deps = deps


def _require_deps() -> CostDeps:
    if _deps is None:
        raise RuntimeError("cost.bind_deps() was not called")
    return _deps


def _diag(*args: Any, **kwargs: Any) -> Any:
    return _require_deps().diag(*args, **kwargs)


def _cfg():
    return _require_deps().get_cfg()


def _pilot():
    return _require_deps().get_pilot()


def _runners():
    return _require_deps().get_runners()


def _sessions():
    return _require_deps().get_sessions()


def _session():
    return _require_deps().get_session()


def _jobs_snapshot():
    return _require_deps().jobs_snapshot()


def _server_attr(name: str, fallback: Any) -> Any:
    """Prefer ``harness.server.<name>`` so test monkeypatches still land.

    After this peel, helpers live here but historical tests patch
    ``harness.server._job_swarm_accounting`` (etc.). Call-time lookup through
    the server module keeps that contract without importing server at top level.
    """
    try:
        import sys

        srv = sys.modules.get("harness.server")
        if srv is not None:
            return getattr(srv, name)
    except Exception:
        pass
    return fallback


# Prompt-cache FALLBACK multipliers (used only when the provider did not return
# usage.cost). OpenRouter billed USD is preferred whenever present.
# Anthropic/Bedrock published rates: reads ~0.1x, 5m writes 1.25x, 1h writes 2x.
# OpenAI/Gemini implicit cache is usually read-only (write bucket stays 0).
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
# Undifferentiated cache_write_tokens (no TTL split) billed at the 5m write rate.
CACHE_WRITE_MULTIPLIER = CACHE_WRITE_5M_MULTIPLIER

# Short-TTL cache for /api/usage boot-pill aggregation (StatusBar polls ~10s).
# Building the response walks every boot-repo job store; serve a hot copy for a
# few seconds like /api/codegraph status.
_usage_response_cache: Dict[str, Tuple[float, dict]] = {}
# Burst dedupe only. StatusBar polls ~10s — a TTL near that interval freezes the
# boot pill across polls (and poisons hermetic pytest order). Keep this short.
_USAGE_RESPONSE_TTL = 2.0
_usage_response_lock = threading.Lock()


def _usage_cache_get(key: str) -> Optional[dict]:
    # Hermetic tests share the process-global cache across cases; never serve
    # a prior test's /api/usage payload.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    now = time.monotonic()
    with _usage_response_lock:
        hit = _usage_response_cache.get(key)
        if not hit:
            return None
        expiry, payload = hit
        if expiry <= now:
            _usage_response_cache.pop(key, None)
            return None
        return payload


def _usage_cache_put(key: str, payload: dict) -> None:
    with _usage_response_lock:
        _usage_response_cache[key] = (time.monotonic() + _USAGE_RESPONSE_TTL, payload)


def _usage_cache_clear_for_tests() -> None:
    with _usage_response_lock:
        _usage_response_cache.clear()


def _session_cost(
    t_in: float,
    t_out: float,
    cached: float,
    price_in: float,
    price_out: float,
    cache_write: float = 0.0,
    cache_write_5m: float = 0.0,
    cache_write_1h: float = 0.0,
) -> float:
    """Deterministic session cost from tokens + per-Mtok prices.

    ``t_in`` is the FULL prompt token total (uncached + cache read + cache
    write). Cache-read / cache-write buckets are peeled out of that total and
    billed at their multipliers; the remainder is full-price input. Falls back
    to pricing the combined total at ``price_out`` when no in/out split is
    available (completion dominates cost, so this is the least-wrong
    single-rate estimate)."""
    t_in = float(t_in or 0.0)
    t_out = float(t_out or 0.0)
    cached = max(0.0, float(cached or 0.0))
    w5 = max(0.0, float(cache_write_5m or 0.0))
    w1 = max(0.0, float(cache_write_1h or 0.0))
    w_u = max(0.0, float(cache_write or 0.0))
    if w5 or w1:
        split = w5 + w1
        # Prefer TTL splits; drop overlapping undifferentiated write totals.
        if w_u <= split + 0.5:
            w_u = 0.0
        else:
            w_u = max(0.0, w_u - split)
    if t_in or t_out or cached or w5 or w1 or w_u:
        cached = min(cached, t_in)
        remain = max(0.0, t_in - cached)
        w1 = min(w1, remain)
        remain -= w1
        w5 = min(w5, remain)
        remain -= w5
        w_u = min(w_u, remain)
        remain -= w_u
        uncached_in = remain
        return (
            (uncached_in / 1.0e6) * price_in
            + (cached / 1.0e6) * price_in * CACHE_READ_MULTIPLIER
            + (w5 / 1.0e6) * price_in * CACHE_WRITE_5M_MULTIPLIER
            + (w1 / 1.0e6) * price_in * CACHE_WRITE_1H_MULTIPLIER
            + (w_u / 1.0e6) * price_in * CACHE_WRITE_MULTIPLIER
            + (t_out / 1.0e6) * price_out
        )
    # No split tracked: price the combined total at the output rate.
    total = t_in + t_out
    return (total / 1.0e6) * price_out


def _pilot_write_buckets(pilot: Any) -> tuple:
    """Return (cache_write, write_5m, write_1h) meters for a pilot-like object."""
    return (
        int(getattr(pilot, "_tokens_cache_write", 0) or 0),
        int(getattr(pilot, "_tokens_cache_write_5m", 0) or 0),
        int(getattr(pilot, "_tokens_cache_write_1h", 0) or 0),
    )


def _session_cost_split(pilot: Any, price_in: float, price_out: float) -> float:
    """Session cost that prices PILOT tokens at the pilot rate and ADDS
    delegated-worker dollars (already priced at each worker's own model rate).

    Worker tokens are folded into the pilot's _tokens_* meters for display, but
    pricing them at the pilot rate under-reports cost when a worker ran on a
    pricier model (e.g. opus at $5/$25 vs a cheap pilot). So we subtract the
    worker token split from the pilot-priced portion and add _worker_cost_usd.

    When the pilot accumulated OpenRouter (or similar) ``usage.cost`` into
    ``_provider_cost_usd``, that billed USD is ground truth for the covered
    token slice; any remaining uncovered pilot tokens fall back to the
    cache-aware catalog estimate. getattr defaults keep OLD sessions (no
    worker / provider split) identical to before."""
    t_in = int(getattr(pilot, "_tokens_in", 0) or 0)
    t_out = int(getattr(pilot, "_tokens_out", 0) or 0)
    t_cached = int(getattr(pilot, "_tokens_cached", 0) or 0)
    t_write, t_write_5m, t_write_1h = _pilot_write_buckets(pilot)
    w_in = int(getattr(pilot, "_worker_tokens_in", 0) or 0)
    w_out = int(getattr(pilot, "_worker_tokens_out", 0) or 0)
    w_cost = float(getattr(pilot, "_worker_cost_usd", 0.0) or 0.0)
    provider_cost = float(getattr(pilot, "_provider_cost_usd", 0.0) or 0.0)
    billed_in = int(getattr(pilot, "_provider_billed_tokens_in", 0) or 0)
    billed_out = int(getattr(pilot, "_provider_billed_tokens_out", 0) or 0)
    billed_cached = int(getattr(pilot, "_provider_billed_tokens_cached", 0) or 0)
    billed_write = int(getattr(pilot, "_provider_billed_tokens_cache_write", 0) or 0)
    billed_write_5m = int(getattr(pilot, "_provider_billed_tokens_cache_write_5m", 0) or 0)
    billed_write_1h = int(getattr(pilot, "_provider_billed_tokens_cache_write_1h", 0) or 0)
    pilot_in = max(0, t_in - w_in)
    pilot_out = max(0, t_out - w_out)
    # Cached / write tokens are subsets of pilot input; clamp so discounts /
    # premiums never exceed the pilot input we are actually pricing here.
    pilot_cached = max(0, min(t_cached, pilot_in))
    pilot_write = max(0, min(t_write, pilot_in))
    pilot_write_5m = max(0, min(t_write_5m, pilot_in))
    pilot_write_1h = max(0, min(t_write_1h, pilot_in))
    if billed_in > 0 or billed_out > 0:
        rem_in = max(0, pilot_in - billed_in)
        rem_out = max(0, pilot_out - billed_out)
        rem_cached = max(0, min(max(0, pilot_cached - billed_cached), rem_in))
        rem_write = max(0, min(max(0, pilot_write - billed_write), rem_in))
        rem_w5 = max(0, min(max(0, pilot_write_5m - billed_write_5m), rem_in))
        rem_w1 = max(0, min(max(0, pilot_write_1h - billed_write_1h), rem_in))
        return (
            provider_cost
            + _session_cost(
                rem_in, rem_out, rem_cached, price_in, price_out,
                cache_write=rem_write,
                cache_write_5m=rem_w5,
                cache_write_1h=rem_w1,
            )
            + w_cost
        )
    return (
        _session_cost(
            pilot_in, pilot_out, pilot_cached, price_in, price_out,
            cache_write=pilot_write,
            cache_write_5m=pilot_write_5m,
            cache_write_1h=pilot_write_1h,
        )
        + w_cost
    )


def _cache_savings(cached: float, price_in: float) -> float:
    """USD saved by billing ``cached`` prompt tokens at the cache-read discount
    instead of the full input price (catalog-rate fallback estimate).

    Cache-write premiums are a cost, not a saving -- they are excluded here."""
    return (float(cached) / 1.0e6) * price_in * (1.0 - CACHE_READ_MULTIPLIER)


def _cost_source_label(pilot_like: Any) -> str:
    """How pilot spend was derived: provider | mixed | estimated | plan_estimated."""
    billed_in = int(getattr(pilot_like, "_provider_billed_tokens_in", 0) or 0)
    billed_out = int(getattr(pilot_like, "_provider_billed_tokens_out", 0) or 0)
    if billed_in <= 0 and billed_out <= 0:
        if getattr(pilot_like, "_plan_billing", False):
            return "plan_estimated"
        return "estimated"
    t_in = int(getattr(pilot_like, "_tokens_in", 0) or 0)
    t_out = int(getattr(pilot_like, "_tokens_out", 0) or 0)
    w_in = int(getattr(pilot_like, "_worker_tokens_in", 0) or 0)
    w_out = int(getattr(pilot_like, "_worker_tokens_out", 0) or 0)
    pilot_in = max(0, t_in - w_in)
    pilot_out = max(0, t_out - w_out)
    if billed_in >= pilot_in and billed_out >= pilot_out:
        return "provider"
    return "mixed"


_BOOT_PLAN_BILLING: bool = False


def _boot_cost_source() -> str:
    """Aggregate cost_source across carry + live runners."""
    from types import SimpleNamespace

    totals = _boot_usage_meters()
    label = _cost_source_label(SimpleNamespace(**totals))
    if label != "estimated":
        return label
    if _BOOT_PLAN_BILLING:
        return "plan_estimated"
    try:
        live = list(_runners().runners())
    except Exception:
        live = []
    if _pilot() is not None and id(_pilot()) not in {id(r) for r in live}:
        live.append(_pilot())
    for runner in live:
        if getattr(runner, "_plan_billing", False):
            return "plan_estimated"
    return "estimated"


# Cost epoch for THIS app run. The swarm store (SQLite) persists across
# launches, so /api/usage must not bill the "session" for every job ever run
# in the state dir -- only jobs created after this process started, matching
# the pilot token meters (which also reset per process).
_COST_EPOCH = datetime.now(timezone.utc)

# Process-lifetime boot meters for the status-bar spend pill. Live runners keep
# their own counters; on drop/evict those meters fold into this carry so the
# pill never resets when the UI attaches a different session. New runners start
# at zero -- do NOT snapshot meters into them on attach/create (that would
# double-count once /api/usage sums carry + all live runners).
#
# Across backend restarts inside the SAME Electron app run, carry + cost epoch
# are restored from boot_usage.json when HARNESS_APP_RUN_ID matches (minted once
# per desktop launch). A full app quit+relaunch mints a new id and the status
# bar starts at zero -- that is the only intentional reset.
_BOOT_METER_ATTRS = (
    "_tokens_used",
    "_tokens_in",
    "_tokens_out",
    "_tokens_cached",
    "_tokens_cache_write",
    "_tokens_cache_write_5m",
    "_tokens_cache_write_1h",
    "_worker_cost_usd",
    "_worker_tokens_in",
    "_worker_tokens_out",
    "_provider_cost_usd",
    "_provider_billed_tokens_in",
    "_provider_billed_tokens_out",
    "_provider_billed_tokens_cached",
    "_provider_billed_tokens_cache_write",
    "_provider_billed_tokens_cache_write_5m",
    "_provider_billed_tokens_cache_write_1h",
)
_BOOT_METER_CARRY: dict[str, float] = {attr: 0.0 for attr in _BOOT_METER_ATTRS}
# Priced USD folded with dropped runners at fold-time rates. Token meters in
# carry stay for display; cost must NOT be recomputed at a later pilot rate
# after a model swap (that would silently reprice historical spend).
_BOOT_CARRY_COST_USD: float = 0.0
# Every workspace opened this process -- boot-pill swarm dollars merge
# epoch-windowed jobs across these repos, not only the active _cfg().repo.
_BOOT_REPOS: set[str] = set()
_BOOT_USAGE_PERSIST_LOCK = threading.Lock()
_BOOT_USAGE_LAST_PERSIST = 0.0
_BOOT_USAGE_RESTORED = False


def _job_in_cost_window(created_at: Any) -> bool:
    """True when a swarm-store job belongs to this app run's cost window.
    Unknown/unparseable timestamps are kept (better to overshow live work than
    silently drop a job that is really spending)."""
    if not created_at:
        return True
    try:
        stamp = datetime.fromisoformat(str(created_at))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp >= _COST_EPOCH
    except Exception:
        return True


def _app_run_id() -> str:
    return (os.environ.get("HARNESS_APP_RUN_ID") or "").strip()


def _boot_usage_path() -> str:
    root = (getattr(_cfg(), "state_dir", None) or "").strip() or os.path.join(
        os.path.expanduser("~"), ".pmharness", "state"
    )
    return os.path.join(root, "boot_usage.json")


def _fold_all_live_runners_into_boot_carry() -> None:
    """Collapse every live runner into carry so a backend restart can persist one blob."""
    try:
        live = list(_runners().runners())
    except Exception:
        live = []
    seen = {id(r) for r in live}
    try:
        pilot = _pilot()
    except Exception:
        pilot = None
    if pilot is not None and id(pilot) not in seen:
        live.append(pilot)
    for runner in live:
        try:
            _fold_runner_meters_into_boot_carry("", runner)
        except Exception:
            pass


def _persist_boot_usage(*, fold_live: bool = False, force: bool = False) -> None:
    """Write boot meters + cost epoch for same-app-run backend respawns.

    Snapshot is ``carry + live runners`` (same shape as the status-bar boot
    pill) so a crash/respawn restores spend/savings without zeroing the live
    process. ``fold_live=True`` is for intentional restart paths that are about
    to kill the process anyway.

    No-op without HARNESS_APP_RUN_ID (tests / bare CLI) so hermetic runs stay clean.
    """
    global _BOOT_USAGE_LAST_PERSIST
    run_id = _app_run_id()
    if not run_id:
        return
    now = time.time()
    with _BOOT_USAGE_PERSIST_LOCK:
        if not force and (now - _BOOT_USAGE_LAST_PERSIST) < 2.0:
            return
        try:
            if fold_live:
                _fold_all_live_runners_into_boot_carry()
                carry_snap = {
                    attr: float(_BOOT_METER_CARRY.get(attr, 0.0) or 0.0)
                    for attr in _BOOT_METER_ATTRS
                }
                cost_snap = float(_BOOT_CARRY_COST_USD or 0.0)
            else:
                try:
                    carry_snap = {
                        attr: float(v)
                        for attr, v in _boot_usage_meters().items()
                        if attr in _BOOT_METER_ATTRS
                    }
                except Exception:
                    carry_snap = {
                        attr: float(_BOOT_METER_CARRY.get(attr, 0.0) or 0.0)
                        for attr in _BOOT_METER_ATTRS
                    }
                try:
                    resolve_prices = _server_attr(
                        "_resolve_active_prices", _resolve_active_prices
                    )
                    price_in, price_out = resolve_prices()
                    boot_cost = _server_attr("_boot_session_cost", _boot_session_cost)
                    cost_snap = float(boot_cost(price_in, price_out))
                except Exception:
                    cost_snap = float(_BOOT_CARRY_COST_USD or 0.0)
            payload = {
                "app_run_id": run_id,
                "cost_epoch": _COST_EPOCH.isoformat(),
                "carry": carry_snap,
                "carry_cost_usd": cost_snap,
                "plan_billing": bool(_BOOT_PLAN_BILLING),
                "repos": sorted(_BOOT_REPOS),
                "saved_at": now,
            }
            path = _boot_usage_path()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            os.replace(tmp, path)
            _BOOT_USAGE_LAST_PERSIST = now
        except Exception as e:
            _diag("server.boot_usage_persist", e)


def _restore_boot_usage() -> bool:
    """Reload boot meters when this backend shares the Electron app-run id."""
    global _COST_EPOCH, _BOOT_USAGE_RESTORED, _BOOT_CARRY_COST_USD, _BOOT_PLAN_BILLING
    if _BOOT_USAGE_RESTORED:
        return False
    _BOOT_USAGE_RESTORED = True
    run_id = _app_run_id()
    if not run_id:
        return False
    path = _boot_usage_path()
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return False
        if str(data.get("app_run_id") or "").strip() != run_id:
            return False
        epoch_raw = data.get("cost_epoch")
        if epoch_raw:
            stamp = datetime.fromisoformat(str(epoch_raw))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            _COST_EPOCH = stamp
        carry = data.get("carry") or {}
        if isinstance(carry, dict):
            for attr in _BOOT_METER_ATTRS:
                try:
                    _BOOT_METER_CARRY[attr] = float(carry.get(attr, 0.0) or 0.0)
                except Exception:
                    pass
        try:
            _BOOT_CARRY_COST_USD = float(data.get("carry_cost_usd", 0.0) or 0.0)
        except Exception:
            _BOOT_CARRY_COST_USD = 0.0
        try:
            _BOOT_PLAN_BILLING = bool(data.get("plan_billing", False))
        except Exception:
            _BOOT_PLAN_BILLING = False
        for repo in data.get("repos") or []:
            try:
                if repo and os.path.isdir(str(repo)):
                    _BOOT_REPOS.add(os.path.abspath(str(repo)))
            except Exception:
                pass
        return True
    except Exception as e:
        _diag("server.boot_usage_restore", e)
        return False


def _active_session_total(session_job_ids, arts_getter, registry) -> Any:
    """Lifetime running total for the ACTIVE chat session, surviving restarts.

    The boot pill above resets to $0 on every relaunch/update (pilot meters are
    per-process, swarm dollars are epoch-windowed), which loses the budgeting
    trail. This figure instead combines:

    * the session row's persisted meters (pilot spend + local-worker dollars,
      accumulated turn-by-turn in harness_sessions.json), and
    * dollars for every swarm-store job stamped with this session id, across
      ALL app runs -- store-job dollars are deliberately kept OUT of the
      persisted meters (see _add_worker_tokens_from_artifacts) so pricing them
      here from artifacts x registry never double-bills.
    """
    sid = _sessions().active or ""
    if not sid:
        return None
    row = next((s for s in _sessions().list() if s.get("id") == sid), None)
    if row is None:
        return None
    swarm_cost = 0.0
    job_acct = _server_attr("_job_swarm_accounting", _job_swarm_accounting)
    for jid in session_job_ids:
        try:
            _tokens, cost = job_acct(arts_getter(jid), registry)
            swarm_cost += cost
        except Exception as e:
            _diag("server.session_total_job", e, msg=f"job={jid}")
    return {
        "session_id": sid,
        "est_cost_usd": round(
            float(row.get("estimated_cost_usd") or 0.0) + swarm_cost, 6
        ),
        "input_tokens": int(row.get("input_tokens") or 0),
        "output_tokens": int(row.get("output_tokens") or 0),
    }


def _repo_session_stamped_meters(repo_root: str) -> dict:
    """Persisted session meters for sessions visible under ``repo_root``.

    Used by repo-scoped ``/api/swarm/live`` so session spend reflects that
    workspace's stamped chat/local-worker dollars without folding in the
    active pilot's process-global meters (which may belong to another repo).
    Store-job dollars stay out of these meters by design -- callers add them
    from the scoped job list separately.
    """
    root = (repo_root or "").strip()
    if not root:
        return {"est_cost_usd": 0.0, "tokens_used": 0}
    state_dir = ""
    try:
        state_dir = getattr(_cfg(), "state_dir", "") or ""
    except Exception:
        state_dir = ""
    cost = 0.0
    tokens = 0
    try:
        rows = _sessions().list(workspace_root=root, state_dir=state_dir)
    except Exception:
        rows = []
    for row in rows or []:
        cost += float(row.get("estimated_cost_usd") or 0.0)
        tokens += int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
    return {"est_cost_usd": round(cost, 6), "tokens_used": tokens}



def _tool_output_savings_fields(price_in: float, *, process_wide: bool = False) -> dict:
    """Compact tool-output savings for session payloads.

    When ``process_wide`` is True (boot /api/usage pill), aggregate across the
    whole state-dir ledger for this process epoch rather than the active
    harness_session_id -- so dir/session swaps do not zero the saved meter.
    Also folds Puppetmaster/CLI ``tool_output_savings.jsonl`` offloads from
    boot-repo state dirs (deduped by tool_call_id).
    """
    # Empty session_id => ledger summarize() aggregates all sessions.
    sid = "" if process_wide else (getattr(_pilot(), "harness_session_id", "") or "")
    cli_dirs: list[str] = []
    if process_wide:
        try:
            from ..cli_job_merge import resolve_cli_state_dir

            repos: set[str] = set(_BOOT_REPOS)
            active = getattr(_cfg(), "repo", "") or ""
            if active:
                repos.add(
                    os.path.abspath(active) if os.path.isdir(active) else active
                )
            seen: set[str] = set()
            for repo in repos:
                cli_dir = resolve_cli_state_dir(repo or "")
                if not cli_dir:
                    continue
                key = os.path.abspath(cli_dir)
                if key in seen:
                    continue
                seen.add(key)
                cli_dirs.append(cli_dir)
        except Exception:
            cli_dirs = []
    try:
        from ..tool_output_savings import session_savings_payload

        payload = session_savings_payload(
            _pilot().state_dir,
            sid,
            price_in,
            cli_state_dirs=cli_dirs or None,
        )
    except Exception:
        payload = {
            "tool_output_tokens_saved": 0,
            "tool_output_savings_usd": 0.0,
            "tool_output_compactions": 0,
        }
    try:
        from ..history_compaction_journal import history_compaction_payload

        # history_compaction_payload("") scopes to all sessions (falsy sid).
        payload.update(
            history_compaction_payload(
                _pilot().state_dir,
                sid if process_wide else (sid or "default"),
            )
        )
    except Exception:
        payload.setdefault("history_compactions", 0)
        payload.setdefault("history_tokens_saved", 0)
    try:
        from ..spill_registry import spill_usage_payload

        payload.update(
            spill_usage_payload(
                _pilot().state_dir,
                sid if process_wide else (sid or "default"),
            )
        )
    except Exception:
        payload.setdefault("spill_count", 0)
        payload.setdefault("spill_chars", 0)
    try:
        from ..eval_history import eval_history_payload

        # State-dir wide on purpose: worker runs record under job ids, not
        # the harness session id, so a session filter would hide them.
        payload.update(eval_history_payload(_pilot().state_dir))
    except Exception:
        payload.setdefault("evals_recorded", 0)
        payload.setdefault("evals_failed", 0)
    try:
        from ..memory_layers import latest_layer_snapshot

        payload["memory_layers"] = latest_layer_snapshot(
            _pilot().state_dir,
            getattr(_pilot(), "harness_session_id", "") or "default",
        )
    except Exception:
        payload.setdefault("memory_layers", {})
    try:
        from ..compaction_advisor import advice_payload

        budget = getattr(getattr(_pilot(), "config", None), "max_context_tokens", 96000)
        payload.update(
            advice_payload(
                _pilot().state_dir,
                getattr(_pilot(), "harness_session_id", "") or "default",
                budget,
            )
        )
    except Exception:
        pass
    return payload


def _job_savings_fields(job_id: str) -> dict:
    """Per-job tool-output savings, merging harness + PM/CLI JSONL ledgers."""
    try:
        from ..cli_job_merge import resolve_cli_state_dir
        from ..tool_output_savings import job_savings_payload

        try:
            from pmharness.registry import resolve_price

            price_in, _ = resolve_price(_cfg().driver)
        except Exception:
            price_in = 0.0
        cli_dir = resolve_cli_state_dir(getattr(_cfg(), "repo", "") or "")
        return job_savings_payload(
            _pilot().state_dir,
            job_id,
            cli_state_dir=cli_dir,
            price_in=price_in,
        )
    except Exception:
        return {
            "tool_output_tokens_saved": 0,
            "tool_output_savings_usd": 0.0,
            "tool_output_compactions": 0,
        }


def _job_cost(tokens_in: float, tokens_out: float, tokens_total: float,
              price_in: float, price_out: float) -> float:
    """Deterministic per-job cost. Uses the real in/out split when the job
    carries it; otherwise prices the single ``tokens`` total at ``price_out``
    (completion tokens dominate cost, matching the session fallback) rather than
    a naive 50/50 blend that mis-prices output-heavy jobs."""
    if tokens_in or tokens_out:
        return ((float(tokens_in) / 1.0e6) * price_in
                + (float(tokens_out) / 1.0e6) * price_out)
    return (float(tokens_total) / 1.0e6) * price_out


def _swarm_registry() -> list:
    """Load the model registry for per-job actual-cost pricing. Best-effort."""
    try:
        from puppetmaster.model_registry import default_registry_path, load_registry
        return load_registry(default_registry_path())
    except Exception:
        return []


def _routing_estimate_by_task(artifacts) -> dict:
    """FINAL router pre-flight estimate per task_id (escalation > fallback > router).

    Untasked ROUTING rows are omitted -- they have no worker row to attach to.
    """
    try:
        from puppetmaster.models import ArtifactType
    except Exception:
        return {}
    rank = {
        "router-escalation": 3,
        "router-fallback": 2,
        "router": 1,
    }
    best: dict = {}  # task_id -> (rank, cost)
    for artifact in artifacts or []:
        if getattr(artifact, "type", None) != ArtifactType.ROUTING:
            continue
        created_by = getattr(artifact, "created_by", "") or ""
        r = rank.get(created_by, 0)
        if r == 0:
            continue
        payload = getattr(artifact, "payload", None) or {}
        cost = float(
            payload.get("estimated_cost_usd") or payload.get("nominal_cost_usd") or 0.0
        )
        task_id = getattr(artifact, "task_id", None)
        if not task_id:
            continue
        prev = best.get(task_id)
        if prev is None or r > prev[0]:
            best[task_id] = (r, cost)
    return {tid: cost for tid, (_r, cost) in best.items()}


def _routing_estimate_cost(artifacts) -> float:
    """Sum FINAL router pre-flight estimates; interim fallback before usage lands.

    Prefer escalation / fallback estimates over the initial ``router`` pick so a
    plan-billed first choice ($0) does not wipe the real fallback estimate.
    """
    try:
        from puppetmaster.models import ArtifactType
    except Exception:
        return 0.0
    rank = {
        "router-escalation": 3,
        "router-fallback": 2,
        "router": 1,
    }
    best: dict = {}  # task_id -> (rank, cost)
    untasked_total = 0.0
    for artifact in artifacts:
        if artifact.type != ArtifactType.ROUTING:
            continue
        created_by = getattr(artifact, "created_by", "") or ""
        r = rank.get(created_by, 0)
        if r == 0:
            continue
        payload = artifact.payload or {}
        cost = float(
            payload.get("estimated_cost_usd") or payload.get("nominal_cost_usd") or 0.0
        )
        task_id = getattr(artifact, "task_id", None)
        if not task_id:
            untasked_total += cost
            continue
        prev = best.get(task_id)
        if prev is None or r > prev[0]:
            best[task_id] = (r, cost)
    return untasked_total + sum(cost for (_r, cost) in best.values())


def _live_price_unpriced_tasks(job_cost) -> float:
    """Price usage records the registry could not price against the live
    OpenRouter price map (public /models feed, disk-cached). Worker model ids
    like 'z-ai/glm-5.2' are OpenRouter slugs, so this usually resolves exactly;
    unmatched models contribute nothing. Best-effort, never raises."""
    total = 0.0
    for task in getattr(job_cost, "tasks", []):
        total += _live_price_task(task)
    return total


def _job_swarm_accounting(raw_arts, registry: list) -> tuple[int, float]:
    """Return (tokens, cost_usd) for a swarm job.

    Prefers measured/estimated usage priced against the registry
    (:func:`puppetmaster.cost.price_job`), topped up with live OpenRouter rates
    for models the registry does not know. Falls back to the router's
    pre-flight estimate only while nothing could be priced from usage.
    When no ROUTING artifact exists (provider-native workers), usage is read
    from VERIFICATION payloads instead.
    """
    from puppetmaster.usage import aggregate_token_usage
    from puppetmaster.cost import price_job

    arts_for_usage = _arts_for_swarm_usage(raw_arts)

    tokens = 0
    try:
        token_usage_dict = aggregate_token_usage(arts_for_usage)
        tokens = int(token_usage_dict.get("total_tokens", 0) or 0)
    except Exception:
        pass

    est_cost_usd = 0.0
    try:
        job_cost = price_job(arts_for_usage, registry)
        usage_cost = job_cost.total_marginal_cost_usd + _live_price_unpriced_tasks(job_cost)
        # Only trust the usage-priced total when something actually priced.
        # Usage can land with models neither source can price (cost 0.0);
        # treating that as authoritative made finished jobs snap from the
        # routing estimate back to $0. Keep the estimate instead.
        if usage_cost > 0:
            est_cost_usd = usage_cost
        else:
            est_cost_usd = _routing_estimate_cost(raw_arts)
    except Exception:
        est_cost_usd = _routing_estimate_cost(raw_arts)
    return tokens, round(est_cost_usd, 6)


def _arts_for_swarm_usage(raw_arts):
    """Artifacts used for usage pricing: VERIFICATION-only when no ROUTING."""
    arts_for_usage = list(raw_arts or [])
    try:
        from puppetmaster.models import ArtifactType
    except Exception:
        return arts_for_usage
    has_routing = any(
        getattr(a, "type", None) == ArtifactType.ROUTING for a in arts_for_usage
    )
    if not has_routing:
        verification = [
            a for a in arts_for_usage if getattr(a, "type", None) == ArtifactType.VERIFICATION
        ]
        if verification:
            return verification
    return arts_for_usage


def _live_price_task(task) -> float:
    """Price one unpriced TaskCost against the live OpenRouter map. Best-effort."""
    if getattr(task, "priced", False) or (
        not getattr(task, "tokens_in", 0) and not getattr(task, "tokens_out", 0)
    ):
        return 0.0
    try:
        from pmharness.registry import price
        price_in, price_out = price(task.model_id)
    except Exception:
        return 0.0
    if price_in is None or price_out is None:
        return 0.0
    return ((task.tokens_in / 1.0e6) * price_in
            + (task.tokens_out / 1.0e6) * price_out)


def _task_swarm_accounting(raw_arts, registry: list) -> dict:
    """Per-task ``{tokens, est_cost_usd}`` for /api/swarm/live worker rows.

    Prefers measured/estimated usage priced like :func:`_job_swarm_accounting`;
    falls back to the FINAL routing estimate per task while usage is absent or
    unpriceable. Keys are task_ids present on usage or ROUTING artifacts.
    """
    from puppetmaster.usage import select_usage_records
    from puppetmaster.cost import price_job

    by_task: dict = {}
    for task_id, cost in _routing_estimate_by_task(raw_arts).items():
        by_task[task_id] = {"tokens": 0, "est_cost_usd": round(float(cost or 0.0), 6)}

    arts_for_usage = _arts_for_swarm_usage(raw_arts)
    try:
        usage = select_usage_records(arts_for_usage)
        job_cost = price_job(arts_for_usage, registry)
        priced = {t.task_id: t for t in getattr(job_cost, "tasks", [])}
        for task_id, record in usage.items():
            if str(task_id).startswith("__untasked_"):
                continue
            tokens = int(record.get("tokens_in") or 0) + int(record.get("tokens_out") or 0)
            cost = 0.0
            tc = priced.get(task_id)
            if tc is not None:
                if tc.priced and tc.marginal_cost_usd > 0:
                    cost = float(tc.marginal_cost_usd)
                else:
                    cost = _live_price_task(tc)
            prev = by_task.get(task_id) or {"tokens": 0, "est_cost_usd": 0.0}
            by_task[task_id] = {
                "tokens": tokens,
                "est_cost_usd": (
                    round(cost, 6) if cost > 0 else float(prev.get("est_cost_usd") or 0.0)
                ),
            }
    except Exception:
        pass
    return by_task


_COST_OPTIMIZING_POLICIES = frozenset({"balanced", "cheap"})


def _routing_saved_usd(raw_arts) -> float:
    """Dollars saved by cost-optimizing router picks vs the snapshotted baseline.

    Only ``balanced`` / ``cheap`` policies count; ``quality`` / ``escalating`` are
    deliberate spend and contribute 0. Best-effort -- never raises.
    """
    try:
        from puppetmaster.models import ArtifactType
    except Exception:
        return 0.0
    seen_tasks: set = set()
    total = 0.0
    try:
        for artifact in raw_arts or []:
            if getattr(artifact, "type", None) != ArtifactType.ROUTING:
                continue
            if getattr(artifact, "created_by", "") != "router":
                continue
            task_id = getattr(artifact, "task_id", None)
            if task_id:
                if task_id in seen_tasks:
                    continue
                seen_tasks.add(task_id)
            payload = getattr(artifact, "payload", None) or {}
            if not isinstance(payload, dict):
                continue
            policy = str(payload.get("policy") or "")
            if policy not in _COST_OPTIMIZING_POLICIES:
                continue
            try:
                baseline = float(payload.get("baseline_cost_usd") or 0.0)
                estimated = float(payload.get("estimated_cost_usd") or 0.0)
            except (TypeError, ValueError):
                continue
            if baseline <= 0:
                continue
            total += max(0.0, baseline - estimated)
    except Exception:
        return 0.0
    return total


def _registry_input_per_mtok(model_id: str, registry: list) -> float:
    """Resolve a model's input $/MTok from the registry; 0 when unknown."""
    if not model_id:
        return 0.0
    for spec in registry or []:
        if getattr(spec, "id", None) == model_id:
            try:
                return float(getattr(spec, "input_per_mtok_usd", 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0
        if getattr(spec, "adapter_model_name", None) == model_id:
            try:
                return float(getattr(spec, "input_per_mtok_usd", 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _tokens_cached_swarm(raw_arts) -> int:
    """Sum ``tokens_cached`` across usage-bearing artifacts (one per task).

    Same task-dedupe as :func:`_cache_saved_usd_swarm` so the token count and
    USD figure stay aligned on /api/swarm/live job rows. Best-effort.
    """
    seen_tasks: set = set()
    total = 0
    try:
        for artifact in raw_arts or []:
            payload = getattr(artifact, "payload", None) or {}
            if not isinstance(payload, dict):
                continue
            if "tokens_in" not in payload and "tokens_out" not in payload:
                continue
            task_id = getattr(artifact, "task_id", None)
            if task_id:
                if task_id in seen_tasks:
                    continue
                seen_tasks.add(task_id)
            try:
                tokens_cached = int(payload.get("tokens_cached") or 0)
            except (TypeError, ValueError):
                continue
            if tokens_cached > 0:
                total += tokens_cached
    except Exception:
        return 0
    return total


def _cache_saved_usd_swarm(raw_arts, registry: list) -> float:
    """Store-job swarm prompt-cache savings for display (not spend).

    Per task with ``tokens_cached > 0`` and a known registry input price:
    tokens_cached/1e6 * input_per_mtok * (1 - CACHE_READ_MULTIPLIER).

    Always credits cache hits even when ``real_cost_usd`` is set — that field
    is the provider spend total (already cache-discounted); suppressing the
    savings *display* left the status bar at $0 for agentic workers.

    Store-job savings belong only here (``cache_saved_usd_swarm``). Harness-
    attributed worker cache hits already land in pilot ``_tokens_cached`` /
    ``cache_savings_usd``; do not fold store-job cache into those meters for
    this figure. Spend math is unchanged. Best-effort.
    """
    seen_tasks: set = set()
    total = 0.0
    try:
        for artifact in raw_arts or []:
            payload = getattr(artifact, "payload", None) or {}
            if not isinstance(payload, dict):
                continue
            if "tokens_in" not in payload and "tokens_out" not in payload:
                continue
            task_id = getattr(artifact, "task_id", None)
            if task_id:
                if task_id in seen_tasks:
                    continue
                seen_tasks.add(task_id)
            try:
                tokens_cached = int(payload.get("tokens_cached") or 0)
            except (TypeError, ValueError):
                continue
            if tokens_cached <= 0:
                continue
            model = (
                payload.get("model")
                or payload.get("model_id")
                or ""
            )
            price_in = _registry_input_per_mtok(str(model), registry)
            if price_in <= 0:
                continue
            total += (tokens_cached / 1.0e6) * price_in * (1.0 - CACHE_READ_MULTIPLIER)
    except Exception:
        return 0.0
    return total


def _sum_job_set_savings(job_ids, arts_getter, registry: list) -> tuple[float, float]:
    """Sum routing + swarm-cache savings over a job id set. Never raises."""
    routing = 0.0
    cache = 0.0
    routing_fn = _server_attr("_routing_saved_usd", _routing_saved_usd)
    cache_fn = _server_attr("_cache_saved_usd_swarm", _cache_saved_usd_swarm)
    for jid in job_ids or []:
        try:
            arts = arts_getter(jid)
        except Exception as e:
            _diag("server.usage_savings_arts", e, msg=f"job={jid}")
            continue
        try:
            routing += routing_fn(arts)
        except Exception as e:
            _diag("server.usage_routing_saved", e, msg=f"job={jid}")
        try:
            cache += cache_fn(arts, registry)
        except Exception as e:
            _diag("server.usage_cache_saved_swarm", e, msg=f"job={jid}")
    return routing, cache


def _resolve_active_prices() -> tuple:
    """Per-Mtok (price_in, price_out) for the active driver; safe defaults on failure."""
    try:
        from pmharness.registry import resolve_price
        price_in, price_out = resolve_price(_cfg().driver)
        return float(price_in), float(price_out)
    except Exception:
        return 0.5, 2.0


def _resolve_prices_for_runner(runner: Any) -> tuple:
    """Per-Mtok prices for a runner's bound driver (fallback: active / defaults).

    Idle swap may have already retargeted ``_cfg().driver`` before rebuild; price
    historical meters from the runner's frozen ``config.driver`` when present.
    """
    try:
        cfg = getattr(runner, "config", None)
        driver = getattr(cfg, "driver", None) if cfg is not None else None
        if driver:
            from pmharness.registry import resolve_price
            price_in, price_out = resolve_price(driver)
            return float(price_in), float(price_out)
    except Exception:
        pass
    resolve_active = _server_attr("_resolve_active_prices", _resolve_active_prices)
    return resolve_active()


def _fold_runner_meters_into_boot_carry(
    session_id: str,
    runner: Any,
    *,
    price_in: Optional[float] = None,
    price_out: Optional[float] = None,
) -> None:
    """Add a runner's meters into the process-lifetime carry.

    Snapshots priced USD at fold-time rates so later model swaps cannot reprice
    historical tokens. Zeros the runner's meters after folding so a lingering
    ``_pilot`` pointer cannot double-count with carry in ``_boot_usage_meters``.

    Optional ``price_in`` / ``price_out`` override active rates (idle model-swap
    freezes at the OLD pilot's prices even if ``_cfg().driver`` already changed).
    """
    global _BOOT_CARRY_COST_USD, _BOOT_PLAN_BILLING
    del session_id  # reserved for diagnostics; meters are process-scoped
    try:
        if price_in is None or price_out is None:
            resolve_prices = _server_attr(
                "_resolve_active_prices", _resolve_active_prices
            )
            resolved_in, resolved_out = resolve_prices()
            if price_in is None:
                price_in = resolved_in
            if price_out is None:
                price_out = resolved_out
        split_fn = _server_attr("_session_cost_split", _session_cost_split)
        _BOOT_CARRY_COST_USD = float(_BOOT_CARRY_COST_USD or 0.0) + float(
            split_fn(runner, float(price_in), float(price_out))
        )
    except Exception:
        pass
    if getattr(runner, "_plan_billing", False):
        _BOOT_PLAN_BILLING = True
    for attr in _BOOT_METER_ATTRS:
        try:
            add = float(getattr(runner, attr, 0) or 0)
            _BOOT_METER_CARRY[attr] = float(_BOOT_METER_CARRY.get(attr, 0.0) or 0.0) + add
            if attr in ("_worker_cost_usd", "_provider_cost_usd"):
                setattr(runner, attr, 0.0)
            else:
                setattr(runner, attr, 0)
        except Exception:
            pass
    try:
        _persist_boot_usage(fold_live=False)
    except Exception:
        pass


def _freeze_pilot_meters_into_boot_carry(runner: Any) -> None:
    """Idle rebuild/swap: snapshot live meters into carry at the runner's rates.

    Does not remove the runner from the registry -- callers replace the same
    view after freezing. Zeros folded meters so the replacement starts clean
    and ``_boot_session_cost`` cannot reprice history at the new model rate.
    """
    resolve_runner = _server_attr(
        "_resolve_prices_for_runner", _resolve_prices_for_runner
    )
    pin, pout = resolve_runner(runner)
    fold = _server_attr(
        "_fold_runner_meters_into_boot_carry", _fold_runner_meters_into_boot_carry
    )
    fold("", runner, price_in=pin, price_out=pout)


def _note_boot_repo(repo: str) -> None:
    """Record a workspace opened this process for boot-pill swarm aggregation."""
    path = (repo or "").strip()
    if path and os.path.isdir(path):
        _BOOT_REPOS.add(os.path.abspath(path))

def _boot_usage_meters() -> dict[str, float]:
    """Process-lifetime meters: carry + sum across all live runners.

    Includes the active ``_pilot`` when it is not already in the registry
    (early boot / tests). Dropped runners are zeroed after fold so a stale
    ``_pilot`` pointer cannot double-count with carry.
    """
    totals = {attr: float(_BOOT_METER_CARRY.get(attr, 0.0) or 0.0) for attr in _BOOT_METER_ATTRS}
    try:
        live = list(_runners().runners())
    except Exception:
        live = []
    seen = {id(r) for r in live}
    if _pilot() is not None and id(_pilot()) not in seen:
        live.append(_pilot())
    for runner in live:
        for attr in _BOOT_METER_ATTRS:
            try:
                totals[attr] = float(totals[attr]) + float(getattr(runner, attr, 0) or 0)
            except Exception:
                pass
    return totals


def _boot_session_cost(price_in: float, price_out: float) -> float:
    """Sum snapshotted carry USD + per-live-runner ``_session_cost_split``.

    Carry dollars are frozen at fold-time rates (see ``_BOOT_CARRY_COST_USD``).
    Live runners still price at the active rate. Legacy carry with token meters
    but no snapshotted USD (pre-upgrade / tests) falls back to pricing carry
    tokens at the supplied rate.
    """
    from types import SimpleNamespace

    carry_cost = float(_BOOT_CARRY_COST_USD or 0.0)
    if carry_cost == 0.0:
        # Legacy / test path: meters stuffed into carry without a fold snapshot.
        has_carry = any(
            float(_BOOT_METER_CARRY.get(attr, 0.0) or 0.0) != 0.0
            for attr in _BOOT_METER_ATTRS
        )
        if has_carry:
            carry_pilot = SimpleNamespace(**{
                attr: _BOOT_METER_CARRY.get(attr, 0.0) for attr in _BOOT_METER_ATTRS
            })
            carry_cost = float(_session_cost_split(carry_pilot, price_in, price_out))
    total = carry_cost
    try:
        live = list(_runners().runners())
    except Exception:
        live = []
    seen = {id(r) for r in live}
    if _pilot() is not None and id(_pilot()) not in seen:
        live.append(_pilot())
    for runner in live:
        try:
            total += float(_session_cost_split(runner, price_in, price_out))
        except Exception:
            pass
    return total


def _scoped_jobs_with_stores(repo_root: str | None = None) -> tuple[list, Any, Any | None]:
    """Visible jobs plus harness and optional CLI stores for bulk reads."""
    from ..cli_job_merge import merge_scoped_cli_jobs
    from ..job_scoping import filter_store_jobs

    jobs = _jobs_snapshot()
    try:
        store = _session().state().store
    except Exception:
        return jobs, None, None
    effective_repo = (repo_root or "").strip() or (_cfg().repo or "")
    workspace_root = effective_repo or os.getcwd()
    active_session_id = _sessions().active or getattr(_pilot(), "harness_session_id", "") or ""
    visible = filter_store_jobs(
        jobs,
        store,
        active_session_id=active_session_id,
        repo_root=effective_repo,
    )
    try:
        merged, cli_store = merge_scoped_cli_jobs(
            visible,
            harness_store=store,
            active_session_id=active_session_id,
            repo_root=effective_repo,
            workspace_root=workspace_root,
        )
        return merged, store, cli_store
    except Exception as e:
        _diag("server.scoped_jobs_cli_merge", e)
        return [{**j, "source": j.get("source", "harness")} for j in visible], store, None


def _scoped_jobs_snapshot(repo_root: str | None = None) -> list:
    """Jobs visible for the active harness session and open workspace.

    When ``repo_root`` is present and non-empty it overrides ``_cfg().repo`` for
    legacy (unstamped) cwd filtering; stamped session jobs are unchanged.
    Merges read-only CLI jobs from the Puppetmaster per-project store when
    present (tagged ``source``: ``harness`` or ``cli``).
    """
    jobs, _, _ = _scoped_jobs_with_stores(repo_root)
    return jobs
