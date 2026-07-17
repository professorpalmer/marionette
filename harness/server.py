from __future__ import annotations

"""Harness web server: a local, zero-dependency-beyond-stdlib HTTP server that
serves the three-pane GUI and streams Session events over SSE. Cursor 3.0 /
Hermes style: left nav, center driver-loop conversation, right durable-state.

stdlib http.server only -- no FastAPI/uvicorn needed, keeps the harness
dependency-light and launchable anywhere.
"""

import json
import os
import time
import threading
import queue
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets as _secrets
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs
import tempfile
import uuid

from dataclasses import replace as _dc_replace

from .config import HarnessConfig
from .session import Session
from .conversation import ConversationalSession
from .mcp_manager import McpManager
from .skill_store import SkillStore
from .rule_store import RuleStore
from .command_store import CommandStore
from .memory_store import MemoryStore, MEMORY_CHAR_LIMIT
from . import workspaces as _ws
from .sessions import (
    SessionStore,
    save_transcript,
    load_transcript,
    session_stored_root,
    session_visible_for_workspace,
)
from .session_runners import (
    SessionRunnerRegistry,
    LeaseExhaustedError,
    build_lease_exhausted_payload,
)
from .deferred_attach import is_deferred_placeholder
# Re-export for tests that patch harness.server.AutoBudget (stream_auto uses
# AutoBudget.from_env via harness.api.streams).
from .autobudget import AutoBudget  # noqa: F401
from ._exec import _puppetmaster_python, _puppetmaster_available, _puppetmaster_cmd, _ensure_node_on_path
from .diag import note as _diag
from .secure_files import restrict_dir_to_owner, restrict_to_owner
# SSE ring + pump/write live in harness.api.sse; stream bodies in
# harness.api.streams. Re-export historical names so Handler methods and
# tests keep importing harness.server.
from .api.sse import (
    _SSE_RING_CAP,
    _SSE_RING_TTL,
    _SSE_RING_MAX_SESSIONS,
    SseEventRing,
    _sse_ring_generation,
    _sse_rings,
    _sse_rings_lock,
    _sse_ring_begin,
    _sse_ring_lookup,
    _sse_ring_current_generation,
    _sse_ring_clear_for_tests,
    sse_pump,
    sse_write,
)
from .api.streams import CHECKPOINT_KINDS as _CHECKPOINT_KINDS


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
        live = list(_runners.runners())
    except Exception:
        live = []
    if _pilot is not None and id(_pilot) not in {id(r) for r in live}:
        live.append(_pilot)
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
# epoch-windowed jobs across these repos, not only the active _cfg.repo.
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
    root = (getattr(_cfg, "state_dir", None) or "").strip() or os.path.join(
        os.path.expanduser("~"), ".pmharness", "state"
    )
    return os.path.join(root, "boot_usage.json")


def _fold_all_live_runners_into_boot_carry() -> None:
    """Collapse every live runner into carry so a backend restart can persist one blob."""
    try:
        live = list(_runners.runners())
    except Exception:
        live = []
    seen = {id(r) for r in live}
    try:
        pilot = _pilot
    except NameError:
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
                    price_in, price_out = _resolve_active_prices()
                    cost_snap = float(_boot_session_cost(price_in, price_out))
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
    sid = _sessions.active or ""
    if not sid:
        return None
    row = next((s for s in _sessions.list() if s.get("id") == sid), None)
    if row is None:
        return None
    swarm_cost = 0.0
    for jid in session_job_ids:
        try:
            _tokens, cost = _job_swarm_accounting(arts_getter(jid), registry)
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
        state_dir = getattr(_cfg, "state_dir", "") or ""
    except Exception:
        state_dir = ""
    cost = 0.0
    tokens = 0
    try:
        rows = _sessions.list(workspace_root=root, state_dir=state_dir)
    except Exception:
        rows = []
    for row in rows or []:
        cost += float(row.get("estimated_cost_usd") or 0.0)
        tokens += int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
    return {"est_cost_usd": round(cost, 6), "tokens_used": tokens}


def _sync_pilot_session_id() -> None:
    """Keep the pilot's savings-ledger session scope aligned with SessionStore."""
    try:
        _pilot.harness_session_id = _sessions.active or ""
    except Exception:
        pass


