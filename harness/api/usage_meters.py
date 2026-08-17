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


def _boot_usage_reset_for_tests() -> None:
    """Zero process-global boot carry so hermetic API tests do not inherit
    spend left by an earlier case (carry is priced into /api/usage but not
    into /api/swarm/live's pilot split -- suite order then flakes)."""
    global _BOOT_CARRY_COST_USD, _BOOT_PLAN_BILLING, _BOOT_USAGE_RESTORED
    _usage_cache_clear_for_tests()
    _BOOT_CARRY_COST_USD = 0.0
    _BOOT_PLAN_BILLING = False
    _BOOT_USAGE_RESTORED = False
    for attr in _BOOT_METER_ATTRS:
        _BOOT_METER_CARRY[attr] = 0.0
    _BOOT_PILOT_BY_MODEL.clear()
    _BOOT_REPOS.clear()


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
    "_worker_tokens_cached",
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
# Cumulative pilot spend locked to the model that incurred it. Fold writes
# here at fold-time rates; live runners are merged on read at each runner's
# bound ``config.driver`` — never the currently selected picker model.
_BOOT_PILOT_BY_MODEL: dict[str, dict] = {}
# Every workspace opened this process -- boot-pill swarm dollars merge
# epoch-windowed jobs across these repos, not only the active _cfg().repo.
_BOOT_REPOS: set[str] = set()
# Must be reentrant: fold_live=True holds this lock while folding runners,
# and each fold calls _persist_boot_usage(fold_live=False) which re-acquires.
# A plain Lock() self-deadlocks the restart path and wedges every /api/usage
# poll behind the same lock (status-bar spend freezes the HTTP server).
_BOOT_USAGE_PERSIST_LOCK = threading.RLock()
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
                "pilot_by_model": _pilot_slices_for_persist(),
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
            # Pre-_worker_tokens_cached carries already folded worker cache into
            # ``_tokens_cached``. Defaulting the missing key to 0 would make
            # ``_source_owned_cache_lanes`` re-add store-job cache on top
            # (e.g. 50k+20k→70k). Conservative peel: treat the whole restored
            # cache meter as worker-overlappable so lane math cannot inflate
            # tokens_cached above the process total when swarm is re-added.
            legacy_missing_worker_cached = (
                "_worker_tokens_cached" not in carry
                and float(carry.get("_worker_tokens_in", 0.0) or 0.0) > 0
            )
            for attr in _BOOT_METER_ATTRS:
                try:
                    _BOOT_METER_CARRY[attr] = float(carry.get(attr, 0.0) or 0.0)
                except Exception:
                    pass
            if legacy_missing_worker_cached:
                try:
                    _BOOT_METER_CARRY["_worker_tokens_cached"] = float(
                        _BOOT_METER_CARRY.get("_tokens_cached", 0.0) or 0.0
                    )
                except Exception:
                    _BOOT_METER_CARRY["_worker_tokens_cached"] = 0.0
        try:
            _BOOT_CARRY_COST_USD = float(data.get("carry_cost_usd", 0.0) or 0.0)
        except Exception:
            _BOOT_CARRY_COST_USD = 0.0
        try:
            _BOOT_PLAN_BILLING = bool(data.get("plan_billing", False))
        except Exception:
            _BOOT_PLAN_BILLING = False
        try:
            _restore_pilot_slices(
                data.get("pilot_by_model"),
                carry_cost=float(_BOOT_CARRY_COST_USD or 0.0),
            )
        except Exception:
            _BOOT_PILOT_BY_MODEL.clear()
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


