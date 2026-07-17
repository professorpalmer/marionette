"""Boot-usage meters, /api/usage response cache, and tool-savings fields.

Owns process-lifetime carry/persist/restore, the short-TTL usage response cache,
session stamped meters, and tool-output savings payloads for the status bar.
``harness.api.cost`` re-exports the historical surface (including mutable boot
scalars via write-through aliases).
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from .cost_accounting import (
    _cost_source_label,
    _resolve_active_prices,
    _resolve_prices_for_runner,
    _session_cost_split,
)
from .cost import _cfg, _diag, _pilot, _runners, _sessions, _server_attr
from .swarm_cost import _job_swarm_accounting

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