def _tool_output_savings_fields(price_in: float, *, process_wide: bool = False) -> dict:
    """Compact tool-output savings for session payloads.

    When ``process_wide`` is True (boot /api/usage pill), aggregate across the
    whole state-dir ledger for this process epoch rather than the active
    harness_session_id -- so dir/session swaps do not zero the saved meter.
    Also folds Puppetmaster/CLI ``tool_output_savings.jsonl`` offloads from
    boot-repo state dirs (deduped by tool_call_id).
    """
    # Empty session_id => ledger summarize() aggregates all sessions.
    sid = "" if process_wide else (getattr(_pilot, "harness_session_id", "") or "")
    cli_dirs: list[str] = []
    if process_wide:
        try:
            from .cli_job_merge import resolve_cli_state_dir

            repos: set[str] = set(_BOOT_REPOS)
            active = getattr(_cfg, "repo", "") or ""
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
        from .tool_output_savings import session_savings_payload

        payload = session_savings_payload(
            _pilot.state_dir,
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
        from .history_compaction_journal import history_compaction_payload

        # history_compaction_payload("") scopes to all sessions (falsy sid).
        payload.update(
            history_compaction_payload(
                _pilot.state_dir,
                sid if process_wide else (sid or "default"),
            )
        )
    except Exception:
        payload.setdefault("history_compactions", 0)
        payload.setdefault("history_tokens_saved", 0)
    try:
        from .spill_registry import spill_usage_payload

        payload.update(
            spill_usage_payload(
                _pilot.state_dir,
                sid if process_wide else (sid or "default"),
            )
        )
    except Exception:
        payload.setdefault("spill_count", 0)
        payload.setdefault("spill_chars", 0)
    try:
        from .eval_history import eval_history_payload

        # State-dir wide on purpose: worker runs record under job ids, not
        # the harness session id, so a session filter would hide them.
        payload.update(eval_history_payload(_pilot.state_dir))
    except Exception:
        payload.setdefault("evals_recorded", 0)
        payload.setdefault("evals_failed", 0)
    try:
        from .memory_layers import latest_layer_snapshot

        payload["memory_layers"] = latest_layer_snapshot(
            _pilot.state_dir,
            getattr(_pilot, "harness_session_id", "") or "default",
        )
    except Exception:
        payload.setdefault("memory_layers", {})
    try:
        from .compaction_advisor import advice_payload

        budget = getattr(getattr(_pilot, "config", None), "max_context_tokens", 96000)
        payload.update(
            advice_payload(
                _pilot.state_dir,
                getattr(_pilot, "harness_session_id", "") or "default",
                budget,
            )
        )
    except Exception:
        pass
    return payload


def _job_savings_fields(job_id: str) -> dict:
    """Per-job tool-output savings, merging harness + PM/CLI JSONL ledgers."""
    try:
        from .cli_job_merge import resolve_cli_state_dir
        from .tool_output_savings import job_savings_payload

        try:
            from pmharness.registry import resolve_price

            price_in, _ = resolve_price(_cfg.driver)
        except Exception:
            price_in = 0.0
        cli_dir = resolve_cli_state_dir(getattr(_cfg, "repo", "") or "")
        return job_savings_payload(
            _pilot.state_dir,
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


def _job_status_is_terminal(status: str) -> bool:
    """Finished swarm rows: complete / fail / cancel / stall (not in-flight)."""
    s = (status or "").lower()
    if not s:
        return False
    if any(tok in s for tok in ("run", "progress", "active", "pending", "queued", "dispatch")):
        return False
    return any(
        tok in s
        for tok in ("complete", "done", "fail", "cancel", "error", "stall")
    )


def _slim_swarm_list_artifacts(raw_arts, state_obj) -> list:
    """Keep only what live/finished cards need: ROUTING + verdict rows.

    Full FINDING/RISK/DECISION streams are fetched on expand via /api/artifacts.
    Applied to both in-progress and terminal jobs on /api/swarm/live so polls
    stay cheap while a swarm is still running.
    """
    try:
        from puppetmaster.models import ArtifactType
    except Exception:
        return state_obj.format_artifacts(raw_arts) if hasattr(state_obj, "format_artifacts") else []

    keep = []
    for art in raw_arts or []:
        atype = getattr(art, "type", None)
        if atype == ArtifactType.ROUTING:
            keep.append(art)
            continue
        if atype == ArtifactType.VERIFICATION:
            payload = getattr(art, "payload", None) or {}
            if payload.get("result") or payload.get("failure"):
                keep.append(art)
    try:
        return state_obj.format_artifacts(keep) if hasattr(state_obj, "format_artifacts") else []
    except Exception:
        return []


def _job_dead_run_failure(raw_arts, status: str):
    """Mirror SwarmPane dead-run detection against raw store artifacts.

    Computed server-side before the live payload is slimmed -- otherwise a
    finished job that still has FINDING rows would look like an all-failed
    dead run once those findings are stripped from the poll response.

    Returns the failure class string, or None when the job is not a dead run.
    """
    s = (status or "").lower()
    if "complete" not in s and "done" not in s:
        return None
    if not raw_arts:
        return None
    failed = []
    for art in raw_arts:
        payload = getattr(art, "payload", None)
        if not isinstance(payload, dict):
            # Already-formatted dict rows (local jobs) carry result on the art.
            if isinstance(art, dict):
                payload = art
            else:
                return None
        result = str(payload.get("result") or "").lower()
        if result in ("failed", "blocked"):
            failed.append(payload)
        else:
            return None
    if not failed:
        return None
    for payload in failed:
        failure = payload.get("failure")
        if failure:
            return str(failure)
    return "workers failed"


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
    for jid in job_ids or []:
        try:
            arts = arts_getter(jid)
        except Exception as e:
            _diag("server.usage_savings_arts", e, msg=f"job={jid}")
            continue
        try:
            routing += _routing_saved_usd(arts)
        except Exception as e:
            _diag("server.usage_routing_saved", e, msg=f"job={jid}")
        try:
            cache += _cache_saved_usd_swarm(arts, registry)
        except Exception as e:
            _diag("server.usage_cache_saved_swarm", e, msg=f"job={jid}")
    return routing, cache


def _get_platform_json_path() -> str:
    override = os.environ.get("TEST_PLATFORM_JSON_PATH")
    if override:
        return override
    return os.path.expanduser("~/.puppetmaster/platform.json")


def _write_platform_json_atomic(path: str, data: dict) -> None:
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_path or ".", prefix="platform_")
    try:
        with os.fdopen(tmp_fd, 'w', encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _init_platform_lock() -> None:
    path = _get_platform_json_path()
    pdata = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                pdata = json.load(f)
        except Exception as e:
            _diag("server.platform_lock_read", e)
    if not isinstance(pdata, dict):
        pdata = {}
    
    # A file with a well-formed "disabled" list is a configured install even
    # without our marker: Puppetmaster's CLI historically rewrote platform.json
    # with only its own keys, stripping "harness_initialized" -- re-applying
    # standalone defaults here would silently disable adapters the operator
    # just enabled (`puppetmaster platform enable cursor` undone on every
    # Marionette boot). Only seed defaults when the file is truly absent or
    # carries no adapter configuration at all.
    already_configured = isinstance(pdata.get("disabled"), list)
    if not already_configured and (
        not os.path.exists(path) or "harness_initialized" not in pdata
    ):
        # Standalone default: out of the box only the built-in ``agentic`` adapter
        # is enabled. It runs its own tool-use loop directly against whatever
        # provider API the user has a key for (Anthropic, OpenAI, Gemini,
        # OpenRouter, ...), so a fresh install needs NOTHING but a provider key --
        # no external agent CLI (cursor / claude / codex / hermes) installed or
        # logged in. Every CLI adapter is left OFF so Marionette stays fully
        # self-contained and vendor-neutral; any of them can still be re-enabled
        # in Settings > Platform for users who have that tooling.
        default_disabled = ["cursor", "claude-code", "codex", "openai", "hermes"]
        if "disabled" not in pdata or not isinstance(pdata["disabled"], list):
            pdata["disabled"] = default_disabled
        else:
            # Legacy platform.json missing the init marker: fold in the standalone
            # defaults (so every CLI adapter lands off) while guaranteeing the
            # built-in agentic adapter stays on.
            merged = set(pdata["disabled"]) | set(default_disabled)
            merged.discard("agentic")
            pdata["disabled"] = sorted(merged)
        pdata["harness_initialized"] = True
        try:
            _write_platform_json_atomic(path, pdata)
        except Exception as e:
            _diag("server.platform_lock_write", e)


def _seed_agentic_catalog() -> None:
    """Seed the standalone 'agentic' models into the Puppetmaster registry.

    auto_route can only pick a standalone model if one is in
    ``~/.puppetmaster/models.json``. This merges the curated agentic catalog
    (API-billed) filtered to the providers the user actually has a key for, so a
    fresh install with, say, only an Anthropic key gets exactly the Anthropic
    agentic models and nothing that would 401. Idempotent (refresh-or-add) and
    never fatal -- a swarm must never fail to start over catalog seeding.
    """
    try:
        from pathlib import Path as _Path
        from puppetmaster.model_registry import load_registry, save_registry, default_registry_path
        from puppetmaster.static_catalog import merge_curated_into_registry
        from puppetmaster.providers import available_providers

        env_path = os.environ.get("PUPPETMASTER_MODELS_PATH")
        registry_path = _Path(env_path) if env_path else default_registry_path()
        existing = load_registry(registry_path)
        merged, _report = merge_curated_into_registry(
            "agentic", "api", existing, allowed_providers=available_providers()
        )
        save_registry(merged, registry_path)
    except Exception as e:
        _diag("server.seed_agentic_catalog", e)


def _get_platform_adapters() -> dict:
    import shutil
    from .keys import get_api_key_status
    path = _get_platform_json_path()
    disabled_list = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                pdata = json.load(f)
                if isinstance(pdata, dict) and "disabled" in pdata and isinstance(pdata["disabled"], list):
                    disabled_list = pdata["disabled"]
        except Exception as e:
            _diag("server.platform_disabled_read", e)

    adapters_config = [
        {"name": "agentic", "implement_capable": True},
        {"name": "cursor", "implement_capable": True},
        {"name": "hermes", "implement_capable": True},
        {"name": "claude-code", "implement_capable": True},
        {"name": "codex", "implement_capable": True},
        {"name": "openai", "implement_capable": False}
    ]

    adapters = []
    for cfg in adapters_config:
        name = cfg["name"]
        enabled = name not in disabled_list
        
        # Best-effort availability
        if name == "agentic":
            try:
                from puppetmaster.providers import available_providers
                ready = sorted(available_providers())
            except Exception:
                ready = []
            available = bool(ready)
            note = (
                "Standalone (default). Runs directly on your provider keys -- no "
                "external CLI. "
                + (f"Ready: {', '.join(ready)}." if ready else "Add a provider key to enable.")
            )
        elif name == "hermes":
            available = ("OPENROUTER_API_KEY" in os.environ) or get_api_key_status("openrouter")["has_key"]
            note = "Hermes via OpenRouter. Uses standard API key."
        elif name == "openai":
            available = ("OPENAI_API_KEY" in os.environ) or get_api_key_status("openai")["has_key"]
            note = "OpenAI API adapter. Note: Analysis-only, cannot drive implement tasks."
        elif name == "cursor":
            available = shutil.which("cursor") is not None
            note = "Cursor editor CLI. Run swarm/implement in a Cursor workspace."
        elif name == "claude-code":
            available = shutil.which("claude") is not None
            note = "Anthropic Claude Code. Requires 'claude' npm command in path."
        elif name == "codex":
            available = shutil.which("codex") is not None
            note = "Codex agent CLI. Requires 'codex' command in path."
        else:
            available = True
            note = ""

        adapters.append({
            "name": name,
            "enabled": enabled,
            "implement_capable": cfg["implement_capable"],
            "available": available,
            "note": note
        })
    return {"adapters": adapters}


_WEB = Path(__file__).resolve().parent / "web"
# One shared session per server process (single-user local app).
_state_dir = os.environ.get("HARNESS_STATE_DIR", "")
_cfg = HarnessConfig.from_env()
def _pmharness_root() -> str:
    """Install root (~/.pmharness). models.json and caches stay here; durable
    session files live under ``state/`` once HARNESS_STATE_DIR is anchored."""
    return os.path.expanduser("~/.pmharness")


def _state_home() -> str:
    """Base dir for app state files (workspace.json, token, drivers, markers).

    Honors HARNESS_STATE_DIR so the test suite -- which sets it to an isolated
    temp dir per test (tests/conftest.py::_isolate_provider_state) -- can NEVER
    read or write the developer's real ~/.pmharness. These paths used to be
    frozen to real home at import time, so importing harness.server during tests
    leaked live state: a dead pytest temp repo in workspace.json and, worse, a
    rewritten auth token. A respawned backend then held a token the renderer no
    longer knew, every request 403'd, and it read as "the backend died."

    When HARNESS_STATE_DIR is unset, prefer ``~/.pmharness/state`` if that dir
    already exists (where live Saves write after the stable-state anchor). Fall
    back to the legacy flat ``~/.pmharness`` root so older installs still restore
    workspace_drivers.json / workspace.json written before the state/ split.
    Matches Electron ``readPmHarnessStateFile`` (state first, then legacy).
    """
    explicit = os.environ.get("HARNESS_STATE_DIR")
    if explicit:
        return explicit
    root = _pmharness_root()
    durable = os.path.join(root, "state")
    if os.path.isdir(durable):
        return durable
    return root


def _home_workspace_path() -> str:
    """Durable default workspace for chats with no Open Folder.

    Production: ``~/.pmharness/home``. Under ``HARNESS_STATE_DIR`` (tests /
    isolated runs): ``{state_dir}/home`` so we never touch the real home tree.
    This path is a real user project root -- not ephemeral -- and must remain
    boot-restorable via ``_record_recent_workspace``.
    """
    explicit = os.environ.get("HARNESS_STATE_DIR")
    if explicit:
        return os.path.join(explicit, "home")
    return os.path.join(_pmharness_root(), "home")


def _is_home_workspace(path: str) -> bool:
    """True when ``path`` is the durable Home workspace (slash/case-insensitive)."""
    if not path:
        return False
    try:
        return _paths_same_workspace(path, _home_workspace_path())
    except Exception:
        return False


def _ensure_home_workspace() -> str:
    """Create the Home workspace on demand, seed a minimal AGENTS.md, record it.

    Returns the absolute home path. Never raises for normal filesystem errors
    beyond returning the intended path; callers may still use it as a bind root.
    """
    home = os.path.abspath(_home_workspace_path())
    try:
        os.makedirs(home, exist_ok=True)
    except Exception as e:
        _diag("server.home_workspace_mkdir", e)
    agents = os.path.join(home, "AGENTS.md")
    try:
        if not os.path.isfile(agents):
            with open(agents, "w", encoding="utf-8", newline="\n") as f:
                f.write(
                    "# Home workspace\n\n"
                    "Default Marionette workspace for chats started without "
                    "Open Folder. Prefer moving durable project work into a "
                    "real repository via relocate_session / Open Folder.\n"
                )
    except Exception as e:
        _diag("server.home_workspace_seed", e)
    try:
        _record_recent_workspace(home, as_active=False)
    except Exception as e:
        _diag("server.home_workspace_record", e)
    return home


def _env_settings_path() -> str:
    return os.path.join(_state_home(), "env_settings.json")


# Env-backed settings that must survive a backend restart. The Settings page
# stores these in os.environ (cheap live-reload: readers check the env each
# turn), but env vars die with the process -- so every relaunch silently reset
# command guard / timeouts / step caps to defaults while the UI claimed they
# were saved. Every write goes through _persist_env_setting and startup
# replays the file with setdefault so an explicit shell/env override still wins.
def _persist_env_setting(env_var: str, value: str) -> None:
    path = _env_settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        data[env_var] = value
        from .registry_wizard import write_json_atomic
        write_json_atomic(path, data)
    except Exception as e:
        _diag("server.persist_env_setting", e)


def _load_env_settings() -> None:
    path = _env_settings_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(k, str) and k.startswith("HARNESS_") and isinstance(v, str):
                    os.environ.setdefault(k, v)
    except Exception as e:
        _diag("server.load_env_settings", e)


def _resolve_existing_state_file(name: str) -> str:
    """Prefer the current write path for ``name``; fall back to ``~/.pmharness/<name>``.

    After the stable HARNESS_STATE_DIR anchor (~/.pmharness/state), saves land
    under state/ while older installs still have workspace.json /
    workspace_drivers.json at the legacy root. Only fall back when state home
    IS that stable anchor — never when tests point HARNESS_STATE_DIR at an
    isolated temp dir (that would leak the developer's real drivers into tests).

    Known names route through ``_workspace_json_path`` /
    ``_workspace_drivers_path`` so test monkeypatches of those helpers still
    cover both reads and writes.
    """
    if name == "workspace.json":
        primary = _workspace_json_path()
    elif name == "workspace_drivers.json":
        primary = _workspace_drivers_path()
    else:
        primary = os.path.join(_state_home(), name)
    if os.path.exists(primary):
        return primary
    legacy_root = _pmharness_root()
    if os.path.realpath(_state_home()) == os.path.realpath(os.path.join(legacy_root, "state")):
        legacy = os.path.join(legacy_root, name)
        if os.path.exists(legacy):
            return legacy
    return primary


def _workspace_json_path() -> str:
    """Write path for workspace.json (always under current state home)."""
    return os.path.join(_state_home(), "workspace.json")


def _workspace_drivers_path() -> str:
    """Write path for workspace_drivers.json (always under current state home)."""
    return os.path.join(_state_home(), "workspace_drivers.json")


# Global fallback key in workspace_drivers.json: the last driver the user chose
# anywhere. Restored on boot when the active workspace has no saved entry (or no
# workspace is open at all), so a settings-page model choice always sticks.
_LAST_DRIVER_KEY = "__last__"


def _save_workspace_driver(repo: str, driver: str) -> None:
    """Remember which model the user last used in a given workspace, so opening
    that dir later restores it (use opus-4-8 in repo A, gpt-5.5 in repo B, and
    each comes back correctly on switch)."""
    if not driver:
        return
    import tempfile as _tf
    # Never persist ephemeral temp dirs (test state leaks otherwise).
    if repo and os.path.realpath(repo).startswith(os.path.realpath(_tf.gettempdir())):
        return
    path = _workspace_drivers_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {}
        # Seed from the readable copy (state/ or legacy) so a first save after
        # the state-dir move does not drop other workspaces' remembered drivers.
        read_path = _resolve_existing_state_file("workspace_drivers.json")
        if os.path.exists(read_path):
            try:
                with open(read_path, encoding="utf-8", errors="replace") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        if repo:
            data[os.path.realpath(repo)] = driver
        data[_LAST_DRIVER_KEY] = driver
        from .registry_wizard import write_json_atomic
        write_json_atomic(path, data)
    except Exception as e:
        _diag("server.workspace_driver_write", e)


def _get_workspace_driver(repo: str):
    """The model last used in this workspace, falling back to the last driver
    chosen anywhere (so a fresh/unknown workspace still boots on the user's
    pick, not the compiled-in default). None if nothing was ever saved."""
    path = _resolve_existing_state_file("workspace_drivers.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        if repo:
            saved = data.get(os.path.realpath(repo))
            if saved:
                return saved
        return data.get(_LAST_DRIVER_KEY)
    except Exception:
        return None

def _norm_realpath(path: str) -> str:
    """Canonical form for path comparisons: resolve + normcase.

    On Windows the same directory surfaces with mixed drive-letter / component
    casing (env-var spelling, 8.3 short names), so raw path strings are not
    comparable with ``==``. Uses ``paths._resolve`` instead of bare
    ``os.path.realpath`` -- the latter can hang indefinitely on Windows when
    the path no longer exists (moved/deleted recents like a relocated Ashita
    tree). Mirrors ``_norm_path`` in job_scoping/sessions.
    """
    from .paths import _resolve
    return os.path.normcase(_resolve(path))


def _paths_same_workspace(a: str, b: str) -> bool:
    """True when two workspace roots refer to the same directory."""
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        return _norm_realpath(a) == _norm_realpath(b)
    except Exception:
        # Fall back to slash/case fold when resolve fails.
        na = os.path.normcase(a.replace("/", os.sep).replace("\\", os.sep)).rstrip(os.sep)
        nb = os.path.normcase(b.replace("/", os.sep).replace("\\", os.sep)).rstrip(os.sep)
        return na == nb


def _app_install_roots() -> list:
    """Paths that are the Marionette app itself, not user projects.

    The packaged checkout (~/.marionette/marionette), the live source root
    Electron passes as MARIONETTE_APP_ROOT / MARIONETTE_CHECKOUT, and the
    checkout that is actually running this process (derived from
    ``harness.__file__``) must never auto-appear as the open workspace or in
    PROJECTS recents -- users only see them if they open that folder manually
    for the current session. Entries are ``_norm_realpath`` canonical so
    comparisons stay case-insensitive on Windows.
    """
    roots = []
    for key in (
        "MARIONETTE_APP_ROOT",
        "HARNESS_APP_ROOT",
        "MARIONETTE_CHECKOUT",
        "HARNESS_CHECKOUT",
    ):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            try:
                roots.append(_norm_realpath(raw))
            except OSError:
                pass
    packaged = os.path.join(os.path.expanduser("~"), ".marionette", "marionette")
    try:
        if os.path.isdir(packaged):
            roots.append(_norm_realpath(packaged))
    except OSError:
        pass
    # Whatever checkout is executing this backend -- catches both the
    # packaged ~/.marionette/marionette tree and a developer
    # Projects/marionette checkout when that is what Electron spawned.
    try:
        import harness as _harness_pkg
        _pkg_dir = os.path.dirname(os.path.abspath(_harness_pkg.__file__))
        _running = os.path.dirname(_pkg_dir)
        if _running:
            roots.append(_norm_realpath(_running))
    except Exception:
        pass
    # Dedupe while preserving order.
    out = []
    seen = set()
    for r in roots:
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _is_app_install_root(path: str) -> bool:
    """True when path is the Marionette app checkout (not a user project)."""
    if not path:
        return False
    try:
        rp = _norm_realpath(path)
    except OSError:
        return False
    return rp in set(_app_install_roots())


def _pick_boot_workspace(ws_data: dict) -> str:
    """Choose the workspace to restore on launch.

    Prefer the persisted ``repo`` key, then recents, skipping the app install
    root and vanished dirs. Empty string = open nothing (first-launch / scrubbed).
    """
    if not isinstance(ws_data, dict):
        return ""
    candidates = []
    repo = (ws_data.get("repo") or "").strip()
    if repo:
        candidates.append(repo)
    for r in (ws_data.get("recents") or []):
        r = (r or "").strip()
        if r and r not in candidates:
            candidates.append(r)
    for c in candidates:
        try:
            if c and os.path.isdir(c) and not _is_app_install_root(c):
                return c
        except OSError:
            continue
    return ""


def _record_recent_workspace(target_repo: str, *, as_active: bool = True) -> list:
    import json
    import os
    import tempfile as _tf
    ws_json_path = _workspace_json_path()
    ws_read_path = _resolve_existing_state_file("workspace.json")
    try:
        os.makedirs(os.path.dirname(ws_json_path), exist_ok=True)
        recents = []
        prior_repo = ""
        if os.path.exists(ws_read_path):
            try:
                with open(ws_read_path, encoding="utf-8", errors="replace") as f:
                    _ws_data = json.load(f)
                    recents = _ws_data.get("recents", []) or []
                    prior_repo = _ws_data.get("repo", "") or ""
            except Exception:
                recents = []
        # never persist temp dirs (test/ephemeral state_dirs leak otherwise)
        # and never persist the Marionette app checkout as a user project.
        from .paths import _resolve
        _tmproot = os.path.normcase(_resolve(_tf.gettempdir()))
        def _persistable(_pth):
            if not _pth:
                return False
            try:
                _rp = os.path.normcase(_resolve(_pth))
            except Exception:
                return False
            if "PYTEST_CURRENT_TEST" not in os.environ:
                if _rp.startswith(_tmproot) or "/var/folders/" in _rp or "/T/tmp" in _pth:
                    return False
            if _is_app_install_root(_pth):
                return False
            return os.path.isdir(_pth)
        # Stable order: if path already in recents (any slash/case spelling),
        # leave its position; if new, append. Do NOT prepend-to-front on every
        # open (that snapped the rail). Still persist active "repo" below for
        # boot restore. Cap 8 + ephemeral guards unchanged. App install root is
        # never added (manual open stays process-local only).
        already = any(_paths_same_workspace(target_repo, r) for r in recents)
        if target_repo and not already and _persistable(target_repo):
            recents = list(recents) + [target_repo]
        # Collapse slash/case duplicate spellings of the same root.
        deduped = []
        for r in recents:
            if not _persistable(r):
                continue
            if any(_paths_same_workspace(r, kept) for kept in deduped):
                continue
            deduped.append(r)
        recents = deduped[:8]

        # The "repo" key is what boot restores as the active workspace, so a
        # temp dir / app checkout here resurrects as a phantom project on next
        # launch. Keep the prior persisted repo when the new target is not a
        # user project. as_active=False (Home seed) only appends to recents.
        if as_active and _persistable(target_repo):
            persisted_repo = target_repo
        elif prior_repo and _persistable(prior_repo):
            persisted_repo = prior_repo
        elif as_active:
            persisted_repo = ""
        else:
            persisted_repo = prior_repo if _persistable(prior_repo) else ""

        # Use atomic-write
        target_dir = os.path.dirname(ws_json_path)
        fd, temp_path = _tf.mkstemp(dir=target_dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
                json.dump({"repo": persisted_repo, "recents": recents}, f)
            os.replace(temp_path, ws_json_path)
        except Exception as e:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            raise e

        if not restrict_to_owner(ws_json_path):
            _diag("secure_files.restrict_failed", msg=ws_json_path)
        return recents
    except Exception:
        # Fallback to get recents if possible
        try:
            if os.path.exists(ws_json_path):
                with open(ws_json_path, encoding="utf-8", errors="replace") as f:
                    return json.load(f).get("recents", []) or []
        except Exception:
            pass
        return []

def _forget_recent_workspace(forget_path: str) -> list:
    import json
    import os
    import tempfile as _tf
    ws_json_path = _workspace_json_path()
    ws_read_path = _resolve_existing_state_file("workspace.json")
    try:
        os.makedirs(os.path.dirname(ws_json_path), exist_ok=True)
        recents = []
        repo = ""
        if os.path.exists(ws_read_path):
            try:
                with open(ws_read_path, encoding="utf-8", errors="replace") as f:
                    data = json.load(f)
                    recents = data.get("recents", []) or []
                    repo = data.get("repo", "")
            except Exception:
                recents = []
        # never persist temp dirs (test/ephemeral state_dirs leak otherwise)
        from .paths import _resolve
        _tmproot = os.path.normcase(_resolve(_tf.gettempdir()))
        def _persistable(_pth):
            if not _pth:
                return False
            try:
                _rp = os.path.normcase(_resolve(_pth))
            except Exception:
                return False
            if "PYTEST_CURRENT_TEST" not in os.environ:
                if _rp.startswith(_tmproot) or "/var/folders/" in _rp or "/T/tmp" in _pth:
                    return False
            if _is_app_install_root(_pth):
                return False
            return os.path.isdir(_pth)

        # Drop every slash/case spelling of forget_path (exact == left siblings).
        recents = [r for r in recents if not _paths_same_workspace(r, forget_path)]
        recents = [r for r in recents if _persistable(r)]
        recents = recents[:8]

        # Forgetting the active workspace must clear the boot-restore repo key,
        # otherwise buildProjectsList re-appends currentRepo and the row sticks
        # as a phantom after Remove from list.
        if repo and _paths_same_workspace(repo, forget_path):
            repo = ""

        # Use atomic-write
        target_dir = os.path.dirname(ws_json_path)
        fd, temp_path = _tf.mkstemp(dir=target_dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
                json.dump({"repo": repo, "recents": recents}, f)
            os.replace(temp_path, ws_json_path)
        except Exception as e:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            raise e

        if not restrict_to_owner(ws_json_path):
            _diag("secure_files.restrict_failed", msg=ws_json_path)
        return recents
    except Exception:
        # Fallback to get recents if possible
        try:
            if os.path.exists(ws_json_path):
                with open(ws_json_path, encoding="utf-8", errors="replace") as f:
                    return json.load(f).get("recents", []) or []
        except Exception:
            pass
        return []

# DURABLE STATE must be anchored BEFORE workspace.json / workspace_drivers
# restore. Saves write under ~/.pmharness/state/; if we restore first,
# _state_home() still points at ~/.pmharness and boot misses the saved
# driver — falling through to enabled_pilots()[0] (often glm-5.2).
# Always join under _pmharness_root() — never _state_home()/state, which
# would nest state/state once _state_home already prefers the durable dir.
if _state_dir:
    _cfg.state_dir = _state_dir
elif not _cfg.state_dir:
    # With no explicit HARNESS_STATE_DIR and no config value, state_dir was
    # left blank -- so the session and pilot each fell back to their OWN
    # throwaway mkdtemp(), landing swarm history / transcripts / job stores
    # in a fresh temp dir every launch that nothing ever reads again. Anchor
    # to a stable per-install dir so history survives close/reopen.
    _stable = os.path.join(_pmharness_root(), "state")
    try:
        os.makedirs(_stable, exist_ok=True)
        _cfg.state_dir = _stable
        os.environ.setdefault("HARNESS_STATE_DIR", _stable)
    except Exception as e:
        _diag("server.stable_state_dir", e)

# Same Electron app-run: restore boot spend/savings after a backend respawn.
try:
    _restore_boot_usage()
except Exception as e:
    _diag("server.boot_usage_restore_boot", e)

def _scrub_leaked_app_root_harness_repo() -> None:
    """Drop HARNESS_REPO when it points at the Marionette app checkout itself.

    Packaged Electron may inherit a process-level HARNESS_REPO equal to the
    app install root; that must not skip workspace.json boot restore. Direct
    CLI use without MARIONETTE_APP_ROOT is intentional even when the path is
    this running checkout (dev working in the Marionette repo).
    """
    repo = (_cfg.repo or os.environ.get("HARNESS_REPO") or "").strip()
    if not repo or not _is_app_install_root(repo):
        return
    app_root_env = (os.environ.get("MARIONETTE_APP_ROOT") or "").strip()
    if app_root_env:
        try:
            if _norm_realpath(repo) == _norm_realpath(app_root_env):
                _cfg.repo = ""
                os.environ.pop("HARNESS_REPO", None)
                return
        except OSError:
            pass
    packaged = os.path.join(os.path.expanduser("~"), ".marionette", "marionette")
    try:
        if os.path.isdir(packaged) and _norm_realpath(repo) == _norm_realpath(packaged):
            _cfg.repo = ""
            os.environ.pop("HARNESS_REPO", None)
    except OSError:
        pass


try:
    _scrub_leaked_app_root_harness_repo()
except Exception as e:
    _diag("server.scrub_leaked_app_root", e)

_ws_boot_path = _resolve_existing_state_file("workspace.json")
if not os.environ.get("HARNESS_REPO") and os.path.exists(_ws_boot_path):
    try:
        with open(_ws_boot_path, "r", encoding="utf-8", errors="replace") as _ws_f:
            _ws_data = json.load(_ws_f)
        if not isinstance(_ws_data, dict):
            _ws_data = {}
        # Prefer last user project; never boot into the Marionette app
        # checkout even if an older build wrote it into workspace.json.
        _boot_repo = _pick_boot_workspace(_ws_data)
        if _boot_repo:
            _cfg.repo = _boot_repo
            os.environ["HARNESS_REPO"] = _boot_repo
        # Scrub app-install paths left in recents by older builds so the
        # PROJECTS rail does not keep surfacing Marionette itself. Close the
        # read handle first -- Windows cannot replace an open file.
        try:
            _scrubbed = [
                r for r in (_ws_data.get("recents") or [])
                if r and os.path.isdir(r) and not _is_app_install_root(r)
            ]
            _prior = (_ws_data.get("repo") or "").strip()
            _persist_repo = _boot_repo or (
                _prior if _prior and not _is_app_install_root(_prior) and os.path.isdir(_prior) else ""
            )
            if _scrubbed != list(_ws_data.get("recents") or []) or _persist_repo != _prior:
                from .registry_wizard import write_json_atomic as _ws_atomic
                _ws_atomic(_workspace_json_path(), {"repo": _persist_repo, "recents": _scrubbed[:8]})
        except Exception as _scrub_e:
            _diag("server.workspace_boot_scrub", _scrub_e)
    except Exception as e:
        _diag("server.workspace_boot_load", e)

# Restore the model last used in the adopted workspace (parity with
# /api/workspace/open). Without this, the saved driver was only read on an
# explicit workspace switch, so every app relaunch silently reset the pilot
# to the compiled-in default even though the picker said the choice was saved.
if "HARNESS_DRIVER" not in os.environ:
    try:
        _boot_saved_driver = _get_workspace_driver(_cfg.repo)
        if _boot_saved_driver and _boot_saved_driver != _cfg.driver:
            _cfg.driver = _boot_saved_driver
            if "HARNESS_MAX_CONTEXT_TOKENS" not in os.environ:
                try:
                    from pmharness.registry import context_window as _boot_ctx_window
                    _cfg.max_context_tokens = _boot_ctx_window(_cfg.driver, default=200000)
                except Exception as e:
                    _diag("server.boot_driver_context_window", e)
    except Exception as e:
        _diag("server.boot_restore_workspace_driver", e)

# Replay persisted Settings-page values into the environment BEFORE the pilot
# is constructed (it snapshots several of these at build time). setdefault
# semantics: an explicit env var set by the host/shell always wins.
_load_env_settings()

# Masker-safe live key: if HARNESS_KEY_FILE points at a file, load it into the
# expected env var for the chosen reach before the Session builds its driver.
from .keys import load_api_keys_on_startup, get_api_key_status, get_env_var_for_reach, set_api_key, clear_api_key
from .keys import (
    get_bedrock_status,
    set_bedrock_credentials,
    clear_bedrock_credentials,
)
from .wiki_config import load_wiki_config_on_startup
from .wiki_backend import ensure_wiki_backend_async
load_api_keys_on_startup(_cfg.reach)
# The Electron host spawns the backend with a stripped PATH; make Node visible so
# CodeGraph (a Node CLI) works out of the box instead of reporting "unsupported".
_ensure_node_on_path()
load_wiki_config_on_startup()
# Boot a local wiki backend only when wiki.json / env already points at loopback.
# Fresh installs stay unconfigured so the UI can guide users to portablellm.wiki.
# Opt out: MARIONETTE_NO_WIKI=1.
ensure_wiki_backend_async()


def _driver_provider_available(spec: str) -> bool:
    """True if the provider backing a driver spec currently has a usable key.
    A bare name (e.g. 'qwen3-coder-30b') routes through the reach provider
    (OpenRouter); a 'provider:model' spec is backed by that provider."""
    from . import providers as _prov
    if not spec:
        return False
    # Stub/offline drivers (stub-oracle-v2, etc.) run deterministically with no
    # provider key, so they are always usable and must never be swapped out by
    # startup driver resolution. Mirrors doctor.py's spec.startswith("stub").
    if spec.startswith("stub"):
        return True
    if ":" in spec:
        prov_name = spec.split(":", 1)[0]
        p = _prov.get_provider(prov_name)
        return bool(p and p.available)
    # Bare catalog name -> uses the reach provider (default openrouter).
    p = _prov.get_provider(_cfg.reach)
    return bool(p and p.available)


def _driver_in_enabled_set(driver: str, enabled: list) -> bool:
    """True if a driver spec matches any enabled picker spec. Handles the
    spelling variants: an enabled spec is 'provider:model' while the driver may
    be the same spec, the bare model id, or a bare catalog name whose provider
    slug differs (e.g. 'qwen3-coder-30b' vs
    'openrouter:qwen/qwen3-coder-30b-a3b-instruct')."""
    if not driver:
        return False
    aliases = {driver}
    try:
        from pmharness.registry import load_catalog
        for m in load_catalog().get("models", []):
            if m.get("name") == driver and m.get("openrouter"):
                aliases.add(m["openrouter"])
    except Exception:
        pass
    for spec in enabled:
        model = spec.split(":", 1)[1] if ":" in spec else spec
        if spec in aliases or model in aliases:
            return True
    return False


def _resolve_available_driver():
    """Make sure the active driver is one the user can actually use: its
    provider must have a key AND, when the user has curated an enabled picker
    set, the driver must be in that set. Otherwise fall back to the first
    available enabled model -- so a fresh boot never lands on the compiled-in
    default (qwen3-coder-30b) when the user disabled it, and never lands on a
    dead provider."""
    global _cfg
    try:
        if not _driver_provider_available(_cfg.driver):
            driver_ok = False
        elif _cfg.driver.startswith("stub") or "HARNESS_DRIVER" in os.environ:
            # Stub/offline drivers and an explicit env override are deliberate
            # choices -- never second-guess them against the picker curation.
            driver_ok = True
        else:
            from . import model_visibility as _mv
            enabled = _mv.get_enabled()
            driver_ok = not enabled or _driver_in_enabled_set(_cfg.driver, enabled)
        if driver_ok:
            return
        from . import model_visibility as _mv
        # Pick the first available pilot (enabled set, key-filtered).
        # enabled_pilots() is ordered by provider then catalog — first toggled
        # model on the first keyed provider wins when the compiled-in default
        # is not in the curated set.
        candidates = _mv.enabled_pilots()
        for spec in candidates:
            if _driver_provider_available(spec):
                _cfg.driver = spec
                # Recompute the context window inline (the _apply_model_context_window
                # helper is defined later in this module; avoid a forward reference).
                if "HARNESS_MAX_CONTEXT_TOKENS" not in os.environ:
                    try:
                        from pmharness.registry import context_window
                        _cfg.max_context_tokens = context_window(_cfg.driver, default=200000)
                    except Exception as e:
                        _diag("server.resolve_driver_context_window", e)
                return
    except Exception as e:
        _diag("server.resolve_available_driver", e)


def _resync_driver_after_model_curation() -> dict:
    """After Models toggles, keep the active pilot inside the enabled set.

    Returns {driver, changed} so the Settings UI / picker can refresh. Persists
    the new driver like /api/pilot/swap so a relaunch does not snap back to
    the compiled-in qwen default.
    """
    prev = _cfg.driver
    _resolve_available_driver()
    changed = _cfg.driver != prev
    if changed:
        try:
            _rebuild_pilot_and_session()
        except Exception as e:
            # Busy mid-turn: leave the resolved _cfg.driver for the next
            # rebuild; still report the intended driver so the picker label
            # matches what will run after the turn.
            _diag("server.model_curation_driver_rebuild", e)
        try:
            _save_workspace_driver(_cfg.repo, _cfg.driver)
        except Exception as e:
            _diag("server.model_curation_driver_persist", e)
    return {"driver": _cfg.driver, "changed": changed}


_resolve_available_driver()
# Tracker Session may share the global view config; each ConversationalSession
# runner gets its OWN HarnessConfig copy so mutating _cfg.repo (workspace open /
# cross-repo switch) cannot retarget a busy turn's tools/cwd.
_session = Session(_cfg)
_pilot = ConversationalSession(_dc_replace(_cfg))
# Session and pilot each fall back to their OWN mkdtemp() when config.state_dir
# is blank (the default), landing run_swarm's job store (pilot's state_dir) and
# the tracker's read store (session's state_dir) in two DIFFERENT temp dirs. The
# Swarm Tracker (/api/swarm/live) and Session Jobs (/api/jobs) read the session
# store, so they stayed empty even after a real swarm ran in the pilot store.
# Pin the session to the pilot's store so both read exactly where jobs are written.
_session.state_dir = _pilot.state_dir
import tempfile as _tf
_sessions = SessionStore(os.path.join(_cfg.state_dir or _tf.gettempdir(), "harness_sessions.json"))
# Per-session runners: active VIEW is which session the UI attaches to; other
# sessions may keep executing under the concurrent-session lease. on_drop is
# wired below once _fold_runner_meters_into_boot_carry is defined.
_runners = SessionRunnerRegistry()
_mcp = McpManager()
# Serialize pilot rebinds so a /api/pilot swap and a workspace-switch rebuild
# cannot interleave their history-copy/rebind steps and leave a torn _pilot.
_pilot_swap_lock = threading.Lock()
# One-shot resume latch for self-edit backend restarts. Set ONLY by
# /api/session/persist or /api/restart (the explicit restart path); never by a
# trailing user turn alone. Survives process respawn via a state-dir flag file
# so the fresh process can report resume_pending exactly once.
_resume_latch = False
from .pty_manager import PtyManager
_pty = PtyManager()
_pilot._mcp = _mcp
_pilot._session_store = _sessions
_init_platform_lock()
_seed_agentic_catalog()
# Seed boot-pill swarm aggregation with the workspace adopted at process start.
if _cfg.repo and os.path.isdir(_cfg.repo):
    _BOOT_REPOS.add(os.path.abspath(_cfg.repo))


def _resume_latch_path() -> str:
    return os.path.join(_cfg.state_dir or _tf.gettempdir(), ".resume_latch")


def _set_resume_latch() -> None:
    """Arm the one-shot auto-resume signal for the next process / state poll."""
    global _resume_latch
    _resume_latch = True
    try:
        p = _resume_latch_path()
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write("1\n")
        restrict_to_owner(p)
    except Exception as e:
        _diag("server.resume_latch_set", e)


def _clear_resume_latch() -> None:
    """Consume the latch (in-memory + on disk) so a later view cannot re-fire."""
    global _resume_latch
    _resume_latch = False
    try:
        p = _resume_latch_path()
        if os.path.exists(p):
            os.unlink(p)
    except Exception as e:
        _diag("server.resume_latch_clear", e)


def _load_resume_latch() -> None:
    """Adopt a latch left by a prior process (self-edit restart continuity)."""
    global _resume_latch
    try:
        p = _resume_latch_path()
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                _resume_latch = f.read().strip() == "1"
            if not _resume_latch:
                _clear_resume_latch()
    except Exception as e:
        _diag("server.resume_latch_load", e)
        _resume_latch = False


def _consume_resume_pending(idle: bool) -> bool:
    """True once when the latch is armed and the pilot is idle; then clear it."""
    global _resume_latch
    if not (_resume_latch and idle):
        return False
    _clear_resume_latch()
    return True


def _copy_pilot_meters(old_pilot: Any, new_pilot: Any) -> None:
    """Copy cost meters onto a replacement runner (legacy / explicit opt-in).

    Idle model swap and same-view rebuild no longer use this -- they freeze
    meters into ``_BOOT_METER_CARRY`` / ``_BOOT_CARRY_COST_USD`` at the old
    rates instead, so historical ``est_cost_usd`` cannot jump when the new
    model is cheaper or dearer. Prefer ``_freeze_pilot_meters_into_boot_carry``.
    """
    for attr in _BOOT_METER_ATTRS:
        try:
            setattr(new_pilot, attr, getattr(old_pilot, attr, getattr(new_pilot, attr, 0)))
        except Exception:
            pass


def _resolve_active_prices() -> tuple:
    """Per-Mtok (price_in, price_out) for the active driver; safe defaults on failure."""
    try:
        from pmharness.registry import resolve_price
        price_in, price_out = resolve_price(_cfg.driver)
        return float(price_in), float(price_out)
    except Exception:
        return 0.5, 2.0


def _resolve_prices_for_runner(runner: Any) -> tuple:
    """Per-Mtok prices for a runner's bound driver (fallback: active / defaults).

    Idle swap may have already retargeted ``_cfg.driver`` before rebuild; price
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
    return _resolve_active_prices()


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
    freezes at the OLD pilot's prices even if ``_cfg.driver`` already changed).
    """
    global _BOOT_CARRY_COST_USD, _BOOT_PLAN_BILLING
    del session_id  # reserved for diagnostics; meters are process-scoped
    try:
        if price_in is None or price_out is None:
            resolved_in, resolved_out = _resolve_active_prices()
            if price_in is None:
                price_in = resolved_in
            if price_out is None:
                price_out = resolved_out
        _BOOT_CARRY_COST_USD = float(_BOOT_CARRY_COST_USD or 0.0) + float(
            _session_cost_split(runner, float(price_in), float(price_out))
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
    pin, pout = _resolve_prices_for_runner(runner)
    _fold_runner_meters_into_boot_carry("", runner, price_in=pin, price_out=pout)


# Wire after definition so boot construction above can create the registry first.
_runners._on_drop = _fold_runner_meters_into_boot_carry


def _note_boot_repo(repo: str) -> None:
    """Record a workspace opened this process for boot-pill swarm aggregation."""
    path = (repo or "").strip()
    if path and os.path.isdir(path):
        _BOOT_REPOS.add(os.path.abspath(path))


def _runner_config_snapshot() -> HarnessConfig:
    """Per-runner HarnessConfig copy so mutating ``_cfg.repo`` cannot retarget a busy turn."""
    return _dc_replace(_cfg)


def _boot_usage_meters() -> dict[str, float]:
    """Process-lifetime meters: carry + sum across all live runners.

    Includes the active ``_pilot`` when it is not already in the registry
    (early boot / tests). Dropped runners are zeroed after fold so a stale
    ``_pilot`` pointer cannot double-count with carry.
    """
    totals = {attr: float(_BOOT_METER_CARRY.get(attr, 0.0) or 0.0) for attr in _BOOT_METER_ATTRS}
    try:
        live = list(_runners.runners())
    except Exception:
        live = []
    seen = {id(r) for r in live}
    if _pilot is not None and id(_pilot) not in seen:
        live.append(_pilot)
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
        live = list(_runners.runners())
    except Exception:
        live = []
    seen = {id(r) for r in live}
    if _pilot is not None and id(_pilot) not in seen:
        live.append(_pilot)
    for runner in live:
        try:
            total += float(_session_cost_split(runner, price_in, price_out))
        except Exception:
            pass
    return total


def _bind_pilot_services(pilot: Any) -> None:
    """Attach shared MCP / session-store handles to a runner."""
    pilot._mcp = _mcp
    pilot._session_store = _sessions
    pilot._on_wiki_ingest = _clear_wiki_graph_cache


def _build_conversational_pilot(*, copy_meters_from: Any = None) -> ConversationalSession:
    """Construct a ConversationalSession with a frozen per-runner config copy.

    New runners start at zero meters -- idle rebuild/swap freezes spend into
    boot carry instead of copying token counters. ``copy_meters_from`` may
    still opt into legacy meter copy + auto-distill continuity; attach/create
    must omit it.
    """
    new_pilot = ConversationalSession(_runner_config_snapshot())
    _bind_pilot_services(new_pilot)
    if copy_meters_from is not None:
        _copy_pilot_meters(copy_meters_from, new_pilot)
        try:
            new_pilot._auto_distill = getattr(
                copy_meters_from, "_auto_distill", getattr(new_pilot, "_auto_distill", False)
            )
        except Exception:
            pass
    return new_pilot


def _active_pilot() -> Any:
    """Return the runner for the current active view (compat: same as ``_pilot``)."""
    return _pilot


def _lease_exhausted_body(exc: Optional[BaseException] = None) -> dict:
    """Build the shared lease_exhausted 409 JSON from the live registry.

    Titles come from SessionStore when cheap (unscoped list); missing titles
    are omitted rather than blocking the response.
    """
    titles_by_id: dict[str, str] = {}
    try:
        for row in _sessions.list():
            sid = str(row.get("id") or "")
            title = str(row.get("title") or "").strip()
            if sid and title:
                titles_by_id[sid] = title
    except Exception as e:
        _diag("server.lease_exhausted_titles", e)
    return build_lease_exhausted_payload(
        _runners,
        error=str(exc) if exc else None,
        titles_by_id=titles_by_id or None,
    )


def _attach_view(
    session_id: str,
    *,
    factory=None,
    load_transcript_on_create: bool = True,
    defer_cold_build: Optional[bool] = None,
) -> Any:
    """Point the UI at ``session_id`` via the runner registry.

    Body lives in ``harness.api.attach.attach_view``; this wrapper injects
    live module globals so tests can keep patching ``harness.server``.
    """
    from .api.attach import attach_view
    return attach_view(
        session_id,
        _attach_services(),
        factory=factory,
        load_transcript_on_create=load_transcript_on_create,
        defer_cold_build=defer_cold_build,
    )


def _ensure_active_pilot_ready(*, timeout: float = 120.0) -> Any:
    """Block until the active view's deferred cold build finishes (if any)."""
    from .api.attach import ensure_active_pilot_ready
    return ensure_active_pilot_ready(_attach_services(), timeout=timeout)


def _gate_active_pilot_ready(*, timeout: float = 120.0) -> Optional[dict]:
    """Ensure the active pilot is a real ConversationalSession."""
    from .api.attach import gate_active_pilot_ready
    return gate_active_pilot_ready(_attach_services(), timeout=timeout)


def _attach_view_transcript_payload(runner: Any, session_id: str) -> dict[str, list]:
    """Transcript for attach/switch responses (live runner, else disk)."""
    from .api.attach import attach_view_transcript_payload
    return attach_view_transcript_payload(runner, session_id, _attach_services())


def _save_active_transcript() -> None:
    """Persist the current active view's transcript (if any)."""
    if _sessions.active:
        save_transcript(
            _sessions_state_dir(),
            _sessions.active,
            _pilot.export_transcript_data(),
        )


_load_resume_latch()


def _reap_stale_swarms_on_boot() -> None:
    """Sweep dead-but-'running' jobs to 'stalled' in every store the tracker
    reads (harness store + the per-project CLI store). Pre-update zombies --
    jobs whose orchestrator died with the old process -- otherwise show as
    running forever and can't be cancelled, since there is nothing left to
    cancel."""
    try:
        from puppetmaster.liveness import reap_stalled_jobs
    except Exception as e:
        _diag("server.boot_reaper_import", e)
        return
    stores = []
    try:
        stores.append(_session.state().store)
    except Exception as e:
        _diag("server.boot_reaper_harness_store", e)
    try:
        from .cli_job_merge import open_cli_durable_state
        cli_state = open_cli_durable_state(_cfg.repo or "")
        if cli_state is not None:
            stores.append(cli_state.store)
    except Exception as e:
        _diag("server.boot_reaper_cli_store", e)
    for store in stores:
        try:
            reaped = reap_stalled_jobs(store)
            if reaped:
                _diag(
                    "server.boot_reaper",
                    msg=f"stalled {len(reaped)} zombie job(s): "
                        f"{[r['job_id'] for r in reaped]}",
                )
        except Exception as e:
            _diag("server.boot_reaper_sweep", e)


threading.Thread(target=_reap_stale_swarms_on_boot, daemon=True).start()


def _sessions_state_dir() -> str:
    return _cfg.state_dir or _tf.gettempdir()


_CODEGRAPH_REASON_UNSET = object()


def _set_codegraph_status(
    status: str,
    reason: Any = _CODEGRAPH_REASON_UNSET,
) -> None:
    """Mutate codegraph status globals (injected into SessionServices).

    Passing only ``status`` leaves ``_codegraph_status_reason`` untouched
    (matches prior inline assignments for ready/unsupported). Passing an
    explicit ``reason`` (including ``None``) updates both.
    """
    global _codegraph_status, _codegraph_status_reason
    _codegraph_status = status
    if reason is not _CODEGRAPH_REASON_UNSET:
        _codegraph_status_reason = reason


def _session_services():
    """Build SessionServices from live server module globals (call-time lookup)."""
    from .api.sessions import SessionServices
    return SessionServices(
        sessions=_sessions,
        runners=_runners,
        cfg=_cfg,
        get_pilot=lambda: _pilot,
        sessions_state_dir=_sessions_state_dir,
        save_active_transcript=_save_active_transcript,
        attach_view=_attach_view,
        sync_pilot_session_id=_sync_pilot_session_id,
        diag=_diag,
        is_app_install_root=_is_app_install_root,
        ensure_home_workspace=_ensure_home_workspace,
        note_boot_repo=_note_boot_repo,
        record_recent_workspace=_record_recent_workspace,
        puppetmaster_available=_puppetmaster_available,
        index_codegraph_bg=_index_codegraph_bg,
        maybe_refresh_codegraph=_maybe_refresh_codegraph,
        get_codegraph_status=_get_codegraph_status,
        lease_exhausted_body=_lease_exhausted_body,
        attach_view_transcript_payload=_attach_view_transcript_payload,
        parse_bool=_parse_bool,
        set_codegraph_status=_set_codegraph_status,
    )


def _stream_services():
    """Build StreamServices from live server module globals (call-time lookup)."""
    from .api.streams import StreamServices
    return StreamServices(
        cfg=_cfg,
        sessions=_sessions,
        get_pilot=lambda: _pilot,
        get_session=lambda: _session,
        ensure_pilot_matches_driver=_ensure_pilot_matches_driver,
        maybe_refresh_codegraph=_maybe_refresh_codegraph,
        pilot_preflight=_pilot_preflight,
        checkpoint_transcript=_checkpoint_transcript,
        finalize_turn=_finalize_turn,
        upload_dir=_UPLOAD_DIR,
        auto_budget_from_env=lambda: AutoBudget.from_env(),
    )


def _job_services():
    """Build JobServices from live server module globals (call-time lookup)."""
    from .api.jobs import JobServices
    return JobServices(
        cfg=_cfg,
        sessions=_sessions,
        get_pilot=lambda: _pilot,
        get_session=lambda: _session,
        diag=_diag,
        scoped_jobs_snapshot=_scoped_jobs_snapshot,
        scoped_jobs_with_stores=_scoped_jobs_with_stores,
        retry_on_locked=_retry_on_locked,
        swarm_registry=_swarm_registry,
        job_status_is_terminal=_job_status_is_terminal,
        slim_swarm_list_artifacts=_slim_swarm_list_artifacts,
        job_swarm_accounting=_job_swarm_accounting,
        task_swarm_accounting=_task_swarm_accounting,
        routing_saved_usd=_routing_saved_usd,
        cache_saved_usd_swarm=_cache_saved_usd_swarm,
        tokens_cached_swarm=_tokens_cached_swarm,
        job_dead_run_failure=_job_dead_run_failure,
        job_savings_fields=_job_savings_fields,
        repo_session_stamped_meters=_repo_session_stamped_meters,
        session_cost_split=_session_cost_split,
        cache_savings=_cache_savings,
        tool_output_savings_fields=_tool_output_savings_fields,
        cost_source_label=_cost_source_label,
    )


def _wiki_services():
    """Build WikiServices from live server module globals (call-time lookup)."""
    from .api.wiki import WikiServices
    return WikiServices(
        cfg=_cfg,
        get_pilot=lambda: _pilot,
    )


def _mcp_services():
    """Build McpServices from live server module globals (call-time lookup)."""
    from .api.mcp import McpServices
    return McpServices(mcp=_mcp)


def _skills_services():
    """Build SkillsServices from live server module globals (call-time lookup)."""
    from .api.skills import SkillsServices
    return SkillsServices(
        skills=_skills,
        rules=_rules,
        memory=_memory,
        get_pilot=lambda: _pilot,
        memory_char_limit=MEMORY_CHAR_LIMIT,
    )


def _worktree_services():
    """Build WorktreeServices from live server module globals (call-time lookup)."""
    from .api.worktrees import WorktreeServices
    return WorktreeServices(cfg=_cfg, parse_bool=_parse_bool)


def _terminal_services():
    """Build TerminalServices from live server module globals (call-time lookup)."""
    from .api.terminals import TerminalServices
    return TerminalServices(cfg=_cfg, pty=_pty)


def _sse_services():
    """Build SseServices from live server module globals (call-time lookup)."""
    from .api.sse import SseServices
    return SseServices(
        ring_lookup=_sse_ring_lookup,
        current_generation=_sse_ring_current_generation,
        default_session_id=lambda: (
            _sessions.active or getattr(_pilot, "harness_session_id", "") or ""
        ),
    )


def _pilot_services():
    """Build PilotServices from live server module globals (call-time lookup)."""
    from .api.pilot import PilotServices
    return PilotServices(
        cfg=_cfg,
        get_pilot=lambda: _pilot,
        apply_model_context_window=_apply_model_context_window,
        save_workspace_driver=_save_workspace_driver,
        perform_pilot_swap=_perform_pilot_swap,
    )


def _commands_services():
    """Build CommandsServices from live server module globals (call-time lookup)."""
    from .api.commands import CommandsServices
    return CommandsServices(commands=_commands, cfg=_cfg)


def _hooks_services():
    """Build HooksServices from live server module globals (call-time lookup)."""
    from .api.hooks import HooksServices
    return HooksServices(parse_bool=_parse_bool)


def _checkpoint_services():
    """Build CheckpointServices from live server module globals (call-time lookup)."""
    from .api.checkpoints import CheckpointServices
    return CheckpointServices(
        cfg=_cfg,
        get_active_session_id=lambda: _sessions.active or "",
    )


def _git_services():
    """Build GitServices from live server module globals (call-time lookup)."""
    from .api.git import GitServices
    return GitServices(cfg=_cfg)


def _review_services():
    """Build ReviewServices from live server module globals (call-time lookup)."""
    from .api.reviews import ReviewServices
    return ReviewServices(
        cfg=_cfg,
        get_pilot=lambda: _pilot,
        resolve_editor_path=_resolve_editor_path,
        strip_markdown_fences=_strip_markdown_fences,
    )


def _registry_services():
    """Build RegistryServices from live server module globals (call-time lookup)."""
    from .api.registry import RegistryServices
    return RegistryServices(diag=_diag)


def _platform_services():
    """Build PlatformServices from live server module globals (call-time lookup)."""
    from .api.platform import PlatformServices
    return PlatformServices(
        get_platform_json_path=_get_platform_json_path,
        write_platform_json_atomic=_write_platform_json_atomic,
        get_platform_adapters=_get_platform_adapters,
        diag=_diag,
    )


def _codegraph_services():
    """Build CodegraphServices from live server module globals (call-time lookup)."""
    from .api.codegraph import CodegraphServices
    import time as _time

    def _set_reason(reason: str) -> None:
        global _codegraph_status_reason
        _codegraph_status_reason = reason

    def _set_live_status(status: str) -> None:
        global _codegraph_status
        _codegraph_status = status

    def _status_cache_put(repo: str, payload) -> None:
        _codegraph_status_cache[repo] = (
            _time.monotonic() + _CODEGRAPH_STATUS_TTL, payload)

    return CodegraphServices(
        cfg=_cfg,
        index_alive=_codegraph_index_alive,
        reindex_bg=_reindex_codegraph_bg,
        index_bg=_index_codegraph_bg,
        get_status=_get_codegraph_status,
        get_reason=lambda: _codegraph_status_reason,
        set_reason=_set_reason,
        get_live_status=lambda: _codegraph_status,
        set_live_status=_set_live_status,
        get_preflight=lambda: _codegraph_preflight,
        get_suggested_action=lambda: _codegraph_suggested_action,
        puppetmaster_available=_puppetmaster_available,
        codegraph_indexed=_codegraph_indexed,
        status_cache_get=lambda repo: _codegraph_status_cache.get(repo),
        status_cache_put=_status_cache_put,
        status_cache_pop=lambda repo: _codegraph_status_cache.pop(repo, None),
        fail_until_for=lambda repo: float(_codegraph_fail_until.get(repo) or 0),
        puppetmaster_cmd=_puppetmaster_cmd,
        status_ttl=_CODEGRAPH_STATUS_TTL,
    )


def _workspace_services():
    """Build WorkspaceServices from live server module globals (call-time lookup)."""
    from .api.workspace import WorkspaceServices

    _UNSET = object()

    def _clear_active_codegraph() -> None:
        global _codegraph_status, _codegraph_status_reason
        _codegraph_status = "none"
        _codegraph_status_reason = None

    def _set_codegraph_status(status: str, reason=_UNSET) -> None:
        global _codegraph_status, _codegraph_status_reason
        _codegraph_status = status
        if reason is not _UNSET:
            _codegraph_status_reason = reason

    return WorkspaceServices(
        cfg=_cfg,
        parse_bool=_parse_bool,
        ws=_ws,
        paths_same_workspace=_paths_same_workspace,
        forget_recent_workspace=_forget_recent_workspace,
        clear_active_codegraph=_clear_active_codegraph,
        get_codegraph_status=_get_codegraph_status,
        workspace_json_path=_workspace_json_path,
        ensure_home_workspace=_ensure_home_workspace,
        home_workspace_path=_home_workspace_path,
        is_app_install_root=_is_app_install_root,
        diag=_diag,
        sessions=_sessions,
        save_active_transcript=_save_active_transcript,
        note_boot_repo=_note_boot_repo,
        get_workspace_driver=_get_workspace_driver,
        apply_model_context_window=_apply_model_context_window,
        record_recent_workspace=_record_recent_workspace,
        sessions_state_dir=_sessions_state_dir,
        session_visible_for_workspace=session_visible_for_workspace,
        attach_view=_attach_view,
        lease_exhausted_body=_lease_exhausted_body,
        lease_exhausted_error=LeaseExhaustedError,
        puppetmaster_available=_puppetmaster_available,
        set_codegraph_status=_set_codegraph_status,
        index_codegraph_bg=_index_codegraph_bg,
        maybe_refresh_codegraph=_maybe_refresh_codegraph,
    )


def _settings_services():
    """Build SettingsServices from live server module globals (call-time lookup)."""
    from .api.settings import SettingsServices
    return SettingsServices(
        cfg=_cfg,
        get_pilot=lambda: _pilot,
        get_session=lambda: _session,
        parse_bool=_parse_bool,
        set_api_key=set_api_key,
        clear_api_key=clear_api_key,
        rebuild_pilot_and_session=_rebuild_pilot_and_session,
        available_pilots=_available_pilots,
        save_workspace_driver=_save_workspace_driver,
        persist_env_setting=_persist_env_setting,
        get_settings_dict=_get_settings_dict,
    )


def _session_control_services():
    """Build SessionControlServices from live server module globals."""
    from .api.session_control import SessionControlServices
    from .turn_context import context_at as _context_at
    return SessionControlServices(
        cfg=_cfg,
        get_pilot=lambda: _pilot,
        get_runners=lambda: _runners,
        gate_active_pilot_ready=_gate_active_pilot_ready,
        stash_put=_stash_put,
        save_active_transcript=_save_active_transcript,
        upload_dir=_UPLOAD_DIR,
        diag=_diag,
        get_sessions=lambda: _sessions,
        save_transcript=save_transcript,
        set_resume_latch=_set_resume_latch,
        persist_boot_usage=_persist_boot_usage,
        consume_resume_pending=_consume_resume_pending,
        checkpoint_transcript=_checkpoint_transcript,
        context_at=_context_at,
    )


def _usage_services():
    """Build UsageServices from live server module globals (call-time lookup)."""
    from .api.usage import UsageServices
    return UsageServices(
        cfg=_cfg,
        boot_repos=lambda: set(_BOOT_REPOS),
        boot_usage_meters=_boot_usage_meters,
        usage_cache_get=_usage_cache_get,
        usage_cache_put=_usage_cache_put,
        boot_session_cost=_boot_session_cost,
        scoped_jobs_with_stores=_scoped_jobs_with_stores,
        job_in_cost_window=_job_in_cost_window,
        swarm_registry=_swarm_registry,
        job_swarm_accounting=_job_swarm_accounting,
        tokens_cached_swarm=_tokens_cached_swarm,
        job_savings_fields=_job_savings_fields,
        active_session_total=_active_session_total,
        sum_job_set_savings=_sum_job_set_savings,
        cache_savings=_cache_savings,
        boot_cost_source=_boot_cost_source,
        tool_output_savings_fields=_tool_output_savings_fields,
        persist_boot_usage=_persist_boot_usage,
        retry_on_locked=_retry_on_locked,
        diag=_diag,
        get_pilot=lambda: _pilot,
    )


def _provider_services():
    """Build ProviderServices from live server module globals (call-time lookup)."""
    from .api.providers import ProviderServices
    return ProviderServices(
        cfg=_cfg,
        diag=_diag,
        parse_bool=_parse_bool,
        resync_driver_after_model_curation=_resync_driver_after_model_curation,
        driver_provider_available=_driver_provider_available,
        resolve_available_driver=_resolve_available_driver,
        rebuild_pilot_and_session=_rebuild_pilot_and_session,
    )


def _file_services():
    """Build FileServices from live server module globals (call-time lookup)."""
    from .api.files import FileServices
    return FileServices(
        cfg=_cfg,
        sessions=_sessions,
        upload_dir=_UPLOAD_DIR,
    )


def _remove_session_transcript(sid: str) -> None:
    from .api.sessions import remove_session_transcript
    remove_session_transcript(sid, state_dir=_sessions_state_dir(), diag=_diag)


def _handle_session_delete(sid: str) -> tuple[int, dict]:
    from .api.sessions import handle_session_delete
    return handle_session_delete(sid, _session_services())


def _handle_session_relocate(body: dict) -> tuple[int, dict]:
    """Move an existing session into a project workspace (no new blank session).

    Updates ``workspace_root``/``repo``, records the target in recents, opens
    the workspace as active, and keeps the same session id / transcript file.
    """
    from .api.sessions import handle_session_relocate
    return handle_session_relocate(body, _session_services())


def _apply_model_context_window():
    """Recompute _cfg.max_context_tokens for the active driver's real window
    after a model swap. An explicit HARNESS_MAX_CONTEXT_TOKENS env override
    always wins (so a deliberate cap is never silently widened)."""
    if "HARNESS_MAX_CONTEXT_TOKENS" in os.environ:
        return
    try:
        from pmharness.registry import context_window
        _cfg.max_context_tokens = context_window(_cfg.driver, default=200000)
    except Exception as e:
        _diag("server.apply_model_context_window", e)


def _attach_services():
    """Build AttachServices from live server module globals (call-time lookup)."""
    from .api.attach import AttachServices

    def _set_pilot(pilot: Any) -> None:
        global _pilot
        _pilot = pilot

    def _set_session(session: Any) -> None:
        global _session
        _session = session

    return AttachServices(
        get_pilot=lambda: _pilot,
        set_pilot=_set_pilot,
        get_session=lambda: _session,
        set_session=_set_session,
        cfg=_cfg,
        runners=_runners,
        sessions=_sessions,
        pilot_swap_lock=_pilot_swap_lock,
        bind_pilot_services=_bind_pilot_services,
        build_conversational_pilot=_build_conversational_pilot,
        sync_pilot_session_id=_sync_pilot_session_id,
        sessions_state_dir=_sessions_state_dir,
        diag=_diag,
        apply_model_context_window=_apply_model_context_window,
        freeze_pilot_meters_into_boot_carry=_freeze_pilot_meters_into_boot_carry,
        runner_config_snapshot=_runner_config_snapshot,
    )


def _live_pilot_driver() -> str:
    """Driver bound to the live ConversationalSession (may lag ``_cfg.driver``
    after a deferred mid-turn picker change)."""
    try:
        d = getattr(getattr(_pilot, "config", None), "driver", None)
        # MagicMock / half-init pilots used in detach tests have a non-str
        # driver; never treat those as a real mismatch worth rebuilding.
        return d.strip() if isinstance(d, str) else ""
    except Exception:
        return ""


def _history_for_pilot_swap(pilot: Any) -> Any:
    """History to copy onto a replacement pilot (prefer live transcript).

    Deferred placeholders keep turns in ``_transcript`` with ``_history=[]``.
    Prefer non-empty ``export_history`` / ``export_transcript_data`` so an idle
    swap cannot wipe the session (mirror hydrate-prefer-live from v0.9.67).
    """
    old_history = getattr(pilot, "_history", None)
    if old_history:
        return old_history
    try:
        exported = None
        export_history = getattr(pilot, "export_history", None)
        if callable(export_history):
            exported = export_history()
        if not exported:
            export_transcript = getattr(pilot, "export_transcript_data", None)
            if callable(export_transcript):
                data = export_transcript()
                if isinstance(data, dict):
                    exported = data.get("history") or []
                elif isinstance(data, list):
                    exported = data
        if exported:
            return list(exported)
    except Exception as e:
        _diag("server.pilot_swap_history_export", e)
    return old_history


def _perform_pilot_swap(model: str) -> None:
    """Rebuild the active pilot onto ``model``, preserving history/MCP.

    Freezes cost meters into boot carry at the OLD pilot's rates before the
    rebuild so historical ``est_cost_usd`` cannot jump when the new model is
    cheaper or dearer. Token meters are not copied onto the replacement.
    Caller must ensure the pilot is not mid-turn. Raises on build failure.
    """
    global _pilot
    # Finish deferred cold build before reading history — placeholders keep
    # turns in _transcript with empty _history; copying that would wipe disk.
    if is_deferred_placeholder(_pilot) or callable(
        getattr(_pilot, "ensure_ready", None)
    ):
        _ensure_active_pilot_ready()
    with _pilot_swap_lock:
        old_history = _history_for_pilot_swap(_pilot)
        old_auto_distill = getattr(_pilot, "_auto_distill", False)
        old_pilot = _pilot
        # Freeze spend at old rates before retargeting _cfg.driver.
        try:
            _freeze_pilot_meters_into_boot_carry(old_pilot)
        except Exception:
            pass
        _cfg.driver = model
        _apply_model_context_window()
        # Frozen per-runner config; meters already in carry -- start clean.
        _pilot = ConversationalSession(_runner_config_snapshot())
        if old_history is not None:
            _pilot._history = old_history
        _pilot._auto_distill = old_auto_distill
        _pilot._mcp = _mcp
        try:
            _bind_pilot_services(_pilot)
        except Exception:
            # Older call sites relied on bare _mcp assign; binding is best-effort.
            pass
        try:
            _sync_pilot_session_id()
        except Exception:
            pass
        active_id = _sessions.active or _runners.active_view_id
        if active_id:
            # notify=False: meters already frozen above; drop must not re-fold.
            _runners.drop(active_id, notify=False)
            _runners.get_or_create(active_id, lambda: _pilot)
            _runners.set_active_view(active_id)
    _save_workspace_driver(_cfg.repo, model)


def _ensure_pilot_matches_driver(target: str | None = None) -> bool:
    """Apply a deferred picker swap before starting an idle turn.

    Returns True if the live pilot already matches (or was rebuilt). Returns
    False when the pilot is busy (caller should not start a conflicting turn
    under a mismatched driver -- the deferred choice waits).
    """
    # Cold-attach may still be building; never start a turn on a placeholder.
    _ensure_active_pilot_ready()
    want = (target or _cfg.driver or "").strip()
    if not want:
        return True
    have = _live_pilot_driver()
    if not have:
        # No bound string driver (unit-test MagicMock, half-init) -- leave alone.
        return True
    if want == have:
        return True
    busy = getattr(_pilot, "_busy", None)
    if busy is not None and busy.locked():
        return False
    _perform_pilot_swap(want)
    return True


def _rebuild_pilot_and_session():
    """Rebuild the ACTIVE view's runner for the current driver, preserving history.

    Body lives in ``harness.api.attach.rebuild_pilot_and_session``; this
    wrapper injects live module globals.
    """
    from .api.attach import rebuild_pilot_and_session
    rebuild_pilot_and_session(_attach_services())


def _session_row_is_empty(row: dict) -> bool:
    """True for a never-used session: zero token meters and no transcript body."""
    tokens = 0
    for key in ("input_tokens", "output_tokens", "cache_read_tokens"):
        try:
            tokens += int(row.get(key, 0) or 0)
        except Exception:
            pass
    if tokens:
        return False
    transcript = load_transcript(_sessions_state_dir(), row.get("id") or "")
    if isinstance(transcript, dict):
        return not (transcript.get("history") or transcript.get("display"))
    return not transcript


def _scrub_app_root_sessions_on_boot() -> None:
    """Boot hygiene for session rows rooted at the Marionette app checkout.

    Rows persisted by pre-v0.9.36 builds still carry repo/workspace_root =
    the app install root; the boot active-session attach and
    /api/sessions/switch then re-point the workspace back at the checkout
    (the "snaps back to marionette" bug). Best-effort, never raises:

    - Purge EMPTY app-root rows (zero tokens, no transcript) including their
      transcript files (state-scoping invariant #5). Non-empty rows survive
      but must not drive workspace selection.
    - If the active session is still rooted at the app checkout while the
      restored workspace repo differs, activate the newest session under the
      restored repo instead (same-workspace promotion, invariant #2).
    """
    try:
        app_rows = [
            s for s in _sessions.rows()
            if _is_app_install_root(session_stored_root(s))
        ]
        if not app_rows:
            return
        prior_active = _sessions.active
        empty_ids = [s["id"] for s in app_rows if _session_row_is_empty(s)]
        removed = _sessions.remove_rows(empty_ids) if empty_ids else []
        for sid in removed:
            _remove_session_transcript(sid)

        restored_repo = (_cfg.repo or "").strip()
        if not restored_repo or _is_app_install_root(restored_repo):
            # The user really is working in the checkout (or nothing was
            # restored): leave the active session alone.
            return
        active_row = next(
            (s for s in _sessions.rows() if s.get("id") == _sessions.active),
            None,
        )
        active_on_app_root = active_row is not None and _is_app_install_root(
            session_stored_root(active_row)
        )
        if active_on_app_root or (prior_active in removed):
            _sessions.activate_newest_for_root(restored_repo)
    except Exception as e:
        _diag("server.boot_app_root_sessions", e)


_scrub_app_root_sessions_on_boot()


def _migrate_orphan_sessions_to_home() -> None:
    """Bind empty-root session rows to the durable Home workspace on boot.

    Pre-home builds left rootless sessions visible everywhere (or nowhere in
    the Projects rail). Migrating them into Home keeps transcripts reachable
    under Projects -> Home without creating new session ids.
    """
    try:
        home = _ensure_home_workspace()
        _sessions.migrate_empty_roots(home)
    except Exception as e:
        _diag("server.boot_home_session_migrate", e)


_migrate_orphan_sessions_to_home()

# Startup: Restore the active/most-recent session's transcript into _pilot
# and register it as the active view in the runner registry.
if _sessions.active:
    _startup_history = load_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active)
    if _startup_history:
        _pilot.load_history(_startup_history)
    _runners.get_or_create(_sessions.active, lambda: _pilot)
    _runners.set_active_view(_sessions.active)
_sync_pilot_session_id()

_skills = SkillStore()
_rules = RuleStore()
_commands = CommandStore()
_memory = MemoryStore()
_UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "harness-uploads")
os.makedirs(_UPLOAD_DIR, mode=0o700, exist_ok=True)
# The mode above only applies when the dir is created (and is umask-clipped);
# harden explicitly so uploaded images are never world-readable under the
# shared system temp dir -- on Windows too (icacls), where makedirs mode is a
# no-op.
restrict_dir_to_owner(_UPLOAD_DIR)

# Message stash: large chat/autopilot payloads cannot ride in the SSE GET's
# query string (they'd blow past the HTTP request-line limit and get
# silently dropped -- real data loss on a big paste). The client instead
# POSTs the payload here first and hands the stream only a short id via
# ?mid=. Small in-process dict, capped so a client that stashes-and-never-
# consumes (e.g. an abandoned tab) can't leak memory forever.
# Chat stash lives in harness.api.sessions; re-export historical names for
# tests and SSE GET mid= resolution.
from .api.sessions import (  # noqa: E402
    _CHAT_STASH,
    _CHAT_STASH_MAX,
    stash_put as _stash_put,
    stash_pop as _stash_pop,
)

# Wiki graph cache / handoff nonces / status helpers live in harness.api.wiki;
# re-export historical names for tests and pilot._on_wiki_ingest.
from .api.wiki import (  # noqa: E402
    WIKI_NEEDS_AUTH_HINT as _WIKI_NEEDS_AUTH_HINT,
    wiki_graph_cache as _wiki_graph_cache,
    WIKI_GRAPH_TTL as _WIKI_GRAPH_TTL,
    wiki_connect_nonces as _wiki_connect_nonces,
    WIKI_CONNECT_NONCE_TTL as _WIKI_CONNECT_NONCE_TTL,
    wiki_cache_key as _wiki_cache_key,
    clear_wiki_graph_cache as _clear_wiki_graph_cache,
    mint_wiki_connect_nonce as _mint_wiki_connect_nonce,
    consume_wiki_connect_nonce as _consume_wiki_connect_nonce,
    wiki_status_extras as _wiki_status_extras,
)

# Per-process auth token (defense-in-depth). Written owner-only (chmod 600 on
# POSIX, NTFS ACL on Windows) so the local client (Electron main / served page)
# can read it; required on mutating endpoints. Origin/Host validation below is
# the primary anti-RCE guard.
_TOKEN = os.environ.get("HARNESS_TOKEN") or _secrets.token_hex(16)
_TOKEN_FILE = os.path.join(_state_home(), "token")
try:
    os.makedirs(os.path.dirname(_TOKEN_FILE), exist_ok=True)
    with open(_TOKEN_FILE, "w", encoding="utf-8") as _tf2:
        _tf2.write(_TOKEN)
    if not restrict_to_owner(_TOKEN_FILE):
        _diag("secure_files.restrict_failed", msg=_TOKEN_FILE)
except OSError:
    pass

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _host_ok(host_header: str) -> bool:
    """Defeat DNS-rebinding: the Host must be a literal loopback name. A rebound
    attacker domain (evil.com -> 127.0.0.1) shows its own name in Host."""
    if not host_header:
        return False
    if host_header.startswith("["):
        # Bracketed IPv6 ("[::1]" or "[::1]:8000"): the bracket pair is the
        # host. A blind rsplit(":") would mangle the portless form to "[:".
        host = host_header.split("]", 1)[0] + "]"
    else:
        host = host_header.rsplit(":", 1)[0]
    return host in _ALLOWED_HOSTS


def _origin_ok(origin: str) -> bool:
    """A malicious webpage sends its own Origin (https://evil.com) on cross-origin
    requests -> reject. Same-origin requests omit Origin; Electron file:// sends
    'null'. Both allowed."""
    if not origin or origin == "null":
        return True
    try:
        from urllib.parse import urlparse as _up
        h = _up(origin).hostname
        return h in _ALLOWED_HOSTS
    except Exception:
        return False


def _parse_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("1", "true", "yes", "on")
    return False


_codegraph_status = "none"
_codegraph_status_reason = None
# Last preflight payload for the active repo (surfaced on /api/codegraph).
_codegraph_preflight = None  # dict | None
_codegraph_suggested_action = None  # dict | None

# Short-TTL cache for the /api/codegraph status payload, keyed by repo path.
# Reading codegraph status spawns a `puppetmaster codegraph status --json`
# subprocess (interpreter cold-start + DB read) on every poll, which is the
# source of the panel's load lag. The graph only changes on (re)index, so we
# serve a cached payload for a few seconds and only re-spawn when stale. The
# cache is bypassed entirely while status == "indexing" (that path never hits
# the subprocess), so a fresh index is reflected as soon as it finishes.
_codegraph_status_cache = {}  # repo -> (monotonic_expiry, payload_dict)
_CODEGRAPH_STATUS_TTL = 30.0  # seconds
# After an indexer failure, suppress GET auto-reindex for this many seconds so
# a missing cwd / WinError 2 cannot spam the panel and log every poll.
_codegraph_fail_until = {}  # repo -> monotonic timestamp

_pilot._on_wiki_ingest = _clear_wiki_graph_cache


# Handle to the in-flight CodeGraph indexer: (repo_path, Popen). Lets status
# self-heal -- a wedged "indexing" flag can never outlive the actual job -- and
# prevents a SECOND indexer from spawning while one runs (concurrent indexers
# collide on the same SQLite and Puppetmaster fails them lock-busy, which
# manifested as the panel locking up + metrics vanishing).
_codegraph_index_proc = None  # tuple[str, subprocess.Popen] | None
_codegraph_index_lock = threading.Lock()


def _codegraph_indexed(repo_path: str) -> bool:
    """True only when a built CodeGraph DB exists for the repo.

    The `.codegraph/` directory alone is NOT proof of an index: `codegraph init`
    writes `config.json` there before any indexing, and `codegraph status` hangs
    on a config-only checkout (no DB) until it times out -- which surfaced as
    "unsupported" on fresh installs. Gate on the actual DB file so an init'd-but-
    unindexed checkout is treated as needing an index, not as ready.
    """
    try:
        return os.path.isfile(os.path.join(repo_path, ".codegraph", "codegraph.db"))
    except Exception:
        return False


def _codegraph_index_alive() -> bool:
    """True only while the tracked indexer subprocess is actually running."""
    p = _codegraph_index_proc
    if p is None:
        return False
    try:
        return p[1].poll() is None
    except Exception:
        return False


def _codegraph_index_log_path() -> str:
    state = (_cfg.state_dir if _cfg else "") or os.path.expanduser("~/.pmharness/state")
    try:
        os.makedirs(state, exist_ok=True)
    except Exception:
        pass
    return os.path.join(state, "codegraph-index.log")


def _codegraph_api_payload(repo, status=None):
    """Shared /api/codegraph fields (reason, preflight, suggested_action)."""
    st = status if status is not None else (_get_codegraph_status(repo) if repo else "none")
    return {
        "indexed": bool(repo and _codegraph_indexed(repo)),
        "status": st,
        "reason": _codegraph_status_reason,
        "preflight": _codegraph_preflight,
        "suggested_action": _codegraph_suggested_action,
        "repo": repo or "",
    }


def _codegraph_tail_log(max_chars: int = 800) -> str:
    path = _codegraph_index_log_path()
    try:
        if not os.path.isfile(path):
            return ""
        with open(path, encoding="utf-8", errors="replace") as f:
            data = f.read()
        if len(data) <= max_chars:
            return data.strip()
        return data[-max_chars:].strip()
    except Exception:
        return ""


def _prepare_codegraph_scope(repo_path: str) -> dict:
    """Run preflight; auto-apply asset excludes when scope is recommended.

    Returns the preflight dict and updates globals for API/UI. Does not start
    the indexer. Verdict ``unlikely`` means callers should NOT start a full
    index; ``scope_recommended`` / ``ok`` may proceed (after excludes merge).
    """
    global _codegraph_status, _codegraph_status_reason
    global _codegraph_preflight, _codegraph_suggested_action
    from .codegraph_preflight import (
        child_exclude_globs,
        ensure_lua_includes,
        merge_codegraph_excludes,
        preflight_workspace,
    )

    pre = preflight_workspace(repo_path)
    _codegraph_preflight = pre
    _codegraph_suggested_action = None
    verdict = pre.get("verdict") or "ok"

    try:
        ensure_lua_includes(repo_path)
    except Exception as e:
        _diag("server.codegraph_lua_include", e)

    if verdict == "unlikely":
        _codegraph_status = "needs_scope"
        _codegraph_status_reason = pre.get("reason") or (
            "Workspace has almost no indexable source under a huge tree."
        )
        roots = pre.get("suggested_roots") or []
        excludes = pre.get("suggested_excludes") or []
        if roots:
            _codegraph_suggested_action = {
                "kind": "open_subdir",
                "path": os.path.join(repo_path, roots[0]),
                "excludes": excludes,
            }
        elif excludes:
            _codegraph_suggested_action = {
                "kind": "write_excludes",
                "excludes": excludes,
            }
        return pre

    if verdict == "scope_recommended":
        extra = child_exclude_globs(pre.get("suggested_excludes") or [])
        try:
            merge_codegraph_excludes(repo_path, extra_excludes=extra or None)
        except Exception as e:
            _diag("server.codegraph_merge_excludes", e)
        _codegraph_status_reason = pre.get("reason") or (
            "Large install detected; asset excludes applied before indexing."
        )
        roots = pre.get("suggested_roots") or []
        if roots:
            _codegraph_suggested_action = {
                "kind": "open_subdir",
                "path": os.path.join(repo_path, roots[0]),
                "excludes": pre.get("suggested_excludes") or [],
            }
        else:
            _codegraph_suggested_action = {
                "kind": "write_excludes",
                "excludes": pre.get("suggested_excludes") or [],
            }
        # Still index after excludes — do not leave the user on needs_scope
        # when we can recover automatically.
        return pre

    # ok
    try:
        # Ensure lua is graphable even on normal repos that already have config.
        ensure_lua_includes(repo_path)
    except Exception:
        pass
    return pre


def _index_codegraph_bg(repo_path: str):
    global _codegraph_status, _codegraph_status_reason, _codegraph_status_cache
    global _codegraph_preflight, _codegraph_suggested_action
    if not _puppetmaster_available():
        _codegraph_status = "unsupported"
        _codegraph_status_reason = "puppetmaster not found -- codegraph/swarm unavailable"
        return
    global _codegraph_index_proc

    # Missing/moved workspace: fail once with a clear reason. Spawning with a
    # bad cwd on Windows surfaces as WinError 2/267 and the GET self-heal loop
    # used to re-kick forever, stuffing codegraph-index.log into the panel.
    if not repo_path or not os.path.isdir(repo_path):
        _codegraph_status = "unsupported"
        _codegraph_status_reason = (
            f"Workspace path is missing or not a directory: {repo_path or '(empty)'}. "
            "Open Folder on the real path (e.g. C:\\Ashita or C:\\Ashita\\addons) "
            "and Remove the phantom from Projects."
        )
        _codegraph_suggested_action = None
        if repo_path:
            _codegraph_status_cache.pop(repo_path, None)
        return

    # Claim INDEXING immediately -- before preflight -- so /api/workspace/open
    # and status polls never flash UNSUPPORTED while scope prep runs. Preflight
    # may still override to needs_scope / unsupported on real failures.
    _codegraph_status = "indexing"
    _codegraph_status_reason = None
    _codegraph_status_cache.pop(repo_path, None)

    # Preflight before spawning: avoid a doomed 10-minute walk of game assets.
    try:
        pre = _prepare_codegraph_scope(repo_path)
    except Exception as e:
        _diag("server.codegraph_preflight", e)
        pre = {"verdict": "ok"}
    if (pre.get("verdict") or "") == "unlikely":
        # Do not start indexer; status already needs_scope with reason.
        _codegraph_status_cache.pop(repo_path, None)
        return

    # Guard against a second indexer while one is already running -- concurrent
    # codegraph indexers collide on the same SQLite (lock-busy) and wedge the panel.
    with _codegraph_index_lock:
        if _codegraph_index_alive():
            _codegraph_status = "indexing"
            return
        _codegraph_status = "indexing"
        if not _codegraph_status_reason:
            _codegraph_status_reason = None
        # Invalidate any cached status for this repo so the panel does not show
        # stale "ready" stats while a fresh (re)index is running.
        _codegraph_status_cache.pop(repo_path, None)
        _codegraph_fail_until.pop(repo_path, None)
        log_path = _codegraph_index_log_path()
        try:
            import subprocess
            log_f = open(log_path, "a", encoding="utf-8", errors="replace")
            log_f.write(f"\n--- codegraph init --index @ {repo_path} ---\n")
            log_f.flush()
            proc = subprocess.Popen(
                _puppetmaster_cmd("codegraph", "init", "--index"),
                cwd=repo_path,
                stdout=log_f,
                stderr=subprocess.STDOUT,
            )
            _codegraph_index_proc = (repo_path, proc)
        except Exception as e:
            _codegraph_status = "unsupported"
            _codegraph_status_reason = f"failed to start indexer: {e}"
            return

    # After scope/excludes, allow a longer run; still a backstop so a wedged
    # process cannot pin the panel forever.
    index_timeout = 1800

    def wait_and_update():
        global _codegraph_status, _codegraph_status_reason, _codegraph_index_proc
        global _codegraph_suggested_action
        timed_out = False
        try:
            proc.wait(timeout=index_timeout)
            if proc.returncode == 0 and _codegraph_indexed(repo_path):
                _codegraph_status = "ready"
                _codegraph_status_reason = None
            elif proc.returncode == 0:
                _codegraph_status = "unsupported"
                _codegraph_status_reason = (
                    "Indexer exited 0 but no codegraph.db was written. "
                    + (_codegraph_tail_log(max_chars=400) or "See codegraph-index.log.")
                )
            else:
                _codegraph_status = "unsupported"
                # One clean failure line — do not dump the whole repeated log.
                tail = _codegraph_tail_log(max_chars=400)
                # Prefer the last non-empty line of the tail.
                last_line = ""
                if tail:
                    for line in reversed(tail.splitlines()):
                        if line.strip():
                            last_line = line.strip()
                            break
                _codegraph_status_reason = (
                    f"Indexer failed (exit {proc.returncode}). "
                    + (last_line or "See ~/.pmharness/state/codegraph-index.log.")
                )
                # Back off auto-reindex for this path so GET polling cannot
                # restart a doomed indexer every few seconds.
                _codegraph_fail_until[repo_path] = time.monotonic() + 120.0
        except Exception:
            timed_out = True
            _codegraph_status = "unsupported"
            _codegraph_status_reason = (
                f"Indexing timed out after {index_timeout // 60} minutes. "
                "The tree is likely still too large — open a code subdirectory "
                "or apply asset excludes, then re-index."
            )
            _codegraph_fail_until[repo_path] = time.monotonic() + 120.0
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            with _codegraph_index_lock:
                if _codegraph_index_proc and _codegraph_index_proc[1] is proc:
                    _codegraph_index_proc = None
            _codegraph_status_cache.pop(repo_path, None)
            if timed_out:
                _codegraph_suggested_action = {
                    "kind": "write_excludes",
                    "excludes": (pre.get("suggested_excludes") if isinstance(pre, dict) else None) or [],
                }

    threading.Thread(target=wait_and_update, daemon=True).start()


def _reindex_codegraph_bg(repo_path: str):
    global _codegraph_status, _codegraph_status_reason
    if not _puppetmaster_available():
        _codegraph_status = "unsupported"
        _codegraph_status_reason = "puppetmaster not found -- codegraph/swarm unavailable"
        return
    # Force a fresh preflight + index (same path as init).
    _index_codegraph_bg(repo_path)


def _get_codegraph_status(repo_path: str) -> str:
    """Resolve CodeGraph badge status for ``repo_path``.

    ``unsupported`` is reserved for confirmed failures (PM missing, path
    missing, indexer failed with a reason). The transient empty-index case
    (PM available, path exists, no DB yet, no failure reason) returns
    ``indexing`` so the LeftRail never flashes UNSUPPORTED before the
    indexer starts.
    """
    global _codegraph_status, _codegraph_status_reason
    if not repo_path:
        return "none"
    if not _puppetmaster_available():
        _codegraph_status = "unsupported"
        return "unsupported"
    if not os.path.isdir(repo_path):
        _codegraph_status = "unsupported"
        return "unsupported"
    # Self-heal: trust "indexing" while the indexer is alive OR while we are
    # still in preflight/spawn (status=indexing but proc not assigned yet).
    # Demoting that window to unsupported caused the LeftRail UNSUPPORTED flash.
    if _codegraph_status == "indexing":
        if _codegraph_index_alive():
            return "indexing"
        if _codegraph_indexed(repo_path):
            _codegraph_status = "ready"
            return "ready"
        # Proc handle present but dead => indexer exited without a DB.
        if _codegraph_index_proc is not None:
            _codegraph_status = "unsupported"
            if not _codegraph_status_reason:
                _codegraph_status_reason = "Indexer stopped before writing codegraph.db"
            return "unsupported"
        # No proc yet (preflight / about to spawn) -- stay indexing.
        return "indexing"
    if _codegraph_status == "needs_scope":
        return "needs_scope"
    if _codegraph_indexed(repo_path):
        _codegraph_status = "ready"
        return "ready"
    # Confirmed failure with a reason sticks; bare default / empty-index does not.
    if _codegraph_status == "unsupported" and _codegraph_status_reason:
        return "unsupported"
    # Transient: PM ok, path ok, not indexed yet — never flash unsupported.
    if _codegraph_status not in ("indexing", "pending"):
        _codegraph_status = "indexing"
    return "indexing"


# Debounce: never re-check staleness more than once per this interval per repo,
# so per-turn triggers cannot thrash the (CPU-heavy) reindex during rapid edits.
_codegraph_stale_check_at = {}  # repo -> monotonic timestamp of last check
_CODEGRAPH_STALE_DEBOUNCE = 20.0  # seconds


def _codegraph_is_stale(repo_path: str) -> bool:
    """True if the working tree has changed since the .codegraph index was built.

    Detects edits AND deletions: we compare the index mtime against the newest
    mtime of (a) every source FILE and (b) every DIRECTORY. Directory mtimes are
    the key to catching deletions/renames -- removing a file bumps its parent
    dir's mtime even though no surviving file looks newer (the original bug:
    deleted files left the index referencing ghosts while this returned False).
    """
    try:
        codegraph_path = os.path.join(repo_path, ".codegraph")
        if not os.path.exists(codegraph_path):
            return False
        cg_mtime = os.path.getmtime(codegraph_path)
        skip_dirs = {".git", "node_modules", ".venv", ".codegraph", "dist", "build", "__pycache__"}
        extensions = {".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".swift", ".go", ".rs"}
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            # (b) directory mtime -- catches deletions/renames/additions in this dir
            try:
                if os.path.getmtime(root) > cg_mtime:
                    return True
            except Exception:
                pass
            # (a) source file mtime -- catches in-place edits
            for file in files:
                _, ext = os.path.splitext(file)
                if ext.lower() in extensions:
                    try:
                        if os.path.getmtime(os.path.join(root, file)) > cg_mtime:
                            return True
                    except Exception:
                        pass
    except Exception:
        pass
    return False


def _maybe_refresh_codegraph(repo_path: str, *, force: bool = False) -> None:
    """Debounced, background staleness-driven reindex. Safe to call on every turn
    and on session switch -- the debounce + the indexing-guard ensure it never
    thrashes. force=True bypasses the debounce (e.g. an explicit user action)."""
    if not repo_path:
        return
    import time as _time
    if not force:
        last = _codegraph_stale_check_at.get(repo_path, 0.0)
        if (_time.monotonic() - last) < _CODEGRAPH_STALE_DEBOUNCE:
            return
    _codegraph_stale_check_at[repo_path] = _time.monotonic()

    def worker():
        global _codegraph_status, _codegraph_status_reason
        if _codegraph_status == "indexing":
            return
        if _codegraph_is_stale(repo_path):
            _codegraph_status_reason = "files changed -- refreshing index"
            _reindex_codegraph_bg(repo_path)
    try:
        threading.Thread(target=worker, daemon=True).start()
    except Exception as e:
        _diag("server.codegraph_stale_check_thread", e)


def _strip_markdown_fences(text: str) -> str:
    text_stripped = text.strip()
    if text_stripped.startswith("```"):
        lines = text_stripped.splitlines()
        if len(lines) >= 2 and lines[0].startswith("```"):
            if lines[-1].strip() == "```":
                return "\n".join(lines[1:-1])
            else:
                return "\n".join(lines[1:])
    return text


# Editor path / mime / multipart helpers live in harness.api.files; re-export
# historical names for inline-edit and any tests that patch harness.server.
from .api.files import (  # noqa: E402
    resolve_editor_path as _resolve_editor_path,
    guess_file_mime as _guess_file_mime,
    sqlite_table_names as _sqlite_table_names,
    binary_file_payload as _binary_file_payload,
    parse_multipart_files as _parse_multipart_files,
)


class Handler(BaseHTTPRequestHandler):
    def handle_one_request(self):
        # A client (the Electron renderer) closing the socket mid-request --
        # navigating away, stopping a stream, swapping models -- or a handler
        # that answers early without draining the request body (e.g. a 413 for an
        # oversized upload) raises ConnectionError/TimeoutError deep in the stdlib
        # request machinery. That is benign, but on a bare ThreadingHTTPServer it
        # escapes to socketserver's default handle_error and dumps a traceback to
        # stderr. Swallow only those transport errors so a disconnect never prints
        # noise; genuine handler bugs still surface unchanged.
        try:
            super().handle_one_request()
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    def log_message(self, *a):  # quiet
        pass

    def _cors(self):
        # No wildcard. Reflect the Origin only when it is a loopback origin, so a
        # cross-origin attacker page can never read responses.
        origin = self.headers.get("Origin", "")
        if origin and origin != "null" and _origin_ok(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Harness-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")

    def _guard(self) -> bool:
        """Reject cross-origin / rebound / unauthenticated requests. Returns True
        if the request should be BLOCKED (and sends the 403)."""
        if not _host_ok(self.headers.get("Host", "")):
            self._send(403, json.dumps({"error": "host not allowed"})); return True
        if not _origin_ok(self.headers.get("Origin", "")):
            self._send(403, json.dumps({"error": "origin not allowed"})); return True
        return False

    def _token_ok(self) -> bool:
        if self.headers.get("X-Harness-Token", "") == _TOKEN:
            return True
        # Accept the token as a query param too, matching do_GET's checks. The IPC
        # POST bridge sends the header, so this changes no current behavior -- it
        # removes an asymmetry where a query-token caller was rejected only on POST.
        try:
            qtok = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        except Exception:
            qtok = ""
        return qtok == _TOKEN

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _handle_wiki_connect(self, u):
        """Apply wiki config from a loopback handoff (nonce + personal LLM URL)."""
        # Host gate stays in the Handler; nonce/token/HTML live in api.wiki.
        if not _host_ok(self.headers.get("Host", "")):
            return self._send(403, json.dumps({"error": "host not allowed"}))
        from .api import wiki as _wiki_api
        status, body, ctype = _wiki_api.handle_wiki_connect(parse_qs(u.query))
        return self._send(status, body, ctype)

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_DELETE(self):
        if self._guard():
            return
        if not self._token_ok():
            return self._send(403, json.dumps({"error": "missing or bad token"}))
        u = urlparse(self.path)
        prefix = "/api/sessions/"
        if u.path.startswith(prefix) and u.path not in ("/api/sessions/clear",):
            sid = u.path[len(prefix):].strip("/")
            if not sid or "/" in sid:
                return self._send(400, json.dumps({"error": "missing session id"}))
            status, payload = _handle_session_delete(sid)
            return self._send(status, json.dumps(payload))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        global _codegraph_status, _codegraph_status_reason
        global _codegraph_preflight, _codegraph_suggested_action
        if self._guard():
            return
        if not self._token_ok():
            return self._send(403, json.dumps({"error": "missing or bad token"}))
        u = urlparse(self.path)
        if u.path == "/api/upload":
            return self._handle_upload()
        if u.path in ("/api/workspaces/switch", "/api/workspaces/create",
                      "/api/sessions/create", "/api/sessions/switch",
                      "/api/sessions/delete", "/api/sessions/clear",
                      "/api/sessions/archive", "/api/sessions/rename",
                      "/api/sessions/relocate", "/api/sessions/move",
                      "/api/session/interrupt", "/api/session/compact", "/api/session/steer",
                      "/api/session/rewind", "/api/session/rewind/restore",
                      "/api/session/queue", "/api/session/queue/reorder",
                      "/api/session/persist", "/api/restart",
                      "/api/chat/stash",
                      "/api/swarm/cancel",
                      "/api/mcp/add", "/api/mcp/remove", "/api/mcp/start",
                      "/api/mcp/stop", "/api/mcp/call",
                      "/api/skills/distill", "/api/skills/approve",
                      "/api/wiki/ingest-prepared",
                      "/api/models/toggle", "/api/models/set",
                      "/api/skills/reject", "/api/skills/archive",
                      "/api/skills/add", "/api/skills/update", "/api/skills/remove",
                      "/api/rules/approve", "/api/rules/reject",
                      "/api/rules/add", "/api/rules/update", "/api/rules/remove",
                      "/api/memory/add", "/api/memory/remove",
                      "/api/memory/propose/accept", "/api/memory/propose/dismiss",
                      "/api/settings", "/api/providers/probe", "/api/providers/key", "/api/wiki/config",
                      "/api/wiki/disconnect", "/api/wiki/handoff", "/api/bedrock",
                      "/api/auth/pools", "/api/auth/pools/add", "/api/auth/pools/remove",
                      "/api/auth/pools/strategy", "/api/auth/pools/reset",
                      "/api/auth/oauth/start", "/api/auth/oauth/poll",
                      "/api/auth/oauth/complete", "/api/auth/oauth/cancel",
                      "/api/auth/cursor-cli/status", "/api/auth/cursor-cli/login",
                      "/api/auth/cursor-cli/trust",
                      "/api/auth/cursor-cli/logout", "/api/auth/cursor-cli/models",
                      "/api/platform", "/api/reviews/apply", "/api/reviews/dismiss",
                      "/api/registry", "/api/roles", "/api/pilot/validate",
                      "/api/worktrees/add", "/api/worktrees/remove",
                      "/api/worktrees/prune", "/api/worktrees/prune-edit-branches",
                      "/api/worktrees/max",
                      "/api/hooks/add", "/api/hooks/update", "/api/hooks/remove",
                      "/api/workspace/open", "/api/workspace/forget", "/api/codegraph/reindex",
                      "/api/codegraph/apply-excludes",
                      "/api/file/write",
                      "/api/file/delete", "/api/file/rename", "/api/file/mkdir",
                      "/api/file/reveal",
                      "/api/inline-edit",
                      "/api/commands/render",
                      "/api/git/connect", "/api/git/device/poll", "/api/git/disconnect",
                      "/api/checkpoints/restore", "/api/checkpoints/snapshot",
                      "/api/terminal/create", "/api/terminal/write",
                      "/api/terminal/resize", "/api/terminal/kill"):
            # Wrap the dispatch so NO handler exception can escape to the
            # socketserver and crash the connection/process. A bad driver spec,
            # a failed rebuild, etc. now return a clean 500 the UI can show
            # instead of taking the whole backend down (the "socket hang up" /
            # "Error opening directory" crash on workspace-open/session-switch).
            try:
                return self._handle_post_json(u.path)
            except Exception as e:
                import traceback as _tb
                _tb.print_exc()
                try:
                    return self._send(500, json.dumps({"error": str(e)}))
                except Exception:
                    return
        return self._send(404, json.dumps({"error": "not found"}))

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        data = self.rfile.read(n)
        try:
            decoded = data.decode()
        except Exception as e:
            raise json.JSONDecodeError("Unicode decode error", doc="", pos=0) from e
        return json.loads(decoded or "{}")

    def _handle_post_json(self, path):
        global _pilot
        global _codegraph_status, _codegraph_status_reason
        global _codegraph_preflight, _codegraph_suggested_action
        try:
            body = self._read_json()
        except json.JSONDecodeError:
            return self._send(400, json.dumps({"error": "invalid JSON"}))
        repo = _cfg.repo

        if path == "/api/reviews/apply":
            from .api import reviews as _rev_api
            status, payload = _rev_api.post_reviews_apply(body, _review_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/reviews/dismiss":
            from .api import reviews as _rev_api
            status, payload = _rev_api.post_reviews_dismiss(body, _review_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/swarm/cancel":
            # Cooperative cancel for a swarm job. Auth/token already applied in
            # do_POST; body + dual-store resolve live in harness.api.jobs.
            from .api import jobs as _jobs_api
            status, payload = _jobs_api.post_swarm_cancel(body, _job_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/session/persist":
            # Flush the live transcript to disk on demand. Called right before a
            # backend restart (self-edit apply) so the fresh process restores the
            # exact conversation state, including any unanswered user turn.
            # Also arms the one-shot resume latch so the post-restart UI can
            # auto-continue -- without treating every trailing user turn as a
            # resume signal on mere session view.
            from .api import session_control as _sc_api
            status, payload = _sc_api.post_session_persist(_session_control_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/restart":
            # Graceful self-restart for non-Electron callers (served browser) and
            # as a fallback path. Persist, ACK, then SIGTERM self so a supervisor
            # (Electron) respawns on the freshly-edited source. In the desktop app
            # the Electron IPC path (harness:restart) is preferred -- it also
            # reloads the renderer -- but this keeps the capability reachable over
            # HTTP for the pilot or a browser session.
            from .api import session_control as _sc_api
            ok, err = _sc_api.prepare_session_restart(_session_control_services())
            if not ok:
                _diag("server.self_edit_restart_persist", Exception(err or "persist failed"))
            self._send(200, json.dumps({"ok": True, "restarting": True}))

            def _delayed_self_terminate():
                import time as _t
                import signal as _signal
                _t.sleep(0.4)  # let the 200 flush before we exit
                try:
                    if os.name == "nt":
                        os._exit(0)
                    else:
                        os.kill(os.getpid(), _signal.SIGTERM)
                except Exception:
                    os._exit(0)
            threading.Thread(target=_delayed_self_terminate, daemon=True).start()
            return
        if path == "/api/session/compact":
            from .api import session_control as _sc_api
            status, payload = _sc_api.post_session_compact(_session_control_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/checkpoints/restore":
            from .api import checkpoints as _ckpt_api
            status, payload = _ckpt_api.post_checkpoints_restore(
                body, _checkpoint_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/checkpoints/snapshot":
            from .api import checkpoints as _ckpt_api
            status, payload = _ckpt_api.post_checkpoints_snapshot(
                body, _checkpoint_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/codegraph/reindex":
            from .api import codegraph as _cg_api
            status, payload = _cg_api.post_codegraph_reindex(_codegraph_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/codegraph/apply-excludes":
            from .api import codegraph as _cg_api
            status, payload = _cg_api.post_codegraph_apply_excludes(
                body, _codegraph_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/commands/render":
            from .api import commands as _cmd_api
            status, payload = _cmd_api.post_commands_render(
                body, _commands_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/inline-edit":
            from .api import reviews as _rev_api
            status, payload = _rev_api.post_inline_edit(body, _review_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/file/write":
            from .api import files as _files_api
            status, payload = _files_api.post_file_write(body, _file_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/file/delete":
            from .api import files as _files_api
            status, payload = _files_api.post_file_delete(body, _file_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/file/rename":
            from .api import files as _files_api
            status, payload = _files_api.post_file_rename(body, _file_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/file/mkdir":
            from .api import files as _files_api
            status, payload = _files_api.post_file_mkdir(body, _file_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/file/reveal":
            from .api import files as _files_api
            status, payload = _files_api.post_file_reveal(body, _file_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/workspace/open":
            from .api import workspace as _ws_api
            status, payload = _ws_api.post_workspace_open(body, _workspace_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/workspace/forget":
            from .api import workspace as _ws_api
            status, payload = _ws_api.post_workspace_forget(
                body, _workspace_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/workspaces/switch":
            from .api import workspace as _ws_api
            status, payload = _ws_api.post_workspaces_switch(
                body, _workspace_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/workspaces/create":
            from .api import workspace as _ws_api
            status, payload = _ws_api.post_workspaces_create(
                body, _workspace_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/mcp/add":
            from .api import mcp as _mcp_api
            status, payload = _mcp_api.post_mcp_add(body, _mcp_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/mcp/remove":
            from .api import mcp as _mcp_api
            status, payload = _mcp_api.post_mcp_remove(body, _mcp_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/mcp/start":
            from .api import mcp as _mcp_api
            status, payload = _mcp_api.post_mcp_start(body, _mcp_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/mcp/stop":
            from .api import mcp as _mcp_api
            status, payload = _mcp_api.post_mcp_stop(body, _mcp_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/mcp/call":
            from .api import mcp as _mcp_api
            status, payload = _mcp_api.post_mcp_call(body, _mcp_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/skills/distill":
            from .api import skills as _skills_api
            status, payload = _skills_api.post_skills_distill(_skills_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/wiki/ingest-prepared":
            # One-click approve: file the locally-orchestrated pages into the wiki.
            from .api import wiki as _wiki_api
            status, payload = _wiki_api.post_wiki_ingest_prepared(
                body, _wiki_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/models/toggle":
            from .api import providers as _prov_api
            status, payload = _prov_api.post_models_toggle(body, _provider_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/models/set":
            from .api import providers as _prov_api
            status, payload = _prov_api.post_models_set(body, _provider_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/skills/approve":
            from .api import skills as _skills_api
            status, payload = _skills_api.post_skills_approve(body, _skills_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/skills/add":
            from .api import skills as _skills_api
            status, payload = _skills_api.post_skills_add(body, _skills_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/skills/update":
            from .api import skills as _skills_api
            status, payload = _skills_api.post_skills_update(body, _skills_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/skills/remove":
            from .api import skills as _skills_api
            status, payload = _skills_api.post_skills_remove(body, _skills_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/skills/reject":
            from .api import skills as _skills_api
            status, payload = _skills_api.post_skills_reject(body, _skills_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/skills/archive":
            from .api import skills as _skills_api
            status, payload = _skills_api.post_skills_archive(body, _skills_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/rules/approve":
            from .api import skills as _skills_api
            status, payload = _skills_api.post_rules_approve(body, _skills_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/rules/add":
            from .api import skills as _skills_api
            status, payload = _skills_api.post_rules_add(body, _skills_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/rules/update":
            from .api import skills as _skills_api
            status, payload = _skills_api.post_rules_update(body, _skills_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/rules/remove":
            from .api import skills as _skills_api
            status, payload = _skills_api.post_rules_remove(body, _skills_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/rules/reject":
            from .api import skills as _skills_api
            status, payload = _skills_api.post_rules_reject(body, _skills_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/memory/add":
            from .api import skills as _skills_api
            status, payload = _skills_api.post_memory_add(body, _skills_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/memory/remove":
            from .api import skills as _skills_api
            status, payload = _skills_api.post_memory_remove(body, _skills_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/memory/propose/accept":
            from .api import skills as _skills_api
            status, payload = _skills_api.post_memory_propose_accept(
                body, _skills_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/memory/propose/dismiss":
            from .api import skills as _skills_api
            status, payload = _skills_api.post_memory_propose_dismiss(
                body, _skills_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/sessions/create":
            from .api import sessions as _sessions_api
            status, payload = _sessions_api.post_sessions_create(body, _session_services())
            return self._send(status, json.dumps(payload))
        if path in ("/api/sessions/relocate", "/api/sessions/move"):
            status, payload = _handle_session_relocate(body)
            return self._send(status, json.dumps(payload))
        if path == "/api/sessions/switch":
            from .api import sessions as _sessions_api
            status, payload = _sessions_api.post_sessions_switch(body, _session_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/sessions/delete":
            from .api import sessions as _sessions_api
            status, payload = _sessions_api.post_sessions_delete(body, _session_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/sessions/clear":
            from .api import sessions as _sessions_api
            status, payload = _sessions_api.post_sessions_clear(_session_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/sessions/archive":
            from .api import sessions as _sessions_api
            status, payload = _sessions_api.post_sessions_archive(body, _session_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/sessions/rename":
            from .api import sessions as _sessions_api
            status, payload = _sessions_api.post_sessions_rename(body, _session_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/chat/stash":
            from .api import session_control as _sc_api
            status, payload = _sc_api.post_chat_stash(
                body, _session_control_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/session/interrupt":
            sid = (body.get("session_id") or "").strip()
            if not sid:
                try:
                    qs = parse_qs(urlparse(self.path).query)
                    sid = (qs.get("session_id") or [""])[0].strip()
                except Exception:
                    sid = ""
            from .api import session_control as _sc_api
            status, payload = _sc_api.post_session_interrupt(
                body, sid, _session_control_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/session/rewind":
            from .api import session_control as _sc_api
            status, payload = _sc_api.post_session_rewind(
                body, _session_control_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/session/rewind/restore":
            from .api import session_control as _sc_api
            status, payload = _sc_api.post_session_rewind_restore(
                _session_control_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/session/steer":
            from .api import session_control as _sc_api
            status, payload = _sc_api.post_session_steer(
                body, _session_control_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/session/queue":
            from .api import session_control as _sc_api
            status, payload = _sc_api.post_session_queue(
                body, _session_control_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/session/queue/reorder":
            from .api import session_control as _sc_api
            status, payload = _sc_api.post_session_queue_reorder(
                body, _session_control_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/terminal/create":
            from .api import terminals as _term_api
            status, payload = _term_api.post_terminal_create(body, _terminal_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/terminal/write":
            from .api import terminals as _term_api
            status, payload = _term_api.post_terminal_write(body, _terminal_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/terminal/resize":
            from .api import terminals as _term_api
            status, payload = _term_api.post_terminal_resize(body, _terminal_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/terminal/kill":
            from .api import terminals as _term_api
            status, payload = _term_api.post_terminal_kill(body, _terminal_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/wiki/config":
            from .api import wiki as _wiki_api
            status, payload = _wiki_api.post_wiki_config(body)
            return self._send(status, json.dumps(payload))
        if path == "/api/wiki/disconnect":
            from .api import wiki as _wiki_api
            status, payload = _wiki_api.post_wiki_disconnect()
            return self._send(status, json.dumps(payload))
        if path == "/api/bedrock":
            from .api import platform as _plat_api
            status, payload = _plat_api.post_bedrock(body)
            return self._send(status, json.dumps(payload))
        if path == "/api/auth/pools":
            from .api import auth as _auth_api
            status, payload = _auth_api.post_auth_pools(body)
            return self._send(status, json.dumps(payload))
        if path == "/api/auth/pools/add":
            from .api import auth as _auth_api
            status, payload = _auth_api.post_auth_pools_add(body, _provider_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/auth/pools/remove":
            from .api import auth as _auth_api
            status, payload = _auth_api.post_auth_pools_remove(body)
            return self._send(status, json.dumps(payload))
        if path == "/api/auth/pools/strategy":
            from .api import auth as _auth_api
            status, payload = _auth_api.post_auth_pools_strategy(body)
            return self._send(status, json.dumps(payload))
        if path == "/api/auth/pools/reset":
            from .api import auth as _auth_api
            status, payload = _auth_api.post_auth_pools_reset(body)
            return self._send(status, json.dumps(payload))
        if path == "/api/auth/oauth/start":
            from .api import auth as _auth_api
            status, payload = _auth_api.post_auth_oauth_start(body)
            return self._send(status, json.dumps(payload))
        if path == "/api/auth/oauth/poll":
            from .api import auth as _auth_api
            status, payload = _auth_api.post_auth_oauth_poll(body)
            return self._send(status, json.dumps(payload))
        if path == "/api/auth/oauth/complete":
            from .api import auth as _auth_api
            status, payload = _auth_api.post_auth_oauth_complete(body)
            return self._send(status, json.dumps(payload))
        if path == "/api/auth/oauth/cancel":
            from .api import auth as _auth_api
            status, payload = _auth_api.post_auth_oauth_cancel(body)
            return self._send(status, json.dumps(payload))
        if path == "/api/auth/cursor-cli/status":
            from .api import auth as _auth_api
            status, payload = _auth_api.post_auth_cursor_cli_status(body)
            return self._send(status, json.dumps(payload))
        if path == "/api/auth/cursor-cli/login":
            from .api import auth as _auth_api
            status, payload = _auth_api.post_auth_cursor_cli_login(
                body, _provider_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/auth/cursor-cli/trust":
            from .api import auth as _auth_api
            status, payload = _auth_api.post_auth_cursor_cli_trust(
                body, _provider_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/auth/cursor-cli/logout":
            from .api import auth as _auth_api
            status, payload = _auth_api.post_auth_cursor_cli_logout()
            return self._send(status, json.dumps(payload))
        if path == "/api/auth/cursor-cli/models":
            from .api import auth as _auth_api
            status, payload = _auth_api.post_auth_cursor_cli_models()
            return self._send(status, json.dumps(payload))
        if path == "/api/wiki/handoff":
            # Mint a one-shot nonce and return a setup URL that carries a
            # loopback return target. Prefer this over marionette:// so Windows
            # never opens the Microsoft Store for an unregistered protocol.
            # Host gate stays here; nonce + URL assembly live in api.wiki.
            host = self.headers.get("Host", "") or ""
            if not _host_ok(host):
                return self._send(400, json.dumps({"error": "bad host"}))
            from .api import wiki as _wiki_api
            status, payload = _wiki_api.post_wiki_handoff(host)
            return self._send(status, json.dumps(payload))
        if path == "/api/git/connect":
            from .api import git as _git_api
            status, payload = _git_api.post_git_connect(body)
            return self._send(status, json.dumps(payload))
        if path == "/api/git/device/poll":
            from .api import git as _git_api
            status, payload = _git_api.post_git_device_poll(body)
            return self._send(status, json.dumps(payload))
        if path == "/api/git/disconnect":
            from .api import git as _git_api
            status, payload = _git_api.post_git_disconnect()
            return self._send(status, json.dumps(payload))
        if path == "/api/platform":
            from .api import platform as _plat_api
            status, payload = _plat_api.post_platform(body, _platform_services())
            return self._send(status, json.dumps(payload))
        if path == "/api/settings":
            from .api import settings as _settings_api
            status, payload = _settings_api.post_settings(
                body, _settings_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/providers/probe":
            from .api import providers as _prov_api
            status, payload = _prov_api.post_providers_probe(body)
            return self._send(status, json.dumps(payload))

        if path == "/api/providers/key":
            from .api import providers as _prov_api
            status, payload = _prov_api.post_providers_key(body, _provider_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/registry":
            from .api import registry as _reg_api
            status, payload = _reg_api.post_registry(body)
            return self._send(status, json.dumps(payload))

        if path == "/api/roles":
            from .api import registry as _reg_api
            status, payload = _reg_api.post_roles(body, _registry_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/pilot/validate":
            from .api import registry as _reg_api
            status, payload = _reg_api.post_pilot_validate(body)
            return self._send(status, json.dumps(payload))

        if path == "/api/worktrees/add":
            from .api import worktrees as _wt_api
            status, payload = _wt_api.post_worktrees_add(body, _worktree_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/worktrees/remove":
            from .api import worktrees as _wt_api
            status, payload = _wt_api.post_worktrees_remove(body, _worktree_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/worktrees/prune":
            from .api import worktrees as _wt_api
            status, payload = _wt_api.post_worktrees_prune(_worktree_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/worktrees/prune-edit-branches":
            from .api import worktrees as _wt_api
            status, payload = _wt_api.post_worktrees_prune_edit_branches(
                _worktree_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/worktrees/max":
            from .api import worktrees as _wt_api
            status, payload = _wt_api.post_worktrees_max(body, _worktree_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/hooks/add":
            from .api import hooks as _hooks_api
            status, payload = _hooks_api.post_hooks_add(body)
            return self._send(status, json.dumps(payload))

        if path == "/api/hooks/update":
            from .api import hooks as _hooks_api
            status, payload = _hooks_api.post_hooks_update(
                body, _hooks_services())
            return self._send(status, json.dumps(payload))

        if path == "/api/hooks/remove":
            from .api import hooks as _hooks_api
            status, payload = _hooks_api.post_hooks_remove(body)
            return self._send(status, json.dumps(payload))

        return self._send(404, json.dumps({"error": "not found"}))

    def _handle_upload(self):
        from .api import files as _files_api
        ctype = self.headers.get("Content-Type", "")
        try:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            content_length = 0
        # Size/ctype gate stays before rfile.read (DoS); parse+save in api.files.
        early = _files_api.check_upload_request(ctype, content_length)
        if early is not None:
            return self._send(early[0], json.dumps(early[1]))
        body = self.rfile.read(content_length)
        status, payload = _files_api.save_upload(body, ctype, _UPLOAD_DIR)
        return self._send(status, json.dumps(payload))

    # GET endpoints that are intentionally public (the same-origin renderer
    # bootstrap assets, which must load BEFORE the page has the token to make
    # authenticated calls). Everything else under /api requires the token.
    _PUBLIC_GET_PATHS = frozenset({"/", "/index.html", "/app.js", "/app.css"})

    def do_GET(self):
        global _codegraph_status, _codegraph_status_reason
        global _codegraph_preflight, _codegraph_suggested_action
        u = urlparse(self.path)
        # Loopback wiki handoff: browser navigates here with a one-shot nonce
        # (no harness token). Must run before the centralized auth gate.
        if u.path == "/api/wiki/connect":
            return self._handle_wiki_connect(u)
        # CENTRALIZED AUTH GATE (defense against per-handler drift): do_POST has
        # a single token check at its top, but do_GET historically required each
        # handler to re-add a copy-pasted token check -- and ~11 endpoints
        # (/api/memory, /api/config, /api/skills, /api/rules, /api/commands,
        # /api/settings, /api/platform, /api/jobs, /api/workspace, /api/mcp*)
        # were left unauthenticated, leaking durable memory/config/skills to any
        # local caller with no token. Gate every non-public path here so a newly
        # added GET endpoint is authenticated by default; the redundant
        # per-handler checks below are now harmless no-ops.
        if u.path not in self._PUBLIC_GET_PATHS:
            if self._guard():
                return
            if not self._token_ok():
                return self._send(403, json.dumps({"error": "missing or bad token"}))
        if u.path in ("/", "/index.html"):
            html = (_WEB / "index.html").read_text()
            # inject the auth token so the same-origin page can call the API
            meta = '<meta name="harness-token" content="%s">' % _TOKEN
            html = html.replace("</head>", meta + "</head>", 1) if "</head>" in html else meta + html
            return self._send(200, html, "text/html")
        if u.path == "/app.js":
            return self._send(200, (_WEB / "app.js").read_text(),
                              "application/javascript")
        if u.path == "/app.css":
            return self._send(200, (_WEB / "app.css").read_text(), "text/css")
        if u.path == "/api/git/status":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            qargs = parse_qs(u.query)
            from .api import git as _git_api
            status, payload = _git_api.get_git_status(
                qargs.get("repo", [""])[0], _git_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/git/branches":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            qargs = parse_qs(u.query)
            from .api import git as _git_api
            status, payload = _git_api.get_git_branches(
                qargs.get("repo", [""])[0], _git_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/git/diff":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            qargs = parse_qs(u.query)
            staged = qargs.get("staged", ["0"])[0].strip().lower() in ("1", "true", "yes")
            from .api import git as _git_api
            status, payload = _git_api.get_git_diff(
                qargs.get("repo", [""])[0],
                qargs.get("file", [""])[0],
                staged,
                _git_services(),
            )
            return self._send(status, json.dumps(payload))
        if u.path == "/api/session/state":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import session_control as _sc_api
            status, payload = _sc_api.get_session_state(_session_control_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/session/context_at":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            try:
                turn = int(parse_qs(u.query).get("turn", ["0"])[0])
            except (TypeError, ValueError):
                return self._send(400, json.dumps({"error": "turn must be an integer"}))
            from .api import session_control as _sc_api
            status, payload = _sc_api.get_session_context_at(
                turn, _session_control_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/session/swarm-results":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import session_control as _sc_api
            status, payload = _sc_api.get_session_swarm_results(
                _session_control_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/session/queue":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import session_control as _sc_api
            status, payload = _sc_api.get_session_queue(_session_control_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/checkpoints":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import checkpoints as _ckpt_api
            status, payload = _ckpt_api.get_checkpoints(_checkpoint_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/checkpoints/diff":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import checkpoints as _ckpt_api
            status, payload = _ckpt_api.get_checkpoints_diff(
                parse_qs(u.query).get("id", [""])[0],
                _checkpoint_services(),
            )
            return self._send(status, json.dumps(payload))
        if u.path == "/api/mcp":
            from .api import mcp as _mcp_api
            status, payload = _mcp_api.get_mcp(_mcp_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/mcp/catalog":
            from .api import mcp as _mcp_api
            status, payload = _mcp_api.get_mcp_catalog()
            return self._send(status, json.dumps(payload))
        if u.path == "/api/commands":
            from .api import commands as _cmd_api
            status, payload = _cmd_api.get_commands(
                parse_qs(u.query).get("repo", [""])[0],
                _commands_services(),
            )
            return self._send(status, json.dumps(payload))
        if u.path == "/api/skills":
            from .api import skills as _skills_api
            status, payload = _skills_api.get_skills(_skills_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/rules":
            from .api import skills as _skills_api
            status, payload = _skills_api.get_rules(_skills_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/memory":
            from .api import skills as _skills_api
            status, payload = _skills_api.get_memory(_skills_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/file/read":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import files as _files_api
            rel_path = parse_qs(u.query).get("path", [""])[0]
            status, payload = _files_api.get_file_read(rel_path, _file_services())
            return self._send(status, json.dumps(payload))

        if u.path == "/api/file/raw":
            # Authenticated bytes for PDF/image/HTML preview — body in api.files.
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import files as _files_api
            rel_path = parse_qs(u.query).get("path", [""])[0]
            status, body_or_err, ctype = _files_api.get_file_raw(
                rel_path, _file_services())
            if isinstance(body_or_err, dict):
                return self._send(status, json.dumps(body_or_err))
            return self._send(status, body_or_err, ctype)

        if u.path == "/api/image":
            from .api import files as _files_api
            req_path = parse_qs(u.query).get("path", [""])[0]
            status, body_or_err, ctype = _files_api.get_image(req_path, _UPLOAD_DIR)
            if isinstance(body_or_err, dict):
                return self._send(status, json.dumps(body_or_err))
            return self._send(status, body_or_err, ctype)

        if u.path == "/api/workspace/files":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import files as _files_api
            status, payload = _files_api.get_workspace_files(_file_services())
            return self._send(status, json.dumps(payload))

        if u.path == "/api/workspace/symbols":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import workspace as _ws_api
            status, payload = _ws_api.get_workspace_symbols(
                parse_qs(u.query).get("q", [""])[0],
                _workspace_services(),
            )
            return self._send(status, json.dumps(payload))
        if u.path == "/api/workspace":
            from .api import workspace as _ws_api
            status, payload = _ws_api.get_workspace(_workspace_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/models/catalog":
            if self._guard():
                return
            q = parse_qs(u.query)
            qtok = q.get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import providers as _prov_api
            force = (q.get("refresh", [""])[0] or "").strip().lower() in ("1", "true", "yes")
            status, payload = _prov_api.get_models_catalog(force=force)
            return self._send(status, json.dumps(payload))
        if u.path == "/api/codegraph":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import codegraph as _cg_api
            status, payload = _cg_api.get_codegraph(_codegraph_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/config":
            from .api import settings as _settings_api
            status, payload = _settings_api.get_config(_settings_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/wiki/config":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import wiki as _wiki_api
            status, payload = _wiki_api.get_wiki_config_payload()
            return self._send(status, json.dumps(payload))
        if u.path == "/api/bedrock":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import platform as _plat_api
            status, payload = _plat_api.get_bedrock()
            return self._send(status, json.dumps(payload))
        if u.path == "/api/auth/pools":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import auth as _auth_api
            qs = parse_qs(u.query)
            pname = (qs.get("provider") or [""])[0].strip()
            status, payload = _auth_api.get_auth_pools(provider=pname)
            return self._send(status, json.dumps(payload))
        if u.path == "/api/wiki/graph":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            # WikiClient / cache / status extras live in harness.api.wiki.
            from .api import wiki as _wiki_api
            status, payload = _wiki_api.get_wiki_graph(_wiki_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/wiki/status":
            # Lightweight summary for the State pane strip -- counts only, no
            # full node/edge arrays. Reuses the same graph cache as /api/wiki/graph.
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import wiki as _wiki_api
            status, payload = _wiki_api.get_wiki_status(_wiki_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/settings":
            from .api import settings as _settings_api
            status, payload = _settings_api.get_settings(_settings_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/reviews":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import reviews as _rev_api
            status, payload = _rev_api.get_reviews(_review_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/platform":
            from .api import platform as _plat_api
            status, payload = _plat_api.get_platform(_platform_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/jobs":
            from .api import jobs as _jobs_api
            q = parse_qs(u.query)
            repo_override = q.get("repo", [""])[0]
            status, payload = _jobs_api.get_jobs(repo_override or None, _job_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/usage":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            repo_override = parse_qs(u.query).get("repo", [""])[0]
            from .api import usage as _usage_api
            status, payload = _usage_api.get_usage(repo_override, _usage_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/artifacts":
            from .api import jobs as _jobs_api
            q = parse_qs(u.query)
            jid = q.get("job_id", [""])[0]
            # Dual-store resolve (harness then CLI) lives in harness.api.jobs.
            status, payload = _jobs_api.get_artifacts(jid, _job_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/swarm/live":
            if self._guard():
                return
            q = parse_qs(u.query)
            qtok = q.get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import jobs as _jobs_api
            repo_override = q.get("repo", [""])[0]
            status, payload = _jobs_api.get_swarm_live(repo_override or None, _job_services())
            return self._send(status, json.dumps(payload))
        # action endpoints (SSE) mutate state / spend budget -> guard them.
        if u.path in ("/api/run", "/api/chat", "/api/auto", "/api/pilot", "/api/sessions/transcript", "/api/sessions/export",
                      "/api/providers", "/api/registry", "/api/roles", "/api/registry/recommend", "/api/context/usage"):
            if self._guard():
                return
            from urllib.parse import parse_qs as _pq
            qtok = _pq(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))

        if u.path == "/api/providers":
            from .api import providers as _prov_api
            status, payload = _prov_api.get_providers()
            return self._send(status, json.dumps(payload))

        if u.path == "/api/registry":
            from .api import registry as _reg_api
            status, payload = _reg_api.get_registry()
            # On-disk file content is already JSON text; empty default is a dict.
            if isinstance(payload, str):
                return self._send(status, payload)
            return self._send(status, json.dumps(payload))

        if u.path == "/api/roles":
            from .api import registry as _reg_api
            status, payload = _reg_api.get_roles(_registry_services())
            return self._send(status, json.dumps(payload))

        if u.path == "/api/registry/recommend":
            from .api import registry as _reg_api
            status, payload = _reg_api.get_registry_recommend()
            return self._send(status, json.dumps(payload))
        if u.path == "/api/run":
            q = parse_qs(u.query)
            from .api.streams import validate_upload_image_paths
            imgs, err = validate_upload_image_paths(
                q.get("images", [""])[0], _UPLOAD_DIR
            )
            if err is not None:
                return self._send(err[0], json.dumps(err[1]))
            return self._stream_run(q.get("prompt", [""])[0], imgs)
        if u.path == "/api/chat":
            q = parse_qs(u.query)
            # A stashed message (see POST /api/chat/stash) takes precedence: it
            # exists precisely because the real message/images were too big for
            # this URL. Falls back to the query-param message for small chats,
            # keeping today's behavior unchanged when no ?mid= is present.
            from .api.streams import (
                resolve_stashed_chat_message,
                validate_upload_image_paths,
            )
            message, raw_images = resolve_stashed_chat_message(
                q.get("mid", [""])[0],
                q.get("message", [""])[0],
                q.get("images", [""])[0],
                _stash_pop,
            )
            imgs, err = validate_upload_image_paths(raw_images, _UPLOAD_DIR)
            if err is not None:
                return self._send(err[0], json.dumps(err[1]))
            plan_val = q.get("plan", ["false"])[0].lower() in ("true", "1", "yes")
            resume_val = q.get("resume", ["false"])[0].lower() in ("true", "1", "yes")
            return self._stream_chat(message, imgs, plan=plan_val, resume=resume_val)
        if u.path == "/api/chat/events":
            # Mid-turn reattach replay: return retained SSE frames since ``since``.
            if self._guard():
                return
            q = parse_qs(u.query)
            qtok = q.get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .api import sse as _sse_api
            since_raw = q.get("since", ["0"])[0]
            try:
                since_c = int(since_raw or 0)
            except (TypeError, ValueError):
                since_c = 0
            gen_raw = q.get("generation", [""])[0]
            generation = None
            if gen_raw not in ("", None):
                try:
                    generation = int(gen_raw)
                except (TypeError, ValueError):
                    return self._send(400, json.dumps({"error": "generation must be an integer"}))
            status, payload = _sse_api.get_chat_events(
                _sse_services(),
                (q.get("session", [""])[0] or "").strip(),
                since_c,
                generation,
            )
            return self._send(status, json.dumps(payload))
        if u.path == "/api/terminal/stream":
            q = parse_qs(u.query)
            return self._stream_terminal(q.get("id", [""])[0])
        if u.path == "/api/pilot":
            q = parse_qs(u.query)
            return self._swap_pilot(q.get("model", [""])[0])
        if u.path == "/api/context/usage":
            from .api import usage as _usage_api
            status, payload = _usage_api.get_context_usage(_usage_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/workspaces":
            from .api import workspace as _ws_api
            status, payload = _ws_api.get_workspaces(_workspace_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/worktrees":
            from .api import worktrees as _wt_api
            status, payload = _wt_api.get_worktrees(_worktree_services())
            return self._send(status, json.dumps(payload))
        if u.path == "/api/hooks":
            from .api import hooks as _hooks_api
            status, payload = _hooks_api.get_hooks()
            return self._send(status, json.dumps(payload))
        if u.path == "/api/sessions/transcript":
            from .api import sessions as _sessions_api
            status, payload = _sessions_api.get_sessions_transcript(
                parse_qs(u.query), _session_services()
            )
            return self._send(status, json.dumps(payload))
        if u.path == "/api/sessions/export":
            from .api import sessions as _sessions_api
            return _sessions_api.write_sessions_export(
                self, parse_qs(u.query), _session_services()
            )
        if u.path == "/api/sessions":
            from .api import sessions as _sessions_api
            status, payload = _sessions_api.get_sessions_list(
                parse_qs(u.query), _session_services()
            )
            return self._send(status, json.dumps(payload))
        if u.path == "/api/sessions/bank":
            from .api import sessions as _sessions_api
            status, payload = _sessions_api.get_sessions_bank(
                parse_qs(u.query), _session_services()
            )
            return self._send(status, json.dumps(payload))
        if u.path == "/api/sessions/search":
            from .api import sessions as _sessions_api
            status, payload = _sessions_api.get_sessions_search(
                parse_qs(u.query), _session_services()
            )
            return self._send(status, json.dumps(payload))
        if u.path == "/api/auto":
            q = parse_qs(u.query)
            objective = q.get("objective", [""])[0]
            mid = q.get("mid", [""])[0]
            if mid:
                stashed = _stash_pop(mid)
                if stashed is not None:
                    objective = stashed.get("message", "")
            return self._stream_auto(objective)
        return self._send(404, json.dumps({"error": "not found"}))

    def _sse_write(self, payload: bytes) -> bool:
        """Write one SSE frame. Returns False if the client has detached."""
        return sse_write(self.wfile, payload)

    def _sse_pump(self, gen, frame_for_event, *, on_event=None, write_done: bool = True,
                  ring: Optional[SseEventRing] = None) -> bool:
        """Pump a turn generator over SSE with Hermes-style detach semantics."""
        return sse_pump(
            self.wfile,
            gen,
            frame_for_event,
            on_event=on_event,
            write_done=write_done,
            ring=ring,
        )

    def _stream_run(self, prompt: str, images=None):
        from .api.streams import stream_run
        return stream_run(self, prompt, images, _stream_services())

    def _stream_auto(self, objective: str):
        """Stream the fully-auto loop (governor-bounded) over SSE."""
        from .api.streams import stream_auto
        return stream_auto(self, objective, _stream_services())

    def _swap_pilot(self, model: str):
        """Hot-swap the pilot model (the whole point: your key -> your pilot).

        Body lives in ``harness.api.pilot``; this wrapper injects live globals.
        Hermes-style mid-turn deferral and idle rebuild semantics are unchanged.
        """
        from .api.pilot import get_pilot_swap
        status, payload = get_pilot_swap(model, _pilot_services())
        return self._send(status, json.dumps(payload))

    def _stream_terminal(self, sid: str):
        """Stream PTY output over SSE. Client sends keystrokes via POST /api/terminal/write."""
        from .api.terminals import stream_terminal
        return stream_terminal(self, sid, _terminal_services())

    def _stream_chat(self, message: str, images=None, plan: bool = False, resume: bool = False):
        """Stream the conversational PILOT loop over SSE."""
        from .api.streams import stream_chat
        return stream_chat(
            self, message, images, _stream_services(), plan=plan, resume=resume,
        )


def _checkpoint_transcript(ctx=None) -> None:
    """Persist the turn's transcript mid-stream so a hard crash before
    _finalize_turn() doesn't lose the in-flight turn. Mirrors the transcript step
    of _finalize_turn (no postRun hooks) and is fully exception-isolated: it must
    never break the SSE stream or take the handler thread down.

    Prefer the turn-bound session_id/pilot from ``ctx`` (captured at stream start)
    so a mid-turn view switch cannot overwrite the newly active session's file.
    """
    try:
        sid = ""
        pilot = None
        if ctx:
            sid = (ctx.get("session_id") or "") if isinstance(ctx, dict) else ""
            pilot = ctx.get("pilot") if isinstance(ctx, dict) else None
        if not pilot:
            pilot = _pilot
        if not sid:
            sid = getattr(pilot, "harness_session_id", "") or (_sessions.active or "")
        if sid and pilot is not None:
            save_transcript(_cfg.state_dir or _tf.gettempdir(),
                            sid, pilot.export_transcript_data())
    except Exception as e:
        import sys
        print(f"[transcript checkpoint error] {e!r}", file=sys.stderr)


def _finalize_turn(ctx) -> None:
    """End-of-turn bookkeeping (post-run hooks + transcript persist) with each step
    isolated so a failure in one cannot break the streaming response or take the
    request handler thread down. The turn is already over for the client when the
    stream ends; a serialization error in export_transcript_data() or a misbehaving
    hook must be logged, never propagated. This is the finish-path hardening for the
    "backend dies right when the response finishes" class of failure.

    Prefer turn-bound session_id/pilot from ``ctx`` so a mid-turn view switch
    cannot overwrite the newly active session's transcript.
    """
    try:
        from .hooks import run_hooks
        run_hooks("postRun", ctx)
    except Exception as e:
        import sys
        print(f"[postRun hook error] {e!r}", file=sys.stderr)
    try:
        sid = ""
        pilot = None
        if ctx and isinstance(ctx, dict):
            sid = ctx.get("session_id") or ""
            pilot = ctx.get("pilot")
        if not pilot:
            pilot = _pilot
        if not sid:
            sid = getattr(pilot, "harness_session_id", "") or (_sessions.active or "")
        if sid and pilot is not None:
            save_transcript(_cfg.state_dir or _tf.gettempdir(),
                            sid, pilot.export_transcript_data())
    except Exception as e:
        import sys
        print(f"[transcript persist error] {e!r}", file=sys.stderr)


def _retry_on_locked(read, attempts: int = 3, delay: float = 0.15):
    """Run a store read, retrying briefly on SQLite 'database is locked'.

    Windows raises these transient lock errors far more readily than macOS
    (concurrent swarm workers + CodeGraph indexer + usage polling on one
    SQLite file), so cost/token endpoints retry instead of erroring out.
    """
    import sqlite3
    import time as _t
    for attempt in range(attempts):
        try:
            return read()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < attempts - 1:
                _t.sleep(delay)
                continue
            raise
    return read()


_last_jobs_snapshot: list = []


def _jobs_snapshot() -> list:
    """List jobs with resilience to a transient SQLite 'database is locked'. A
    brief lock (e.g. a lingering second backend during a relaunch) must not 500
    the jobs poll and disconnect the UI -- retry briefly, then fall back to the
    last good snapshot so the panel holds steady instead of erroring out."""
    global _last_jobs_snapshot
    import sqlite3
    import time as _t
    for attempt in range(3):
        try:
            jobs = _session.state().list_jobs()
            _last_jobs_snapshot = jobs
            return jobs
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 2:
                _t.sleep(0.15)
                continue
            import sys
            print(f"[jobs poll degraded] {e!r} -- serving last-known "
                  f"({len(_last_jobs_snapshot)})", file=sys.stderr)
            return _last_jobs_snapshot
        except Exception as e:
            import sys
            print(f"[jobs poll error] {e!r} -- serving last-known", file=sys.stderr)
            return _last_jobs_snapshot
    return _last_jobs_snapshot


def _scoped_jobs_with_stores(repo_root: str | None = None) -> tuple[list, Any, Any | None]:
    """Visible jobs plus harness and optional CLI stores for bulk reads."""
    from .cli_job_merge import merge_scoped_cli_jobs
    from .job_scoping import filter_store_jobs

    jobs = _jobs_snapshot()
    try:
        store = _session.state().store
    except Exception:
        return jobs, None, None
    effective_repo = (repo_root or "").strip() or (_cfg.repo or "")
    workspace_root = effective_repo or os.getcwd()
    active_session_id = _sessions.active or getattr(_pilot, "harness_session_id", "") or ""
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

    When ``repo_root`` is present and non-empty it overrides ``_cfg.repo`` for
    legacy (unstamped) cwd filtering; stamped session jobs are unchanged.
    Merges read-only CLI jobs from the Puppetmaster per-project store when
    present (tagged ``source``: ``harness`` or ``cli``).
    """
    jobs, _, _ = _scoped_jobs_with_stores(repo_root)
    return jobs


def _pilot_preflight():
    return _session.preflight()


def _available_pilots():
    """The pilot picker's model list: the user's ENABLED set (curated in
    Settings -> Models), filtered to providers that currently have a key and are
    not disconnected. The Settings tab is the curation surface -- it shows the
    FULL live catalog (incl. newly released models like gpt-5.5) as toggles; the
    picker shows only what is toggled on there, so the two always agree.

    The current driver is forced first when it is still in the enabled set so
    the picker shows it selected. A stale compiled-in default (e.g. qwen) that
    the user never toggled on is NOT injected — that made the composer look
    like it was on a model that could not run.
    """
    from . import model_visibility as _mv
    cur = _cfg.driver
    pilots = _mv.enabled_pilots()
    curated = _mv.get_enabled()
    cur_allowed = False
    if cur:
        if cur in pilots:
            cur_allowed = True
        elif curated and _driver_in_enabled_set(cur, curated):
            cur_allowed = True
        elif not curated:
            # No curation yet: full available set — keep current first if present.
            cur_allowed = cur in pilots or _driver_in_enabled_set(cur, pilots)
    if cur_allowed:
        ordered = [cur] + [p for p in pilots if p != cur]
    else:
        ordered = list(pilots)
    # De-dup while preserving order.
    seen = set()
    out = []
    for s in ordered:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out or ([cur] if cur else [])


def _get_settings_dict():
    from harness.hash_edit import hash_edit_enabled
    from harness.reasoning_effort import current_reasoning_effort

    reach = _cfg.reach
    status = get_api_key_status(reach)
    preflight_ok = (_session.preflight() is None)
    return {
        "driver": _cfg.driver,
        "reach": reach,
        "budget": _cfg.budget,
        "models": _available_pilots(),
        "auto_distill": getattr(_pilot, "_auto_distill", False),
        "reviewEditsBeforeApply": getattr(_pilot, "_review_edits_before_apply", False),
        "wiki_auto": getattr(_cfg, "wiki_auto", False),
        "autoVerify": getattr(_cfg, "auto_verify", True),
        "verifyCommand": getattr(_cfg, "verify_command", ""),
        "autoCommandGuard": getattr(_pilot, "_auto_command_guard", True),
        "hash_edit_enabled": hash_edit_enabled(),
        "commandTimeout": (os.environ.get("HARNESS_COMMAND_TIMEOUT", "").strip() or "120"),
        "maxPilotSteps": (os.environ.get("HARNESS_MAX_PILOT_STEPS", "").strip() or "40"),
        "reasoning_effort": current_reasoning_effort(),
        "state_dir": _session.state_dir,
        "repo": _cfg.repo,
        "has_api_key": status["has_key"],
        "api_key_masked": status["masked"],
        "masked": status["masked"],
        "key_env_var": get_env_var_for_reach(reach),
        "preflight_ok": preflight_ok,
        "bedrock": get_bedrock_status(),
    }


_startup_index_fired = False


def _maybe_auto_index_codegraph():
    global _startup_index_fired, _codegraph_status, _codegraph_status_reason
    if _startup_index_fired:
        return
    _startup_index_fired = True

    repo = _cfg.repo
    if repo and os.path.isdir(repo):
        if not _puppetmaster_available():
            _codegraph_status = "unsupported"
            _codegraph_status_reason = "puppetmaster not found -- codegraph/swarm unavailable"
            return
        
        if _codegraph_indexed(repo):
            _codegraph_status = "ready"
        else:
            # No built DB yet (fresh checkout, or init'd-but-never-indexed):
            # build it in the background so the panel comes up ready without a
            # manual re-index. `_index_codegraph_bg` runs `codegraph init --index`.
            def target():
                _index_codegraph_bg(repo)
            t = threading.Thread(target=target, daemon=True)
            t.start()


def _cleanup_marker(marker_path: str, pid: int) -> None:
    try:
        if os.path.exists(marker_path):
            with open(marker_path, "r", encoding="utf-8") as f:
                m = json.load(f)
            if m and isinstance(m, dict) and m.get("pid") == pid:
                os.remove(marker_path)
    except Exception:
        pass


def serve(host: str = "127.0.0.1", port: int = 8799, force: bool = False) -> None:
    import errno
    import sys
    import urllib.request
    import urllib.error
    import time
    import atexit

    # Force line-buffered stdout/stderr. The packaged PyInstaller backend does not
    # honor PYTHONUNBUFFERED, so its output (including crash tracebacks) sat in a
    # pipe buffer and was LOST when the process exited -- which made backend deaths
    # invisible in the desktop app's log. Line buffering flushes every line to the
    # Electron [out]/[err] pipes in real time so failures are actually captured.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(line_buffering=True)
        except Exception:
            pass

    marker_dir = _state_home()
    marker_path = os.path.join(marker_dir, "backend.json")

    if not force:
        try:
            if os.path.exists(marker_path):
                with open(marker_path, "r", encoding="utf-8") as f:
                    m = json.load(f)
                if m and isinstance(m, dict) and m.get("port"):
                    m_port = m["port"]
                    try:
                        url = f"http://127.0.0.1:{m_port}/api/config"
                        with urllib.request.urlopen(url, timeout=2.0) as resp:
                            if resp.status == 200:
                                print(f"pm-harness already running at http://{host}:{m_port} — reusing")
                                return
                    except urllib.error.HTTPError as he:
                        # A live server that answers with an HTTP status (e.g. 403
                        # from the auth gate on /api/config) is DEFINITELY running
                        # -- the probe carries no token by design. Any HTTP
                        # response, including 403, proves reuse; treat it as alive.
                        if getattr(he, "code", 0):
                            print(f"pm-harness already running at http://{host}:{m_port} — reusing")
                            return
                    except Exception:
                        # Connection refused / unreachable -> stale marker, fall
                        # through to bind a fresh server below.
                        pass
        except Exception:
            pass

    # allow quick restarts without TIME_WAIT blocking the bind. POSIX-only:
    # on Windows SO_REUSEADDR means "two live sockets may bind the same port",
    # which silently defeats the already-in-use guard (EADDRINUSE never fires)
    # and lets a second backend hijack the first one's port.
    ThreadingHTTPServer.allow_reuse_address = os.name == "posix"

    # Cap concurrent request threads. ThreadingHTTPServer is thread-per-request
    # with NO ceiling, so a burst of slow requests (e.g. many hung provider
    # calls) could fan out into unbounded threads and exhaust the process. A
    # bounded semaphore acquired before each handler thread turns that into
    # backpressure: excess connections wait in the accept queue instead.
    _max_workers = int(os.environ.get("HARNESS_MAX_WORKERS", "64"))

    class _HarnessServer(ThreadingHTTPServer):
        daemon_threads = True  # handler threads never block process shutdown
        _worker_slots = threading.BoundedSemaphore(_max_workers)

        def process_request(self, request, client_address):
            # Acquire in the accept loop so we block accepting new work when at
            # capacity; the slot is released when the handler thread finishes.
            self._worker_slots.acquire()
            super().process_request(request, client_address)

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self._worker_slots.release()

        def handle_error(self, request, client_address):
            # The renderer closing a socket mid-request (navigating away, stopping
            # a stream, swapping models) raises ConnectionResetError/BrokenPipeError
            # deep in socketserver. That is benign -- suppress the per-request
            # traceback that otherwise floods ~/.pmharness/electron.log and buries
            # real errors. Anything else still gets a full traceback.
            exc = sys.exc_info()[1]
            if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
                return
            import traceback
            traceback.print_exc()

    try:
        srv = _HarnessServer((host, port), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"pm-harness: port {port} is already in use. Another harness GUI "
                  f"may be running.\n  - open the existing one at http://{host}:{port}\n"
                  f"  - or pick another port: harness gui --port {port + 1}",
                  file=sys.stderr)
            raise SystemExit(2)
        raise

    port = srv.server_address[1]

    try:
        os.makedirs(marker_dir, exist_ok=True)
        with open(marker_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump({
                "port": port,
                "pid": os.getpid(),
                "at": int(time.time() * 1000)
            }, f)
    except Exception:
        pass

    print(f"pm-harness GUI on http://{host}:{port}  (driver={_cfg.driver})")
    # SECURITY/RESOURCE: ensure spawned MCP child processes are reaped on exit
    # (Ctrl-C, SIGTERM, SystemExit) instead of being orphaned.
    import signal
    atexit.register(_mcp.stop_all)
    atexit.register(_cleanup_marker, marker_path, os.getpid())

    def _graceful(signum, frame):
        try:
            _mcp.stop_all()
        finally:
            _cleanup_marker(marker_path, os.getpid())
            raise SystemExit(0)
    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(_sig, _graceful)
        except (ValueError, OSError):
            pass  # not on the main thread (e.g. under tests) -- atexit still covers it
    try:
        # Sync the agentic registry at startup so models.json reflects current keys
        from .auto_registry import sync_agentic_registry_safe
        sync_agentic_registry_safe()
        
        _maybe_auto_index_codegraph()
        # Connect configured MCP servers (incl. local Docker HTTP) without
        # blocking the GUI bind. Failures land on status().error for State→MCP.
        def _boot_mcp() -> None:
            try:
                report = _mcp.start_all()
                for _name, _res in report.items():
                    if isinstance(_res, str):
                        _diag("mcp.boot_error", name=_name, error=_res)
            except Exception as _e:
                _diag("mcp.boot_fail", error=str(_e))

        threading.Thread(target=_boot_mcp, name="mcp-boot", daemon=True).start()
        srv.serve_forever()
    except SystemExit:
        raise
    except BaseException:
        # Capture the real cause of an unexpected backend exit before it unwinds.
        # Without this the traceback could be swallowed and the desktop app would
        # only see the backend vanish. Flush explicitly in case buffering lingers.
        import traceback
        print("[backend FATAL] serve_forever exited abnormally:", file=sys.stderr)
        traceback.print_exc()
        try:
            sys.stderr.flush()
        except Exception:
            pass
        raise
    finally:
        _mcp.stop_all()
        _cleanup_marker(marker_path, os.getpid())


if __name__ == "__main__":
    import sys
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    serve(port=p)