def standing_economics_enabled() -> bool:
    """AGNT-inspired standing floor / cache-TTL fields (default off)."""
    raw = (os.environ.get("HARNESS_STANDING_ECONOMICS") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def prompt_cache_ttl_ms_for_driver(driver: str) -> Optional[int]:
    """Known prompt-cache TTL in ms — re-export from prompt_cache helpers."""
    from pmharness.drivers.prompt_cache import prompt_cache_ttl_ms_for_driver as _fn

    return _fn(driver)


def _standing_prefix_tokens(pilot: Any) -> Tuple[int, int, int]:
    """Return ``(system_tokens, tool_tokens, floor_tokens)`` for the fixed prefix.

    Floor = system + tools + standing rules/skills/MCP (not conversation).
    """
    system_tokens = 0
    tool_tokens = 0
    floor_tokens = 0
    try:
        usage = pilot.get_context_usage()
    except Exception:
        return 0, 0, 0
    categories = usage.get("categories") if isinstance(usage, dict) else None
    if not isinstance(categories, list):
        return 0, 0, 0
    standing_names = {
        "System prompt",
        "Tool definitions",
        "Rules",
        "Skills",
        "MCP",
    }
    for cat in categories:
        if not isinstance(cat, dict):
            continue
        name = str(cat.get("name") or "")
        try:
            toks = max(0, int(cat.get("tokens") or 0))
        except (TypeError, ValueError):
            toks = 0
        if name == "System prompt":
            system_tokens = toks
        elif name == "Tool definitions":
            tool_tokens = toks
        if name in standing_names:
            floor_tokens += toks
    return system_tokens, tool_tokens, floor_tokens


def _standing_economics_fields(price_in: float) -> dict:
    """AGNT-inspired standing floor + prompt-cache TTL forecast (estimated only).

    Gated by ``HARNESS_STANDING_ECONOMICS`` (default off). Never folds into
    billed spend or list-price value totals — informational estimated fields.
    After TTL expiry, cached-floor USD is omitted so we never claim cache value.
    """
    if not standing_economics_enabled():
        return {}
    try:
        pilot = _pilot()
    except Exception:
        return {}
    if pilot is None:
        return {}

    try:
        pin = float(price_in or 0.0)
    except (TypeError, ValueError):
        pin = 0.0
    if pin <= 0:
        return {}

    system_tokens, tool_tokens, floor_tokens = _standing_prefix_tokens(pilot)
    if floor_tokens <= 0 and system_tokens <= 0 and tool_tokens <= 0:
        return {}

    from .cost_accounting import CACHE_READ_MULTIPLIER

    floor_cost = (floor_tokens / 1.0e6) * pin
    floor_cost_cached = (floor_tokens / 1.0e6) * pin * CACHE_READ_MULTIPLIER

    driver = ""
    try:
        driver = str(getattr(getattr(pilot, "config", None), "driver", "") or "")
    except Exception:
        driver = ""
    if not driver:
        try:
            driver = str(getattr(_cfg(), "driver", "") or "")
        except Exception:
            driver = ""

    ttl_ms = prompt_cache_ttl_ms_for_driver(driver)
    now = time.time()
    last_cache_read = max(0, int(getattr(pilot, "_last_turn_cache_read_tokens", 0) or 0))
    activity_at = getattr(pilot, "_last_prompt_cache_activity_at", None)
    # Cached-floor display requires explicit cache-read evidence for this window.
    if last_cache_read <= 0:
        activity_at = None
    age_ms: Optional[int] = None
    expires_in_ms: Optional[int] = None
    cache_state: Optional[str] = None
    try:
        if activity_at is not None:
            age_ms = max(0, int((now - float(activity_at)) * 1000))
    except (TypeError, ValueError):
        age_ms = None
    prompt_cache_on = True
    try:
        from pmharness.drivers.prompt_cache import prompt_cache_enabled

        prompt_cache_on = prompt_cache_enabled()
    except Exception:
        prompt_cache_on = True

    if prompt_cache_on and ttl_ms is not None and age_ms is not None:
        if age_ms >= ttl_ms:
            cache_state = "expired"
            expires_in_ms = 0
        else:
            cache_state = "warm"
            expires_in_ms = max(0, ttl_ms - age_ms)

    payload: Dict[str, Any] = {
        # Explicit estimated labeling — never billed / never list-price totals.
        "standing_economics_basis": "estimated",
        "standing_system_tokens": int(system_tokens),
        "standing_tool_tokens": int(tool_tokens),
        "standing_floor_tokens": int(floor_tokens),
        "standing_floor_cost_usd": round(floor_cost, 6),
    }
    # Cached-floor value only for explicit warm TTL — never unknown/no activity.
    if prompt_cache_on and cache_state == "warm":
        payload["standing_floor_cost_cached_usd"] = round(floor_cost_cached, 6)
    if prompt_cache_on:
        if ttl_ms is not None:
            payload["prompt_cache_ttl_ms"] = int(ttl_ms)
        if age_ms is not None:
            payload["prompt_cache_age_ms"] = int(age_ms)
        if expires_in_ms is not None:
            payload["prompt_cache_expires_in_ms"] = int(expires_in_ms)
        if cache_state is not None:
            payload["prompt_cache_state"] = cache_state
    return payload


def _tool_output_savings_fields(price_in: float, *, process_wide: bool = False) -> dict:
    """Compact tool-output savings for session payloads.

    When ``process_wide`` is True (boot /api/usage pill), aggregate across the
    whole state-dir ledger for this process epoch rather than the active
    harness_session_id -- so dir/session swaps do not zero the saved meter.
    Also folds Puppetmaster/CLI ``tool_output_savings.jsonl`` offloads from
    boot-repo state dirs (deduped by tool_call_id).
    """
    # Empty session_id => ledger summarize() aggregates all sessions.
    from ..job_scoping import cli_cost_merge_enabled as _cli_cost_merge_enabled

    sid = "" if process_wide else (getattr(_pilot(), "harness_session_id", "") or "")
    cli_dirs: list[str] = []
    if process_wide and _cli_cost_merge_enabled():
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
        from ..compaction_advisor import advice_payload, apply_manual_compaction_ack

        pilot = _pilot()
        try:
            budget = int(pilot.active_context_limit())
        except Exception:
            budget = getattr(getattr(pilot, "config", None), "max_context_tokens", 96000)
        advice = advice_payload(
            _pilot().state_dir,
            getattr(_pilot(), "harness_session_id", "") or "default",
            budget,
        )
        payload.update(apply_manual_compaction_ack(advice, _pilot()))
    except Exception:
        pass
    # Standing floor/TTL are session-scoped — omit from process-wide boot pill.
    if not process_wide:
        try:
            payload.update(_standing_economics_fields(price_in))
        except Exception:
            pass
    return payload


def _job_savings_fields(job_id: str) -> dict:
    """Per-job tool-output savings, merging harness + PM/CLI JSONL ledgers."""
    try:
        from ..cli_job_merge import resolve_cli_state_dir
        from ..job_scoping import cli_cost_merge_enabled
        from ..tool_output_savings import job_savings_payload

        try:
            from pmharness.registry import resolve_price

            price_in, _ = resolve_price(_cfg().driver)
            if price_in is None:
                price_in = 0.0
        except Exception:
            price_in = 0.0
        cli_dir = None
        if cli_cost_merge_enabled():
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



def _iter_live_runners() -> list:
    """Live registry runners plus the active pilot when it is not registered."""
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
    return live


def _runner_pilot_model(runner: Any) -> str:
    """Driver that actually owns this runner's meters (not the picker target)."""
    cfg = getattr(runner, "config", None)
    driver = getattr(cfg, "driver", None) if cfg is not None else None
    text = str(driver or "").strip()
    return text or "unknown"


def _empty_pilot_slice(model: str, price_in: float = 0.0, price_out: float = 0.0) -> dict:
    return {
        "model": model,
        "est_cost_usd": 0.0,
        "tokens_used": 0.0,
        "tokens_in": 0.0,
        "tokens_out": 0.0,
        "tokens_cached": 0.0,
        "pilot_cache_read_tokens": 0.0,
        "worker_cost_usd": 0.0,
        "worker_tokens_cached": 0.0,
        "provider_cost_usd": 0.0,
        "cache_savings_gross_usd": 0.0,
        "price_in": float(price_in or 0.0),
        "price_out": float(price_out or 0.0),
    }


def _runner_has_meters(runner: Any) -> bool:
    for attr in (
        "_tokens_used",
        "_tokens_in",
        "_tokens_out",
        "_tokens_cached",
        "_worker_cost_usd",
        "_provider_cost_usd",
    ):
        try:
            if float(getattr(runner, attr, 0) or 0) != 0.0:
                return True
        except Exception:
            continue
    return False


def _merge_runner_into_slices(
    dest: dict,
    runner: Any,
    price_in: float,
    price_out: float,
) -> None:
    """Add a runner's locked spend onto ``dest`` without mutating the runner."""
    from .cost_accounting import _cache_savings_gross

    model = _runner_pilot_model(runner)
    split_fn = _server_attr("_session_cost_split", _session_cost_split)
    try:
        cost = float(split_fn(runner, float(price_in), float(price_out)))
    except Exception:
        cost = 0.0
    sl = dest.get(model)
    if sl is None:
        sl = _empty_pilot_slice(model, price_in, price_out)
        dest[model] = sl
    tokens_cached = float(getattr(runner, "_tokens_cached", 0) or 0)
    worker_cached = float(getattr(runner, "_worker_tokens_cached", 0) or 0)
    pilot_cached = max(0.0, tokens_cached - worker_cached)
    sl["est_cost_usd"] = float(sl.get("est_cost_usd") or 0.0) + cost
    sl["tokens_used"] = float(sl.get("tokens_used") or 0.0) + float(
        getattr(runner, "_tokens_used", 0) or 0
    )
    sl["tokens_in"] = float(sl.get("tokens_in") or 0.0) + float(
        getattr(runner, "_tokens_in", 0) or 0
    )
    sl["tokens_out"] = float(sl.get("tokens_out") or 0.0) + float(
        getattr(runner, "_tokens_out", 0) or 0
    )
    sl["tokens_cached"] = float(sl.get("tokens_cached") or 0.0) + tokens_cached
    sl["pilot_cache_read_tokens"] = (
        float(sl.get("pilot_cache_read_tokens") or 0.0) + pilot_cached
    )
    sl["worker_cost_usd"] = float(sl.get("worker_cost_usd") or 0.0) + float(
        getattr(runner, "_worker_cost_usd", 0) or 0
    )
    sl["worker_tokens_cached"] = (
        float(sl.get("worker_tokens_cached") or 0.0) + worker_cached
    )
    sl["provider_cost_usd"] = float(sl.get("provider_cost_usd") or 0.0) + float(
        getattr(runner, "_provider_cost_usd", 0) or 0
    )
    try:
        sl["cache_savings_gross_usd"] = float(
            sl.get("cache_savings_gross_usd") or 0.0
        ) + float(_cache_savings_gross(pilot_cached, price_in))
    except Exception:
        pass
    if float(price_in or 0.0) > 0:
        sl["price_in"] = float(price_in)
    if float(price_out or 0.0) > 0:
        sl["price_out"] = float(price_out)


def _copy_pilot_slices(src: dict) -> dict:
    return {str(model): dict(sl) for model, sl in src.items() if isinstance(sl, dict)}


def _boot_pilot_by_model_map() -> dict:
    """Carry slices plus live runners priced at each runner's bound driver."""
    merged = _copy_pilot_slices(_BOOT_PILOT_BY_MODEL)
    resolve_runner = _server_attr(
        "_resolve_prices_for_runner", _resolve_prices_for_runner
    )
    for runner in _iter_live_runners():
        if not _runner_has_meters(runner):
            continue
        try:
            pin, pout = resolve_runner(runner)
        except Exception:
            pin, pout = 0.0, 0.0
        _merge_runner_into_slices(merged, runner, float(pin or 0.0), float(pout or 0.0))
    if not merged:
        carry_cost = float(_BOOT_CARRY_COST_USD or 0.0)
        has_carry = any(
            float(_BOOT_METER_CARRY.get(attr, 0.0) or 0.0) != 0.0
            for attr in _BOOT_METER_ATTRS
        )
        if carry_cost > 0.0 or has_carry:
            sl = _empty_pilot_slice("unknown")
            sl["est_cost_usd"] = carry_cost
            sl["tokens_used"] = float(_BOOT_METER_CARRY.get("_tokens_used", 0.0) or 0.0)
            sl["tokens_in"] = float(_BOOT_METER_CARRY.get("_tokens_in", 0.0) or 0.0)
            sl["tokens_out"] = float(_BOOT_METER_CARRY.get("_tokens_out", 0.0) or 0.0)
            sl["tokens_cached"] = float(_BOOT_METER_CARRY.get("_tokens_cached", 0.0) or 0.0)
            merged["unknown"] = sl
    return merged


def _boot_pilot_by_model_payload() -> list:
    """JSON rows for /api/usage: locked cumulative spend per pilot model."""
    rows = []
    for model, sl in _boot_pilot_by_model_map().items():
        cost = float(sl.get("est_cost_usd") or 0.0)
        tokens = int(float(sl.get("tokens_used") or 0.0))
        if cost <= 0.0 and tokens <= 0:
            continue
        rows.append({
            "model": str(sl.get("model") or model),
            "est_cost_usd": round(cost, 6),
            "tokens_used": tokens,
            "tokens_in": int(float(sl.get("tokens_in") or 0.0)),
            "tokens_out": int(float(sl.get("tokens_out") or 0.0)),
            "tokens_cached": int(float(sl.get("tokens_cached") or 0.0)),
        })
    rows.sort(key=lambda row: (-float(row["est_cost_usd"]), str(row["model"])))
    return rows


def _pilot_slices_for_persist() -> dict:
    """Snapshot carry+live slices (same shape restored after an in-run respawn)."""
    out = {}
    for model, sl in _boot_pilot_by_model_map().items():
        if not isinstance(sl, dict):
            continue
        out[str(model)] = {
            "model": str(sl.get("model") or model),
            "est_cost_usd": float(sl.get("est_cost_usd") or 0.0),
            "tokens_used": float(sl.get("tokens_used") or 0.0),
            "tokens_in": float(sl.get("tokens_in") or 0.0),
            "tokens_out": float(sl.get("tokens_out") or 0.0),
            "tokens_cached": float(sl.get("tokens_cached") or 0.0),
            "pilot_cache_read_tokens": float(sl.get("pilot_cache_read_tokens") or 0.0),
            "worker_cost_usd": float(sl.get("worker_cost_usd") or 0.0),
            "worker_tokens_cached": float(sl.get("worker_tokens_cached") or 0.0),
            "provider_cost_usd": float(sl.get("provider_cost_usd") or 0.0),
            "cache_savings_gross_usd": float(sl.get("cache_savings_gross_usd") or 0.0),
            "price_in": float(sl.get("price_in") or 0.0),
            "price_out": float(sl.get("price_out") or 0.0),
        }
    return out


def _restore_pilot_slices(raw: Any, *, carry_cost: float = 0.0) -> None:
    """Replace carry slices from boot_usage.json; synthesize unknown if legacy."""
    _BOOT_PILOT_BY_MODEL.clear()
    if isinstance(raw, dict) and raw:
        for model, sl in raw.items():
            if not isinstance(sl, dict):
                continue
            key = str(sl.get("model") or model or "").strip() or "unknown"
            restored = _empty_pilot_slice(key)
            for field in restored:
                if field == "model":
                    continue
                try:
                    restored[field] = float(sl.get(field, 0.0) or 0.0)
                except Exception:
                    pass
            restored["model"] = key
            _BOOT_PILOT_BY_MODEL[key] = restored
        return
    if carry_cost > 0.0:
        sl = _empty_pilot_slice("unknown")
        sl["est_cost_usd"] = float(carry_cost)
        sl["tokens_used"] = float(_BOOT_METER_CARRY.get("_tokens_used", 0.0) or 0.0)
        sl["tokens_in"] = float(_BOOT_METER_CARRY.get("_tokens_in", 0.0) or 0.0)
        sl["tokens_out"] = float(_BOOT_METER_CARRY.get("_tokens_out", 0.0) or 0.0)
        sl["tokens_cached"] = float(_BOOT_METER_CARRY.get("_tokens_cached", 0.0) or 0.0)
        _BOOT_PILOT_BY_MODEL["unknown"] = sl


def _boot_pilot_cache_savings(
    *,
    fallback_cached: float,
    fallback_price_in: float,
    provider_cost_usd: Optional[float] = None,
) -> Tuple[float, float, str]:
    """Prompt-cache value locked to each slice's fold-time input rate.

    Leftover cached tokens not yet attributed to a slice (legacy carry) still
    use ``fallback_price_in`` so a first poll before any fold is not $0.
    """
    from .cost_accounting import _cache_savings_gross

    gross = 0.0
    accounted = 0.0
    for sl in _boot_pilot_by_model_map().values():
        try:
            gross += float(sl.get("cache_savings_gross_usd") or 0.0)
            accounted += float(sl.get("pilot_cache_read_tokens") or 0.0)
        except Exception:
            continue
    leftover = max(0.0, float(fallback_cached or 0.0) - accounted)
    if leftover > 0.0 and float(fallback_price_in or 0.0) > 0.0:
        try:
            gross += float(_cache_savings_gross(leftover, fallback_price_in))
        except Exception:
            pass
    if gross <= 0:
        return 0.0, 0.0, "catalog"
    if provider_cost_usd is None:
        return gross, gross, "catalog"
    try:
        prov = float(provider_cost_usd)
    except (TypeError, ValueError):
        return 0.0, gross, "unknown"
    if prov <= 0:
        return 0.0, gross, "unknown"
    if gross > prov:
        return prov, gross, "capped"
    return gross, gross, "catalog"


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
        _merge_runner_into_slices(
            _BOOT_PILOT_BY_MODEL, runner, float(price_in), float(price_out)
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
    for runner in _iter_live_runners():
        for attr in _BOOT_METER_ATTRS:
            try:
                totals[attr] = float(totals[attr]) + float(getattr(runner, attr, 0) or 0)
            except Exception:
                pass
    return totals


def _boot_session_cost(price_in: float, price_out: float) -> float:
    """Sum snapshotted carry USD + per-live-runner ``_session_cost_split``.

    Carry dollars are frozen at fold-time rates (see ``_BOOT_CARRY_COST_USD``).
    Live runners price at each runner's bound ``config.driver``, not the
    currently selected picker — a deferred mid-turn swap must not reprice
    tokens the outgoing model already churned. Legacy carry with token meters
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
    resolve_runner = _server_attr(
        "_resolve_prices_for_runner", _resolve_prices_for_runner
    )
    for runner in _iter_live_runners():
        try:
            pin, pout = resolve_runner(runner)
            total += float(
                _session_cost_split(runner, float(pin), float(pout))
            )
        except Exception:
            try:
                total += float(_session_cost_split(runner, price_in, price_out))
            except Exception:
                pass
    return total
