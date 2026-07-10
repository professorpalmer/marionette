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
from typing import Any
from urllib.parse import urlparse, parse_qs
import tempfile
import uuid

from dataclasses import replace as _dc_replace

from .config import HarnessConfig
from .session import Session
from .conversation import ConversationalSession
from .mcp_manager import McpManager, CATALOG
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
from .session_runners import SessionRunnerRegistry, LeaseExhaustedError
from .autobudget import AutoBudget
from ._exec import _puppetmaster_python, _puppetmaster_available, _puppetmaster_cmd, _ensure_node_on_path
from .diag import note as _diag
from .secure_files import restrict_dir_to_owner, restrict_to_owner


# Prompt-cache reads are billed at a steep discount vs fresh input tokens:
# Anthropic prompt caching and OpenAI/Gemini cached-input reads are ~10% of the
# normal input price. We meter cache_read_tokens separately (see
# ConversationalSession) and re-bill that slice at this multiplier instead of
# the full input rate. 0.1 is the published cache-read ratio and is
# conservative (some providers go lower), so we never *under*-count spend.
CACHE_READ_MULTIPLIER = 0.1


def _session_cost(t_in: float, t_out: float, cached: float,
                  price_in: float, price_out: float) -> float:
    """Deterministic session cost from tokens + per-Mtok prices.

    Cached prompt tokens are a subset of ``t_in`` and are billed at the
    cache-read discount rather than full input price. Falls back to pricing the
    whole total at ``price_out`` when no in/out split is available (completion
    dominates cost, so this is the least-wrong single-rate estimate)."""
    if t_in or t_out:
        cached = max(0.0, min(float(cached), float(t_in)))
        uncached_in = max(0.0, float(t_in) - cached)
        return ((uncached_in / 1.0e6) * price_in
                + (cached / 1.0e6) * price_in * CACHE_READ_MULTIPLIER
                + (float(t_out) / 1.0e6) * price_out)
    # No split tracked: price the combined total at the output rate.
    total = float(t_in) + float(t_out)
    return (total / 1.0e6) * price_out


def _session_cost_split(pilot: Any, price_in: float, price_out: float) -> float:
    """Session cost that prices PILOT tokens at the pilot rate and ADDS
    delegated-worker dollars (already priced at each worker's own model rate).

    Worker tokens are folded into the pilot's _tokens_* meters for display, but
    pricing them at the pilot rate under-reports cost when a worker ran on a
    pricier model (e.g. opus at $5/$25 vs a cheap pilot). So we subtract the
    worker token split from the pilot-priced portion and add _worker_cost_usd.
    getattr defaults keep OLD sessions (no worker split) identical to before."""
    t_in = int(getattr(pilot, "_tokens_in", 0) or 0)
    t_out = int(getattr(pilot, "_tokens_out", 0) or 0)
    t_cached = int(getattr(pilot, "_tokens_cached", 0) or 0)
    w_in = int(getattr(pilot, "_worker_tokens_in", 0) or 0)
    w_out = int(getattr(pilot, "_worker_tokens_out", 0) or 0)
    w_cost = float(getattr(pilot, "_worker_cost_usd", 0.0) or 0.0)
    pilot_in = max(0, t_in - w_in)
    pilot_out = max(0, t_out - w_out)
    # Cached tokens are a subset of pilot input; clamp so cache discount never
    # exceeds the pilot input we are actually pricing here.
    pilot_cached = max(0, min(t_cached, pilot_in))
    return _session_cost(pilot_in, pilot_out, pilot_cached, price_in, price_out) + w_cost


def _cache_savings(cached: float, price_in: float) -> float:
    """USD saved by billing ``cached`` prompt tokens at the cache-read discount
    instead of the full input price."""
    return (float(cached) / 1.0e6) * price_in * (1.0 - CACHE_READ_MULTIPLIER)


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
    "_worker_cost_usd",
    "_worker_tokens_in",
    "_worker_tokens_out",
)
_BOOT_METER_CARRY: dict[str, float] = {attr: 0.0 for attr in _BOOT_METER_ATTRS}
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
            payload = {
                "app_run_id": run_id,
                "cost_epoch": _COST_EPOCH.isoformat(),
                "carry": carry_snap,
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
    global _COST_EPOCH, _BOOT_USAGE_RESTORED
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
    """Keep only what collapsed Finished cards need: ROUTING + verdict rows.

    Full FINDING/RISK/DECISION streams are fetched on expand via /api/artifacts.
    With ~20 finished jobs, shipping every finding on each poll was the tracker lag.
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
    """Canonical form for path comparisons: realpath + normcase.

    On Windows the same directory surfaces with mixed drive-letter / component
    casing (env-var spelling, 8.3 short names), so raw realpath strings are
    not comparable with ``==``. Mirrors ``_norm_path`` in job_scoping/sessions.
    """
    return os.path.normcase(os.path.realpath(path))


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


def _record_recent_workspace(target_repo: str) -> list:
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
        _tmproot = os.path.realpath(_tf.gettempdir())
        def _persistable(_pth):
            if not _pth:
                return False
            _rp = os.path.realpath(_pth)
            if "PYTEST_CURRENT_TEST" not in os.environ:
                if _rp.startswith(_tmproot) or "/var/folders/" in _rp or "/T/tmp" in _pth:
                    return False
            if _is_app_install_root(_pth):
                return False
            return os.path.isdir(_pth)
        # Stable order: if path already in recents, leave its position; if new,
        # append. Do NOT prepend-to-front on every open (that snapped the rail).
        # Still persist active "repo" below for boot restore. Cap 8 + ephemeral
        # guards unchanged. App install root is never added (manual open stays
        # process-local only).
        if target_repo and target_repo not in recents and _persistable(target_repo):
            recents = list(recents) + [target_repo]
        recents = [r for r in recents if _persistable(r)]
        recents = recents[:8]

        # The "repo" key is what boot restores as the active workspace, so a
        # temp dir / app checkout here resurrects as a phantom project on next
        # launch. Keep the prior persisted repo when the new target is not a
        # user project.
        if _persistable(target_repo):
            persisted_repo = target_repo
        elif prior_repo and _persistable(prior_repo):
            persisted_repo = prior_repo
        else:
            persisted_repo = ""

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
        _tmproot = os.path.realpath(_tf.gettempdir())
        def _persistable(_pth):
            if not _pth:
                return False
            _rp = os.path.realpath(_pth)
            if "PYTEST_CURRENT_TEST" not in os.environ:
                if _rp.startswith(_tmproot) or "/var/folders/" in _rp or "/T/tmp" in _pth:
                    return False
            if _is_app_install_root(_pth):
                return False
            return os.path.isdir(_pth)

        # remove forget_path
        recents = [r for r in recents if r != forget_path]
        recents = [r for r in recents if _persistable(r)]
        recents = recents[:8]

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
from .wiki_config import load_wiki_config_on_startup, get_wiki_config, set_wiki_config
from .wiki_backend import ensure_wiki_backend_async
load_api_keys_on_startup(_cfg.reach)
# The Electron host spawns the backend with a stripped PATH; make Node visible so
# CodeGraph (a Node CLI) works out of the box instead of reporting "unsupported".
_ensure_node_on_path()
load_wiki_config_on_startup()
# Auto-provision (clone + venv + token) and boot a local wiki backend so the
# panel works out of the box with no manual terminal. Opt out: MARIONETTE_NO_WIKI=1.
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
    """Carry process-lifetime cost meters across a SAME-view pilot rebuild/swap.

    Used only when a rebuild replaces the active view's runner in place
    (``_rebuild_pilot_and_session`` / model swap). New runners created on
    attach/create must start at zero -- boot-pill totals come from
    ``_BOOT_METER_CARRY`` + sum of live runners, not from copying meters.
    """
    for attr in _BOOT_METER_ATTRS:
        try:
            setattr(new_pilot, attr, getattr(old_pilot, attr, getattr(new_pilot, attr, 0)))
        except Exception:
            pass


def _fold_runner_meters_into_boot_carry(session_id: str, runner: Any) -> None:
    """Add a dropped/evicted runner's meters into the process-lifetime carry.

    Zeros the runner's meters after folding so a lingering ``_pilot`` pointer
    cannot double-count with carry in ``_boot_usage_meters``.
    """
    del session_id  # reserved for diagnostics; meters are process-scoped
    for attr in _BOOT_METER_ATTRS:
        try:
            add = float(getattr(runner, attr, 0) or 0)
            _BOOT_METER_CARRY[attr] = float(_BOOT_METER_CARRY.get(attr, 0.0) or 0.0) + add
            if attr == "_worker_cost_usd":
                setattr(runner, attr, 0.0)
            else:
                setattr(runner, attr, 0)
        except Exception:
            pass
    try:
        _persist_boot_usage(fold_live=False)
    except Exception:
        pass


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
    """Sum per-pilot ``_session_cost_split`` over carry (as a virtual pilot) + live runners.

    Pricing each live runner separately keeps worker dollars at each worker's
    own model rate. Carry is priced as a synthetic pilot at the active rate
    (same approximation as a single-runner process after eviction).
    """
    from types import SimpleNamespace

    carry_pilot = SimpleNamespace(**{
        attr: _BOOT_METER_CARRY.get(attr, 0.0) for attr in _BOOT_METER_ATTRS
    })
    total = float(_session_cost_split(carry_pilot, price_in, price_out))
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


def _build_conversational_pilot(*, copy_meters_from: Any = None) -> ConversationalSession:
    """Construct a ConversationalSession with a frozen per-runner config copy.

    ``copy_meters_from`` is only for SAME-view rebuild/swap. Attach/create must
    omit it so new runners start at zero for boot-pill accounting.
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


def _attach_view(session_id: str, *, factory=None, load_transcript_on_create: bool = True) -> Any:
    """Point the UI at ``session_id`` via the runner registry.

    get_or_create under the lease; set_active_view; assign global ``_pilot``
    (and pin ``_session.state_dir``). Loads the session transcript when the
    runner is newly created or when switching to a different view.
    Raises ``LeaseExhaustedError`` when a new runner is required but every
    lease slot holds a busy runner.
    """
    global _pilot, _session
    if not session_id:
        raise ValueError("session_id required to attach view")

    prev_view = _runners.active_view_id
    created = _runners.get(session_id) is None

    def _factory():
        if factory is not None:
            return factory()
        # New runners start at zero meters -- boot pill sums carry + live.
        return _build_conversational_pilot()

    runner = _runners.get_or_create(session_id, _factory)
    _runners.set_active_view(session_id)
    with _pilot_swap_lock:
        _pilot = runner
        # Keep tracker/jobs pointed at the store this runner writes to.
        try:
            _session.state_dir = _pilot.state_dir
        except Exception:
            pass
        _bind_pilot_services(_pilot)
        # Existing runners already hold live history (including in-flight turns).
        # Only hydrate from disk when the runner was just created.
        if created and load_transcript_on_create:
            history = load_transcript(_sessions_state_dir(), session_id)
            _pilot.load_history(history)
        _sync_pilot_session_id()
    return _pilot


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


def _remove_session_transcript(sid: str) -> None:
    safe_sid = "".join(c for c in sid if c.isalnum() or c in ("-", "_"))
    if not safe_sid:
        return
    state_dir = _sessions_state_dir()
    trans_dir = os.path.abspath(os.path.join(state_dir, "transcripts"))
    p = os.path.abspath(os.path.join(trans_dir, f"{safe_sid}.json"))
    if p.startswith(trans_dir) and os.path.exists(p):
        try:
            os.remove(p)
        except Exception as e:
            _diag("server.session_delete_transcript", e, msg=f"sid={safe_sid}")


def _handle_session_delete(sid: str) -> tuple[int, dict]:
    if not sid:
        return 400, {"error": "missing session id"}
    is_active = (_sessions.active == sid)
    from .hooks import run_hooks
    run_hooks("sessionEnd", {"session_id": sid})
    new_active = _sessions.delete(sid)
    _remove_session_transcript(sid)
    try:
        _runners.drop(sid)
    except Exception as e:
        _diag("server.session_delete_drop_runner", e)
    if is_active:
        if new_active:
            try:
                _attach_view(new_active)
            except LeaseExhaustedError:
                # Fall back to loading into the current global pilot pointer.
                history = load_transcript(_sessions_state_dir(), new_active)
                _pilot.load_history(history)
                _sync_pilot_session_id()
        else:
            _pilot.load_history([])
            _sync_pilot_session_id()
    return 200, {"ok": True, "active": new_active}


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


def _rebuild_pilot_and_session():
    """Rebuild the ACTIVE view's runner for the current driver, preserving history.

    Only replaces the active view's entry in the registry -- never wipes other
    busy runners. If the active runner is mid-turn, refuse (callers that need
    a hard swap should 409 first).

    Defensive: if the configured driver cannot be built (e.g. a stale saved
    spec the catalog no longer knows), do NOT let the exception escape and
    crash the POST handler -- that left the whole app dead on workspace-open /
    session-switch. We roll back to the previous working driver and surface the
    error to the caller to show, instead of taking down the process.
    """
    global _session, _pilot, _cfg
    active_id = _sessions.active or _runners.active_view_id
    if active_id:
        existing = _runners.get(active_id)
        if existing is not None:
            busy = getattr(existing, "_busy", None)
            locked = getattr(busy, "locked", None) if busy is not None else None
            if callable(locked) and locked():
                raise RuntimeError("pilot busy -- finish or stop the current turn before rebuilding")

    prev_driver = _cfg.driver
    _apply_model_context_window()
    try:
        # Tracker Session may share the view config; the runner gets a frozen copy.
        new_session = Session(_cfg)
        new_pilot = ConversationalSession(_runner_config_snapshot())
    except Exception as e:
        # Roll back to the last driver that built successfully.
        _cfg.driver = prev_driver
        _apply_model_context_window()
        raise RuntimeError(
            f"could not load model {prev_driver!r}: {e}. Reverted to the "
            f"previous pilot."
        ) from e
    # Keep the tracker/jobs reads pointed at the store the pilot writes to (see
    # the pin at initial construction) across workspace/driver switches too.
    new_session.state_dir = new_pilot.state_dir
    with _pilot_swap_lock:
        old_history = _pilot._history
        old_auto_distill = getattr(_pilot, "_auto_distill", False)
        old_pilot = _pilot
        _session = new_session
        _pilot = new_pilot
        _pilot._history = old_history
        _pilot._auto_distill = old_auto_distill
        _copy_pilot_meters(old_pilot, _pilot)
        _bind_pilot_services(_pilot)
        _sync_pilot_session_id()
        if active_id:
            # Replace only this view's registry entry; leave other runners alone.
            # notify=False: meters were copied onto the replacement, so folding
            # the old runner into boot carry would double-count.
            _runners.drop(active_id, notify=False)
            _runners.get_or_create(active_id, lambda: _pilot)
            _runners.set_active_view(active_id)


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
_CHAT_STASH: dict[str, dict] = {}
_CHAT_STASH_MAX = 32


def _stash_put(message: str, images=None) -> str:
    mid = _secrets.token_hex(8)
    _CHAT_STASH[mid] = {"message": message, "images": images or []}
    # Evict oldest entries beyond the cap (insertion order == age in a dict).
    while len(_CHAT_STASH) > _CHAT_STASH_MAX:
        try:
            _CHAT_STASH.pop(next(iter(_CHAT_STASH)))
        except StopIteration:
            break
    return mid


def _stash_pop(mid: str):
    """Returns the stashed {'message', 'images'} dict, or None if unknown/expired."""
    return _CHAT_STASH.pop(mid, None)

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


_codegraph_status = "unsupported"
_codegraph_status_reason = None

# Short-TTL cache for the /api/codegraph status payload, keyed by repo path.
# Reading codegraph status spawns a `puppetmaster codegraph status --json`
# subprocess (interpreter cold-start + DB read) on every poll, which is the
# source of the panel's load lag. The graph only changes on (re)index, so we
# serve a cached payload for a few seconds and only re-spawn when stale. The
# cache is bypassed entirely while status == "indexing" (that path never hits
# the subprocess), so a fresh index is reflected as soon as it finishes.
_codegraph_status_cache = {}  # repo -> (monotonic_expiry, payload_dict)
_CODEGRAPH_STATUS_TTL = 30.0  # seconds

# Short-TTL cache for the /api/wiki/graph payload. Each fetch is an HTTP round
# trip to the wiki host (up to an 8s timeout when slow/unreachable), and the
# wiki graph changes rarely, so a brief cache removes the repeated stall on the
# panel without making the data meaningfully stale.
_wiki_graph_cache = {}  # base_url -> (monotonic_expiry, payload_dict)
_WIKI_GRAPH_TTL = 60.0  # seconds


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


def _index_codegraph_bg(repo_path: str):
    global _codegraph_status, _codegraph_status_reason, _codegraph_status_cache
    if not _puppetmaster_available():
        _codegraph_status = "unsupported"
        _codegraph_status_reason = "puppetmaster not found -- codegraph/swarm unavailable"
        return
    global _codegraph_index_proc
    # Guard against a second indexer while one is already running -- concurrent
    # codegraph indexers collide on the same SQLite (lock-busy) and wedge the panel.
    with _codegraph_index_lock:
        if _codegraph_index_alive():
            _codegraph_status = "indexing"
            return
        _codegraph_status = "indexing"
        _codegraph_status_reason = None
        # Invalidate any cached status for this repo so the panel does not show
        # stale "ready" stats while a fresh (re)index is running.
        _codegraph_status_cache.pop(repo_path, None)
        try:
            import subprocess
            proc = subprocess.Popen(
                _puppetmaster_cmd("codegraph", "init", "--index"),
                cwd=repo_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            _codegraph_index_proc = (repo_path, proc)
        except Exception:
            _codegraph_status = "unsupported"
            return

    def wait_and_update():
        global _codegraph_status, _codegraph_index_proc
        try:
            proc.wait(timeout=600)  # max 10 mins
            if proc.returncode == 0:
                _codegraph_status = "ready"
            else:
                _codegraph_status = "unsupported"
        except Exception:
            _codegraph_status = "unsupported"
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            # Clear the tracker so status can self-heal and a future index can run.
            with _codegraph_index_lock:
                if _codegraph_index_proc and _codegraph_index_proc[1] is proc:
                    _codegraph_index_proc = None
            _codegraph_status_cache.pop(repo_path, None)

    threading.Thread(target=wait_and_update, daemon=True).start()


def _reindex_codegraph_bg(repo_path: str):
    global _codegraph_status, _codegraph_status_reason
    if not _puppetmaster_available():
        _codegraph_status = "unsupported"
        _codegraph_status_reason = "puppetmaster not found -- codegraph/swarm unavailable"
        return
    global _codegraph_index_proc
    with _codegraph_index_lock:
        if _codegraph_index_alive():
            _codegraph_status = "indexing"
            return
        _codegraph_status = "indexing"
        _codegraph_status_reason = None
        _codegraph_status_cache.pop(repo_path, None)
        try:
            import subprocess
            proc = subprocess.Popen(
                _puppetmaster_cmd("codegraph", "index"),
                cwd=repo_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            _codegraph_index_proc = (repo_path, proc)
        except Exception:
            _codegraph_status = "unsupported"
            return

    def wait_and_update():
        global _codegraph_status, _codegraph_index_proc
        try:
            proc.wait(timeout=600)  # max 10 mins
            if proc.returncode == 0:
                _codegraph_status = "ready"
            else:
                _codegraph_status = "unsupported"
        except Exception:
            _codegraph_status = "unsupported"
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            with _codegraph_index_lock:
                if _codegraph_index_proc and _codegraph_index_proc[1] is proc:
                    _codegraph_index_proc = None
            _codegraph_status_cache.pop(repo_path, None)

    threading.Thread(target=wait_and_update, daemon=True).start()


def _get_codegraph_status(repo_path: str) -> str:
    global _codegraph_status
    if not repo_path:
        return "unsupported"
    if not _puppetmaster_available():
        _codegraph_status = "unsupported"
        return "unsupported"
    # Self-heal: trust the "indexing" flag ONLY while the indexer subprocess is
    # actually alive. A stale flag (proc finished but the wait thread lost a
    # race, or an old global left over) must not pin the panel on "indexing"
    # forever -- fall through to the disk check below. This is the bug that
    # required a full app restart to clear.
    if _codegraph_status == "indexing":
        if _codegraph_index_alive():
            return "indexing"
        # Indexer is not running -> resolve real state from disk.
        _codegraph_status = "ready" if os.path.isdir(os.path.join(repo_path, ".codegraph")) else "unsupported"

    if os.path.isdir(os.path.join(repo_path, ".codegraph")):
        _codegraph_status = "ready"
        return "ready"
    else:
        return "unsupported"


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


def _parse_multipart_files(body: bytes, content_type: str) -> list:
    """Extract uploaded files from a multipart/form-data body using the stdlib
    email parser. Replaces cgi.FieldStorage, which was removed in Python 3.13.
    Returns a list of (filename, data_bytes) for every part carrying a filename.
    The body is already size-capped by the caller, so buffering it is bounded."""
    from email.parser import BytesParser
    # Synthesize the MIME header block the parser needs, then feed it the body.
    header = (b"MIME-Version: 1.0\r\nContent-Type: "
              + content_type.encode("latin-1", "replace") + b"\r\n\r\n")
    message = BytesParser().parsebytes(header + body)
    files = []
    if not message.is_multipart():
        return files
    for part in message.get_payload():
        filename = part.get_filename()
        if not filename:
            continue
        data = part.get_payload(decode=True)
        if data is None:
            continue
        files.append((filename, data))
    return files


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
        global _codegraph_status
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
                      "/api/session/interrupt", "/api/session/compact", "/api/session/steer",
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
                      "/api/platform", "/api/reviews/apply", "/api/reviews/dismiss",
                      "/api/registry", "/api/roles", "/api/pilot/validate",
                      "/api/worktrees/add", "/api/worktrees/remove",
                      "/api/worktrees/prune", "/api/worktrees/prune-edit-branches",
                      "/api/worktrees/max",
                      "/api/hooks/add", "/api/hooks/update", "/api/hooks/remove",
                      "/api/workspace/open", "/api/workspace/forget", "/api/codegraph/reindex",
                      "/api/file/write",
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
        try:
            body = self._read_json()
        except json.JSONDecodeError:
            return self._send(400, json.dumps({"error": "invalid JSON"}))
        repo = _cfg.repo

        if path == "/api/reviews/apply":
            review_id = body.get("id", "").strip()
            decisions = body.get("decisions", {})
            if not review_id:
                return self._send(400, json.dumps({"error": "Missing review id"}))
            res = _pilot.apply_review(review_id, decisions)
            return self._send(200, json.dumps(res))

        if path == "/api/reviews/dismiss":
            review_id = body.get("id", "").strip()
            if not review_id:
                return self._send(400, json.dumps({"error": "Missing review id"}))
            success = _pilot.dismiss_review(review_id)
            return self._send(200, json.dumps({"ok": success}))
        if path == "/api/swarm/cancel":
            # Cooperative cancel for a swarm job. Best-effort and never raises:
            # local (provider-worker) jobs are cancelled via the per-job Event on
            # the conversation; durable store jobs are marked cancelled in the
            # store where possible. Mirrors the auth/token guard already applied to
            # every POST (see do_POST). Shape: {ok, job_id} or {ok:false,error}.
            job_id = (body.get("job_id") or "").strip()
            if not job_id:
                return self._send(400, json.dumps({"ok": False, "error": "missing job_id"}))
            # 0) Trip the in-process kill switch FIRST: inline agentic workers
            # check it mid-stream (per chunk) and per turn, so the swarm stops in
            # seconds instead of "cancelling..." until the workers run out of
            # turns naturally.
            try:
                from puppetmaster.cancellation import request_cancel
                request_cancel(job_id)
            except Exception as e:
                _diag("server.swarm_cancel_flag", e)
            # 1) Local provider-worker job on the live conversation.
            try:
                if hasattr(_pilot, "cancel_local_job") and _pilot.cancel_local_job(job_id):
                    return self._send(200, json.dumps({"ok": True, "job_id": job_id}))
            except Exception as e:
                _diag("server.swarm_cancel_local", e)
            # 2) Durable Puppetmaster store job -- best-effort mark cancelled.
            # Check BOTH stores the tracker reads: the harness store and the
            # per-project CLI store (jobs started via `python -m puppetmaster`
            # live only there; cancel used to 404 on them).
            def _candidate_stores():
                try:
                    state_obj = _session.state()
                    yield getattr(state_obj, "store", None), state_obj.list_jobs
                except Exception as e:
                    _diag("server.swarm_cancel_harness_store", e)
                try:
                    from .cli_job_merge import open_cli_durable_state
                    cli_state = open_cli_durable_state(_cfg.repo or "")
                    if cli_state is not None:
                        yield cli_state.store, cli_state.store.list_jobs
                except Exception as e:
                    _diag("server.swarm_cancel_cli_store", e)

            def _mark_cancelled(store) -> bool:
                for meth in ("cancel_job", "set_job_status", "update_job_status"):
                    fn = getattr(store, meth, None)
                    if fn is None:
                        continue
                    try:
                        fn(job_id) if meth == "cancel_job" else fn(job_id, "cancelled")
                        return True
                    except Exception:
                        continue
                return False

            try:
                for store, list_jobs in _candidate_stores():
                    if store is None:
                        continue
                    try:
                        known = any(
                            (j.get("id") if isinstance(j, dict) else getattr(j, "id", None)) == job_id
                            for j in list_jobs()
                        )
                    except Exception:
                        known = False
                    if not known:
                        continue
                    return self._send(200, json.dumps({
                        "ok": True, "job_id": job_id, "durable": True,
                        "marked": _mark_cancelled(store),
                    }))
            except Exception as e:
                _diag("server.swarm_cancel_durable", e)
            return self._send(404, json.dumps({"ok": False, "error": "unknown job_id", "job_id": job_id}))
        if path == "/api/session/persist":
            # Flush the live transcript to disk on demand. Called right before a
            # backend restart (self-edit apply) so the fresh process restores the
            # exact conversation state, including any unanswered user turn.
            # Also arms the one-shot resume latch so the post-restart UI can
            # auto-continue -- without treating every trailing user turn as a
            # resume signal on mere session view.
            try:
                if _sessions.active:
                    save_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active, _pilot.export_transcript_data())
                _set_resume_latch()
                _persist_boot_usage(fold_live=True, force=True)
                return self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}))
        if path == "/api/restart":
            # Graceful self-restart for non-Electron callers (served browser) and
            # as a fallback path. Persist, ACK, then SIGTERM self so a supervisor
            # (Electron) respawns on the freshly-edited source. In the desktop app
            # the Electron IPC path (harness:restart) is preferred -- it also
            # reloads the renderer -- but this keeps the capability reachable over
            # HTTP for the pilot or a browser session.
            try:
                if _sessions.active:
                    save_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active, _pilot.export_transcript_data())
                _set_resume_latch()
                _persist_boot_usage(fold_live=True, force=True)
            except Exception as e:
                _diag("server.self_edit_restart_persist", e)
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
            before = _pilot._estimate_context_tokens()
            orig_tokens = getattr(_cfg, "max_context_tokens", 96000)
            _cfg.max_context_tokens = 1
            try:
                events = list(_pilot._maybe_compact_history())
            finally:
                _cfg.max_context_tokens = orig_tokens
            after = _pilot._estimate_context_tokens()
            if _sessions.active:
                save_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active, _pilot.export_transcript_data())
            return self._send(200, json.dumps({
                "ok": True,
                "before_tokens": before,
                "after_tokens": after
            }))
        if path == "/api/checkpoints/restore":
            if not repo or not os.path.exists(repo):
                return self._send(400, json.dumps({"error": "No open workspace"}))
            checkpoint_id = body.get("id", "").strip()
            if not checkpoint_id:
                return self._send(400, json.dumps({"error": "Missing checkpoint id"}))
            from .checkpoints import CheckpointStore
            active_sid = _sessions.active or ""
            store = CheckpointStore(repo, session_id=active_sid or None)
            result = store.restore(
                checkpoint_id,
                session_id=active_sid or None,
                expected_repo=repo,
            )
            if result.get("ok"):
                return self._send(200, json.dumps(result))
            else:
                return self._send(400, json.dumps({"error": result.get("error", "Restore failed")}))

        if path == "/api/checkpoints/snapshot":
            if not repo or not os.path.exists(repo):
                return self._send(400, json.dumps({"error": "No open workspace"}))
            label = body.get("label", "").strip() or "Manual checkpoint"
            from .checkpoints import CheckpointStore
            active_sid = _sessions.active or ""
            store = CheckpointStore(repo, session_id=active_sid or None)
            checkpoint_id = store.snapshot(
                label=label, trigger="manual", session_id=active_sid or None
            )
            if checkpoint_id:
                return self._send(200, json.dumps({"ok": True, "id": checkpoint_id}))
            else:
                return self._send(400, json.dumps({"error": "Failed to create checkpoint snapshot"}))

        if path == "/api/codegraph/reindex":
            if not repo or not os.path.isdir(repo):
                return self._send(400, json.dumps({"error": "No open workspace"}))
            # Don't stack a second indexer on top of a running one -- concurrent
            # codegraph indexers collide on the same SQLite and wedge the panel.
            if _codegraph_index_alive():
                return self._send(200, json.dumps({"ok": True, "status": "indexing", "note": "already indexing"}))
            _reindex_codegraph_bg(repo)
            return self._send(200, json.dumps({"ok": True, "status": "indexing"}))
        if path == "/api/commands/render":
            name = body.get("name", "").strip()
            args = body.get("args", "")
            if not name:
                return self._send(400, json.dumps({"error": "Missing name parameter"}))
            rendered = _commands.render(name, args, repo=repo)
            if rendered is None:
                return self._send(404, json.dumps({"error": "unknown command"}))
            return self._send(200, json.dumps({"name": name, "prompt": rendered}))

        if path == "/api/inline-edit":
            if not repo or not os.path.exists(repo):
                return self._send(400, json.dumps({"error": "No open workspace"}))
            rel_path = body.get("path", "").strip()
            if not rel_path:
                return self._send(400, json.dumps({"error": "Missing path parameter"}))
            target_path = os.path.abspath(os.path.join(repo, rel_path))
            from .conversation import is_safe_path
            if not is_safe_path(target_path, repo):
                return self._send(400, json.dumps({"error": f"Path traversal attempt rejected: {rel_path}"}))
            
            selection = body.get("selection", "")
            instruction = body.get("instruction", "")
            prefix = body.get("prefix", "")
            suffix = body.get("suffix", "")
            language = body.get("language", "")
            
            if len(selection) > 20000:
                return self._send(400, json.dumps({"error": "Selection size exceeds 20000 characters limit"}))
            if len(prefix) > 4000:
                return self._send(400, json.dumps({"error": "Prefix size exceeds 4000 characters limit"}))
            if len(suffix) > 4000:
                return self._send(400, json.dumps({"error": "Suffix size exceeds 4000 characters limit"}))
            
            system_msg = (
                "You are a precise code-editing assistant. You rewrite ONLY the user's SELECTED code per their instruction. "
                "Output ONLY the replacement code for the selection -- no markdown fences, no explanation, no surrounding code. "
                "Preserve the surrounding indentation style. If the instruction cannot apply, output the selection unchanged."
            )
            
            task_prompt = (
                f"We are editing a file of language: {language or 'unknown'}.\n"
                f"File Path: {rel_path}\n\n"
                f"CONTEXT BEFORE THE SELECTION (Do not modify this, only use for context):\n"
                f"---BEGIN PREFIX---\n{prefix}\n---END PREFIX---\n\n"
                f"SELECTED CODE TO REWRITE:\n"
                f"---BEGIN SELECTION---\n{selection}\n---END SELECTION---\n\n"
                f"CONTEXT AFTER THE SELECTION (Do not modify this, only use for context):\n"
                f"---BEGIN SUFFIX---\n{suffix}\n---END SUFFIX---\n\n"
                f"INSTRUCTION: {instruction}\n\n"
                f"Please output ONLY the new rewritten code that will replace the SELECTED CODE TO REWRITE. "
                f"Do not output prefix context, suffix context, explanation, or markdown fences. Output the replacement code directly."
            )
            
            try:
                if not hasattr(_pilot, "pilot") or not _pilot.pilot:
                    return self._send(200, json.dumps({"ok": False, "error": "No pilot driver configured"}))
                
                resp = _pilot.pilot.complete(task_prompt, system=system_msg)
                if getattr(resp, "error", None):
                    return self._send(200, json.dumps({"ok": False, "error": resp.error}))
                
                cleaned_text = _strip_markdown_fences(resp.text)
                return self._send(200, json.dumps({"ok": True, "edit": cleaned_text}))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": f"Failed during inline edit pilot execution: {str(e)}"}))

        if path == "/api/file/write":
            if not repo or not os.path.exists(repo):
                return self._send(400, json.dumps({"error": "No open workspace"}))
            rel_path = body.get("path", "").strip()
            content = body.get("content", "")
            if not rel_path:
                return self._send(400, json.dumps({"error": "Missing path parameter"}))
            target_path = os.path.abspath(os.path.join(repo, rel_path))
            from .conversation import is_safe_path
            if not is_safe_path(target_path, repo):
                return self._send(403, json.dumps({"error": f"Path traversal attempt rejected: {rel_path}"}))
            parts = rel_path.split(os.sep)
            if ".git" in parts or any(p.startswith(".git") for p in parts):
                return self._send(403, json.dumps({"error": "Access denied: .git files are restricted"}))
            try:
                try:
                    from .checkpoints import CheckpointStore
                    active_sid = _sessions.active or ""
                    store = CheckpointStore(repo, session_id=active_sid or None)
                    store.snapshot(
                        label=f"before manual edit {rel_path}",
                        trigger="manual_edit",
                        session_id=active_sid or None,
                    )
                except Exception as cp_err:
                    import sys
                    print(f"Checkpoint error before write: {cp_err}", file=sys.stderr)
                
                target_dir = os.path.dirname(target_path)
                os.makedirs(target_dir, exist_ok=True)
                import tempfile
                fd, temp_path = tempfile.mkstemp(dir=target_dir, prefix=".tmp-")
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
                        f.write(content)
                    os.replace(temp_path, target_path)
                except Exception as e:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    raise e
                bytes_written = len(content.encode('utf-8'))
                return self._send(200, json.dumps({
                    "ok": True,
                    "bytes": bytes_written
                }))
            except Exception as e:
                return self._send(500, json.dumps({"error": f"Failed to write file: {e}"}))
        if path == "/api/workspace/open":
            import subprocess
            target_repo = body.get("path", "").strip()
            if not target_repo or not os.path.isdir(target_repo):
                return self._send(400, json.dumps({"error": "Path is not an existing directory"}))

            # Save outgoing conversation transcript for the current active runner
            _save_active_transcript()

            # Snapshot so a lease-exhausted attach can roll back without leaving
            # the process pointed at the target repo / session.
            prev_repo = _cfg.repo
            prev_driver = _cfg.driver
            prev_active = _sessions.active
            prev_env_repo = os.environ.get("HARNESS_REPO")

            _cfg.repo = target_repo
            os.environ["HARNESS_REPO"] = target_repo
            _note_boot_repo(target_repo)

            # Restore the model last used in this workspace (if any + still
            # available), so each dir remembers its model across switches.
            try:
                saved_driver = _get_workspace_driver(target_repo)
                if saved_driver and saved_driver != _cfg.driver:
                    from . import model_visibility as _mv
                    avail = {row["spec"] for row in _mv.catalog(available_only=True)}
                    if saved_driver in avail or not avail:
                        _cfg.driver = saved_driver
                        _apply_model_context_window()
            except Exception as e:
                _diag("server.restore_workspace_driver", e)

            try:
                recents = _record_recent_workspace(target_repo)
            except Exception as e:
                _diag("server.record_recent_workspace", e)

            is_git = False
            branch = ""
            try:
                proc = subprocess.run(
                    ["git", "-C", target_repo, "rev-parse", "--is-inside-work-tree"],
                    capture_output=True, text=True, timeout=5
                )
                if proc.returncode == 0:
                    is_git = True
                    proc_branch = subprocess.run(
                        ["git", "-C", target_repo, "rev-parse", "--abbrev-ref", "HEAD"],
                        capture_output=True, text=True, timeout=5
                    )
                    if proc_branch.returncode == 0:
                        branch = proc_branch.stdout.strip()
            except Exception:
                pass

            # Select/create the target project's session, then attach via registry
            # (do not rebuild in a way that orphans busy runners).
            target_sessions = [
                s for s in _sessions.list()
                if session_visible_for_workspace(s, target_repo, _sessions_state_dir())
            ]
            if target_sessions:
                newest_session = max(target_sessions, key=lambda s: s.get("created", 0))
                _sessions.switch(newest_session["id"])
            else:
                basename = os.path.basename(os.path.abspath(target_repo)) or "Workspace"
                _sessions.create(title=basename, repo=target_repo, branch=branch)

            if _sessions.active:
                try:
                    _attach_view(_sessions.active)
                except LeaseExhaustedError as e:
                    _cfg.repo = prev_repo
                    _cfg.driver = prev_driver
                    _apply_model_context_window()
                    if prev_env_repo is None:
                        os.environ.pop("HARNESS_REPO", None)
                    else:
                        os.environ["HARNESS_REPO"] = prev_env_repo
                    if prev_active:
                        try:
                            _sessions.switch(prev_active)
                        except Exception as roll_e:
                            _diag("server.workspace_open_lease_rollback", roll_e)
                    return self._send(409, json.dumps({
                        "error": str(e) or "session runner lease exhausted",
                        "code": "lease_exhausted",
                    }))

            has_codegraph = os.path.isdir(os.path.join(target_repo, ".codegraph"))
            if not has_codegraph:
                _index_codegraph_bg(target_repo)
            else:
                if _puppetmaster_available():
                    _codegraph_status = "ready"
                    _maybe_refresh_codegraph(target_repo)
                else:
                    _codegraph_status = "unsupported"

            return self._send(200, json.dumps({
                "ok": True,
                "repo": target_repo,
                "branch": branch,
                "is_git": is_git,
                "codegraph": _get_codegraph_status(target_repo),
                "active_session": _sessions.active
            }))

        if path == "/api/workspace/forget":
            target_repo = body.get("path", "").strip()
            if not target_repo:
                return self._send(400, json.dumps({"error": "Path is required"}))
            try:
                recents = _forget_recent_workspace(target_repo)
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
            return self._send(200, json.dumps({
                "ok": True,
                "recents": recents
            }))

        if path == "/api/workspaces/switch":
            return self._send(200, json.dumps(_ws.switch_workspace(repo, body.get("name",""),
                              allow_dirty=_parse_bool(body.get("allow_dirty")))))
        if path == "/api/workspaces/create":
            return self._send(200, json.dumps(_ws.create_workspace(repo, body.get("name",""),
                              body.get("branch") or None)))
        if path == "/api/mcp/add":
            name = body.get("name", "")
            server = {k: body[k] for k in ("command", "args", "env", "cwd", "url", "headers") if k in body}
            _mcp.save_server(name, server)
            try:
                tools = _mcp.start_server(name)
                return self._send(200, json.dumps({"ok": True, "tools": len(tools)}))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}))
        if path == "/api/mcp/remove":
            _mcp.remove_server(body.get("name", ""))
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/mcp/start":
            try:
                tools = _mcp.start_server(body.get("name", ""))
                return self._send(200, json.dumps({"ok": True, "tools": len(tools)}))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}))
        if path == "/api/mcp/stop":
            _mcp.stop_server(body.get("name", ""))
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/mcp/call":
            args = body.get("arguments")
            if args is not None and not isinstance(args, dict):
                return self._send(400, json.dumps({"error": "arguments must be a dictionary"}))
            try:
                out = _mcp.call(body.get("tool", ""), args or {})
                return self._send(200, json.dumps({"ok": True, "result": out}))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}))
        if path == "/api/skills/distill":
            return self._send(200, json.dumps(_pilot.distill()))
        if path == "/api/wiki/ingest-prepared":
            # One-click approve: file the locally-orchestrated pages into the wiki.
            pages = body.get("pages") or []
            count = _pilot.ingest_prepared_pages(pages)
            return self._send(200, json.dumps({"ok": count > 0, "ingested": count}))
        if path == "/api/models/toggle":
            from . import model_visibility as _mv
            spec = body.get("spec", "")
            on = _parse_bool(body.get("enabled", True))
            enabled = _mv.toggle(spec, on)
            return self._send(200, json.dumps({"ok": True, "enabled": enabled}))
        if path == "/api/models/set":
            from . import model_visibility as _mv
            enabled = _mv.set_enabled(body.get("enabled") or [])
            return self._send(200, json.dumps({"ok": True, "enabled": enabled}))
        if path == "/api/skills/approve":
            sk = _skills.set_state(body.get("slug", ""), "active")
            return self._send(200, json.dumps({"ok": bool(sk)}))
        if path == "/api/skills/add":
            name = (body.get("name") or "").strip()
            if not name:
                return self._send(400, json.dumps({"error": "name is required"}))
            from .skill_store import Skill
            sk = Skill(
                name=name,
                description=(body.get("description") or "").strip(),
                body=(body.get("body") or "").strip(),
                state="active",
                source="manual",
            )
            _skills.save(sk)
            return self._send(200, json.dumps({
                "ok": True,
                "slug": sk.slug,
                "name": sk.name,
                "state": sk.state,
                "source": sk.source,
            }))
        if path == "/api/skills/update":
            slug = (body.get("slug") or "").strip()
            if not slug:
                return self._send(400, json.dumps({"error": "slug is required"}))
            sk = _skills.update(
                slug,
                name=body.get("name"),
                description=body.get("description"),
                body=body.get("body"),
            )
            if not sk:
                return self._send(404, json.dumps({"error": "skill not found"}))
            return self._send(200, json.dumps({
                "ok": True,
                "slug": sk.slug,
                "name": sk.name,
                "description": sk.description,
                "state": sk.state,
            }))
        if path == "/api/skills/remove":
            ok = _skills.remove(body.get("slug", ""))
            return self._send(200, json.dumps({"ok": ok}))
        if path == "/api/skills/reject":
            _skills.set_state(body.get("slug", ""), "archived")
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/skills/archive":
            _skills.set_state(body.get("slug", ""), "archived")
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/rules/approve":
            ok = _rules.set_state(body.get("slug", ""), "active")
            return self._send(200, json.dumps({"ok": ok}))
        if path == "/api/rules/add":
            text = (body.get("text") or "").strip()
            if not text:
                return self._send(400, json.dumps({"error": "text is required"}))
            from .rule_store import Rule
            rule = Rule(
                text=text,
                scope=(body.get("scope") or "global").strip() or "global",
                state="active",
                source="manual",
            )
            _rules.add(rule)
            return self._send(200, json.dumps({
                "ok": True,
                "slug": rule.slug,
                "text": rule.text,
                "scope": rule.scope,
                "state": rule.state,
                "source": rule.source,
            }))
        if path == "/api/rules/update":
            slug = (body.get("slug") or "").strip()
            if not slug:
                return self._send(400, json.dumps({"error": "slug is required"}))
            rule = _rules.update(slug, text=body.get("text"), scope=body.get("scope"))
            if not rule:
                return self._send(404, json.dumps({"error": "rule not found"}))
            return self._send(200, json.dumps({
                "ok": True,
                "slug": rule.slug,
                "text": rule.text,
                "scope": rule.scope,
                "state": rule.state,
            }))
        if path == "/api/rules/remove":
            ok = _rules.remove(body.get("slug", ""))
            return self._send(200, json.dumps({"ok": ok}))
        if path == "/api/rules/reject":
            _rules.set_state(body.get("slug", ""), "archived")
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/memory/add":
            text = body.get("text", "")
            category = body.get("category", "general")
            entry = _memory.add(text, category=category, source="user")
            return self._send(200, json.dumps({
                "id": entry.id,
                "text": entry.text,
                "category": entry.category,
                "created_at": entry.created_at,
                "source": entry.source
            }))
        if path == "/api/memory/remove":
            entry_id = body.get("id", "")
            ok = _memory.remove(entry_id)
            return self._send(200, json.dumps({"ok": ok}))
        if path == "/api/memory/propose/accept":
            proposal_id = (body.get("id") or "").strip()
            if not proposal_id:
                return self._send(400, json.dumps({"ok": False, "error": "missing id"}))
            result = _pilot.accept_memory_proposal(proposal_id)
            code = 200 if result.get("ok") else 404
            return self._send(code, json.dumps(result))
        if path == "/api/memory/propose/dismiss":
            proposal_id = (body.get("id") or "").strip()
            if not proposal_id:
                return self._send(400, json.dumps({"ok": False, "error": "missing id"}))
            result = _pilot.dismiss_memory_proposal(proposal_id)
            code = 200 if result.get("ok") else 404
            return self._send(code, json.dumps(result))
        if path == "/api/sessions/create":
            _save_active_transcript()
            # Snapshot so a lease-exhausted attach can roll back without leaving
            # the store pointed at an unattached session.
            prev_active = _sessions.active
            title = body.get("title") or "New session"
            repo = _cfg.repo or ""
            branch = ""
            if repo and os.path.isdir(repo):
                import subprocess
                try:
                    proc = subprocess.run(
                        ["git", "-C", repo, "rev-parse", "--is-inside-work-tree"],
                        capture_output=True, text=True, timeout=5
                    )
                    if proc.returncode == 0:
                        proc_branch = subprocess.run(
                            ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True, timeout=5
                        )
                        if proc_branch.returncode == 0:
                            branch = proc_branch.stdout.strip()
                except Exception:
                    pass
            res = _sessions.create(title, repo=repo, branch=branch, workspace_root=repo)
            sid = res.get("id", "")
            if sid:
                try:
                    # New session runner starts at zero meters (boot pill sums
                    # carry + all live runners -- do not snapshot from active).
                    _attach_view(
                        sid,
                        load_transcript_on_create=False,
                    )
                    _pilot.load_history([])
                except LeaseExhaustedError as e:
                    try:
                        _sessions.delete(sid)
                    except Exception as roll_e:
                        _diag("server.session_create_lease_delete", roll_e)
                    if prev_active:
                        try:
                            _sessions.switch(prev_active)
                        except Exception as roll_e:
                            _diag("server.session_create_lease_rollback", roll_e)
                    return self._send(409, json.dumps({
                        "error": str(e) or "session runner lease exhausted",
                        "code": "lease_exhausted",
                    }))

            from .hooks import run_hooks
            run_hooks("sessionStart", {"session_id": sid, "title": title})

            return self._send(200, json.dumps(res))
        if path == "/api/sessions/switch":
            # Multi-session: switching VIEW must not 409 just because the
            # outgoing (or another) runner is busy -- other sessions keep
            # executing under the lease. Only LeaseExhaustedError blocks.
            target_id = (body.get("id") or "").strip()
            _save_active_transcript()
            # Snapshot so a lease-exhausted attach can roll back active + repo.
            prev_active = _sessions.active
            prev_repo = _cfg.repo
            prev_env_repo = os.environ.get("HARNESS_REPO")
            res = _sessions.switch(target_id)
            if res.get("ok") and _sessions.active:
                target_sess = None
                for s in _sessions.list():
                    if s.get("id") == _sessions.active:
                        target_sess = s
                        break
                target_repo = ""
                if target_sess:
                    target_repo = (
                        session_stored_root(target_sess)
                        or (target_sess.get("repo") or "").strip()
                    )

                # Never let a stale app-checkout session yank the live workspace
                # back to ~/.marionette/marionette (or the running source tree).
                # Conversation view still switches; only the project root is kept.
                if (
                    target_repo
                    and os.path.isdir(target_repo)
                    and target_repo != _cfg.repo
                    and not _is_app_install_root(target_repo)
                ):
                    _cfg.repo = target_repo
                    os.environ["HARNESS_REPO"] = target_repo
                    _note_boot_repo(target_repo)
                    # Session-switch repoints must land in recents too, or the
                    # dir only exists in the projects list while it is current
                    # and vanishes the moment the workspace moves elsewhere.
                    try:
                        _record_recent_workspace(target_repo)
                    except Exception as e:
                        _diag("server.session_switch_record_recent", e)

                    has_codegraph = os.path.isdir(os.path.join(target_repo, ".codegraph"))
                    if not has_codegraph:
                        _index_codegraph_bg(target_repo)
                    else:
                        if _puppetmaster_available():
                            _codegraph_status = "ready"
                            _maybe_refresh_codegraph(target_repo)
                        else:
                            _codegraph_status = "unsupported"

                try:
                    _attach_view(_sessions.active)
                except LeaseExhaustedError as e:
                    if prev_active:
                        try:
                            _sessions.switch(prev_active)
                        except Exception as roll_e:
                            _diag("server.session_switch_lease_rollback", roll_e)
                    if _cfg.repo != prev_repo:
                        _cfg.repo = prev_repo
                        if prev_env_repo is None:
                            os.environ.pop("HARNESS_REPO", None)
                        else:
                            os.environ["HARNESS_REPO"] = prev_env_repo
                    return self._send(409, json.dumps({
                        "error": str(e) or "session runner lease exhausted",
                        "code": "lease_exhausted",
                    }))

                res["repo"] = _cfg.repo
                res["codegraph"] = _get_codegraph_status(_cfg.repo) if _cfg.repo else "unsupported"

            return self._send(200, json.dumps(res))
        if path == "/api/sessions/delete":
            sid = body.get("session") or body.get("id") or ""
            status, payload = _handle_session_delete(sid)
            return self._send(status, json.dumps(payload))
        if path == "/api/sessions/clear":
            repo_root = _cfg.repo or ""
            state_dir = _sessions_state_dir()
            prior_active = _sessions.active
            deleted_ids, new_active = _sessions.clear_for_workspace(repo_root, state_dir)
            from .hooks import run_hooks
            for sid in deleted_ids:
                run_hooks("sessionEnd", {"session_id": sid})
                _remove_session_transcript(sid)
                try:
                    _runners.drop(sid)
                except Exception as e:
                    _diag("server.session_clear_drop_runner", e)
            if prior_active in deleted_ids:
                if new_active:
                    try:
                        _attach_view(new_active)
                    except LeaseExhaustedError:
                        history = load_transcript(state_dir, new_active)
                        _pilot.load_history(history)
                        _sync_pilot_session_id()
                else:
                    _pilot.load_history([])
                    _sync_pilot_session_id()
            return self._send(200, json.dumps({
                "ok": True,
                "deleted": len(deleted_ids),
                "active": new_active,
            }))
        if path == "/api/sessions/archive":
            sid = body.get("session") or body.get("id") or ""
            if not sid:
                return self._send(400, json.dumps({"error": "missing session id"}))
            archived = _parse_bool(body.get("archived"))
            _sessions.archive(sid, archived)
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/sessions/rename":
            sid = body.get("session") or body.get("id") or ""
            title = body.get("title") or ""
            if not sid:
                return self._send(400, json.dumps({"error": "missing session id"}))
            if not title:
                return self._send(400, json.dumps({"error": "missing title"}))
            ok = _sessions.rename(sid, title)
            return self._send(200, json.dumps({"ok": ok}))
        if path == "/api/chat/stash":
            # Companion to GET /api/chat's ?mid= param (see _CHAT_STASH above).
            # A large paste or autopilot objective can't fit in the URL that
            # EventSource requires for the SSE GET, so the client POSTs it
            # here first and gets back a short id to reference instead.
            message = body.get("message", "")
            images = body.get("images") or []
            if isinstance(images, str):
                images = [p for p in images.split("|") if p]
            if not message and not images:
                return self._send(400, json.dumps({"error": "missing message"}))
            mid = _stash_put(message, images)
            return self._send(200, json.dumps({"id": mid}))
        if path == "/api/session/interrupt":
            _pilot.interrupt()
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/session/steer":
            text = body.get("text", "").strip()
            images = body.get("images") or []
            if isinstance(images, str):
                images = [p for p in images.split("|") if p]
            if not text and not images:
                return self._send(400, json.dumps({"error": "missing text"}))
            if not _pilot:
                return self._send(404, json.dumps({"error": "no active session"}))
            # Validate every image path lives inside the upload dir (mirror
            # /api/session/queue, /api/run, and /api/chat validation).
            valid_imgs = []
            upload_dir_real = os.path.realpath(_UPLOAD_DIR)
            for p in images:
                if not p:
                    continue
                real_p = os.path.realpath(p)
                try:
                    if os.path.commonpath([upload_dir_real, real_p]) == upload_dir_real:
                        valid_imgs.append(p)
                    else:
                        return self._send(400, json.dumps({"error": f"Invalid image path: {p}"}))
                except ValueError:
                    return self._send(400, json.dumps({"error": f"Invalid image path: {p}"}))
            # Route through steer_with_images so an attached screenshot is
            # transcribed into the steer text (a steer is a text injection and
            # cannot carry raw image blocks mid-turn).
            if valid_imgs and hasattr(_pilot, "steer_with_images"):
                _pilot.steer_with_images(text, valid_imgs)
            else:
                _pilot.enqueue_steer(text)
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/session/queue":
            # PROMPT QUEUE mutations, mirroring the auth/token guard already
            # applied to every POST. Distinct from /api/session/steer: the queue
            # is a "playlist" of FULL user prompts that each run as their own
            # complete turn once the current one finishes. Never raises.
            if not _pilot:
                return self._send(404, json.dumps({"error": "no active session"}))
            # DELETE-style body: {clear: true} clears the queue; {id: "..."}
            # removes a single item. We accept these via POST so the browser
            # transport shim (which lacks a DELETE helper) can drive both.
            if body.get("clear") is True:
                try:
                    n = _pilot.clear_prompts()
                except Exception:
                    n = 0
                return self._send(200, json.dumps({"ok": True, "cleared": n}))
            rid = (body.get("id") or "").strip() if isinstance(body.get("id"), str) else ""
            if rid:
                try:
                    ok = _pilot.remove_prompt(rid)
                except Exception:
                    ok = False
                return self._send(200, json.dumps({"ok": bool(ok), "id": rid}))
            # Otherwise: enqueue a new prompt.
            text = (body.get("text") or "").strip()
            if not text:
                return self._send(400, json.dumps({"error": "missing text"}))
            # Optional image attachments: accept a list or a '|'-joined string
            # (mirror the steer endpoint) and validate every path lives inside
            # the upload dir (mirror /api/run and /api/chat validation). A queued
            # prompt runs as its own fresh turn so it can carry real images.
            images = body.get("images") or []
            if isinstance(images, str):
                images = [p for p in images.split("|") if p]
            valid_imgs = []
            upload_dir_real = os.path.realpath(_UPLOAD_DIR)
            for p in images:
                if not p:
                    continue
                real_p = os.path.realpath(p)
                try:
                    if os.path.commonpath([upload_dir_real, real_p]) == upload_dir_real:
                        valid_imgs.append(p)
                    else:
                        return self._send(400, json.dumps({"error": f"Invalid image path: {p}"}))
                except ValueError:
                    return self._send(400, json.dumps({"error": f"Invalid image path: {p}"}))
            try:
                item = _pilot.enqueue_prompt(text, images=valid_imgs)
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
            if not item or not item.get("id"):
                return self._send(400, json.dumps({"error": "enqueue failed"}))
            return self._send(200, json.dumps({"ok": True, "item": item}))
        if path == "/api/session/queue/reorder":
            if not _pilot:
                return self._send(404, json.dumps({"error": "no active session"}))
            ids = body.get("ids") or []
            if not isinstance(ids, list):
                return self._send(400, json.dumps({"error": "ids must be a list"}))
            try:
                items = _pilot.reorder_prompts([str(x) for x in ids])
            except Exception:
                try:
                    items = _pilot.list_prompts()
                except Exception:
                    items = []
            return self._send(200, json.dumps({"ok": True, "items": items}))
        if path == "/api/terminal/create":
            try:
                # Reap any dead PTY sessions first so exited/stuck terminals do
                # not pile up across restarts (the Restart button creates a fresh
                # session each time; the old dead ones should be cleaned up).
                _pty.reap()
                cwd = _cfg.repo or os.path.expanduser("~")
                cols = int(body.get("cols", 80)); rows = int(body.get("rows", 24))
                sess = _pty.create(cwd=cwd, cols=cols, rows=rows)
                return self._send(200, json.dumps({"id": sess.id, "cwd": sess._cwd}))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
        if path == "/api/terminal/write":
            sess = _pty.get(body.get("id", ""))
            if not sess:
                return self._send(404, json.dumps({"error": "no such terminal"}))
            sess.write(body.get("data", ""))
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/terminal/resize":
            sess = _pty.get(body.get("id", ""))
            if not sess:
                return self._send(404, json.dumps({"error": "no such terminal"}))
            sess.resize(int(body.get("rows", 24)), int(body.get("cols", 80)))
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/terminal/kill":
            _pty.kill(body.get("id", ""))
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/wiki/config":
            api_base = body.get("api_base")
            owner_token = body.get("owner_token")
            res = set_wiki_config(
                api_base=api_base if api_base is not None else None,
                owner_token=owner_token if owner_token is not None else None,
            )
            return self._send(200, json.dumps(res))
        if path == "/api/git/connect":
            method = body.get("method")
            if method not in ("gh", "device"):
                return self._send(400, json.dumps({"error": f"Invalid method: {method}"}))
            from .git_provision import GitProvisioner, save_connection, get_status
            prov = GitProvisioner()
            if method == "gh":
                info = prov.detect_gh()
                if not info["available"]:
                    return self._send(400, json.dumps({"error": "GitHub CLI not authenticated or not installed"}))
                token = prov.github_token()
                if not token:
                    return self._send(400, json.dumps({"error": "Could not retrieve GitHub CLI token"}))
                res = prov.provision_wiki_repo(token)
                if not res.get("ok"):
                    return self._send(500, json.dumps({"error": res.get("error", "Failed to provision repository")}))
                save_connection("gh", res["repo_full_name"], res["html_url"])
                return self._send(200, json.dumps(get_status()))
            elif method == "device":
                res = prov.device_flow_start()
                if "error" in res:
                    return self._send(500, json.dumps({"error": res["error"]}))
                return self._send(200, json.dumps(res))
        if path == "/api/git/device/poll":
            device_code = body.get("device_code")
            if not device_code:
                return self._send(400, json.dumps({"error": "Missing device_code"}))
            from .git_provision import GitProvisioner, save_connection, save_device_token, get_status
            prov = GitProvisioner()
            res = prov.device_flow_poll(None, device_code)
            if res.get("status") == "authorized":
                token = res.get("token")
                if not token:
                    return self._send(500, json.dumps({"error": "No token in authorized response"}))
                repo_res = prov.provision_wiki_repo(token)
                if not repo_res.get("ok"):
                    return self._send(500, json.dumps({"error": repo_res.get("error", "Failed to provision repository")}))
                save_device_token(token)
                save_connection("device", repo_res["repo_full_name"], repo_res["html_url"])
                return self._send(200, json.dumps(get_status()))
            elif res.get("status") == "pending":
                return self._send(200, json.dumps({"status": "pending"}))
            else:
                return self._send(400, json.dumps({"error": res.get("error", "Verification failed")}))
        if path == "/api/git/disconnect":
            from .git_provision import delete_connection, get_status
            delete_connection()
            return self._send(200, json.dumps(get_status()))
        if path == "/api/platform":
            name = body.get("name")
            enabled = body.get("enabled")
            if name not in ("agentic", "cursor", "hermes", "claude-code", "codex", "openai"):
                return self._send(400, json.dumps({"error": f"Unknown adapter: {name}"}))
            if not isinstance(enabled, bool):
                return self._send(400, json.dumps({"error": "enabled must be a boolean"}))
            
            path_file = _get_platform_json_path()
            pdata = {}
            if os.path.exists(path_file):
                try:
                    with open(path_file, "r", encoding="utf-8") as f:
                        pdata = json.load(f)
                except Exception as e:
                    _diag("server.platform_toggle_load", e)
            if not isinstance(pdata, dict):
                pdata = {}
            if "disabled" not in pdata or not isinstance(pdata["disabled"], list):
                pdata["disabled"] = []
            
            disabled_list = pdata["disabled"]
            if enabled:
                pdata["disabled"] = [x for x in disabled_list if x != name]
            else:
                if name not in disabled_list:
                    pdata["disabled"] = disabled_list + [name]
            
            try:
                _write_platform_json_atomic(path_file, pdata)
            except Exception as e:
                return self._send(500, json.dumps({"error": f"Failed to save platform.json: {str(e)}"}))
            
            return self._send(200, json.dumps(_get_platform_adapters()))
        if path == "/api/settings":
            requires_rebuild = False
            if "api_key" in body or body.get("clear_api_key") is True:
                requires_rebuild = True
            driver = body.get("driver")
            if driver is not None and driver != _cfg.driver:
                requires_rebuild = True
            if requires_rebuild:
                if not _pilot._busy.acquire(blocking=False):
                    return self._send(409, json.dumps({"error": "pilot busy, try again"}))
                _pilot._busy.release()

            reach_to_use = body.get("reach", _cfg.reach)
            if "api_key" in body:
                val = str(body["api_key"]).strip()
                if val:
                    set_api_key(reach_to_use, val)
                    _rebuild_pilot_and_session()
                    # Resync agentic registry when a key is added
                    from .auto_registry import sync_agentic_registry_safe
                    sync_agentic_registry_safe()
            elif body.get("clear_api_key") is True:
                clear_api_key(reach_to_use)
                _rebuild_pilot_and_session()
                # Resync agentic registry when a key is cleared
                from .auto_registry import sync_agentic_registry_safe
                sync_agentic_registry_safe()

            driver = body.get("driver")
            if driver is not None:
                # Validate against the FULL available catalog (every model from a
                # keyed provider), not just the enabled picker subset -- a user may
                # set a driver that is valid but not currently toggled into the
                # dropdown. _available_pilots() is the curated picker list; the
                # catalog is the superset of what can actually be built.
                from . import model_visibility as _mv
                catalog_specs = {c["spec"] for c in _mv.catalog(available_only=True)}
                av = set(_available_pilots()) | catalog_specs
                if driver not in av:
                    return self._send(400, json.dumps({"error": f"Unknown or unavailable driver: {driver}"}))
                if driver != _cfg.driver:
                    try:
                        _cfg.driver = driver
                        _rebuild_pilot_and_session()
                        # Persist like /api/pilot/swap does -- a settings-page
                        # change that only lived in _cfg silently reverted to
                        # the compiled-in default on the next backend start.
                        _save_workspace_driver(_cfg.repo, driver)
                    except Exception as e:
                        return self._send(500, json.dumps({"error": f"Failed to swap driver: {str(e)}"}))
            budget = body.get("budget")
            if budget is not None:
                try:
                    b_val = int(budget)
                    _cfg.budget = max(1, min(50, b_val))
                except (ValueError, TypeError):
                    return self._send(400, json.dumps({"error": "Invalid budget value"}))
            def _set_env_setting(env_var: str, value: str) -> None:
                os.environ[env_var] = value
                _persist_env_setting(env_var, value)

            if "auto_distill" in body:
                ad_val = _parse_bool(body["auto_distill"])
                _pilot._auto_distill = ad_val
                _set_env_setting("HARNESS_AUTO_DISTILL", "true" if ad_val else "false")
            if "reviewEditsBeforeApply" in body:
                rev_val = _parse_bool(body["reviewEditsBeforeApply"])
                _pilot._review_edits_before_apply = rev_val
                _set_env_setting("HARNESS_REVIEW_EDITS_BEFORE_APPLY", "true" if rev_val else "false")
            if "autoCommandGuard" in body:
                g_val = _parse_bool(body["autoCommandGuard"])
                _pilot._auto_command_guard = g_val
                _set_env_setting("HARNESS_AUTO_COMMAND_GUARD", "true" if g_val else "off")
            if "autoVerify" in body:
                av_val = _parse_bool(body["autoVerify"])
                _cfg.auto_verify = av_val
                _set_env_setting("HARNESS_AUTO_VERIFY", "true" if av_val else "false")
            if "hash_edit_enabled" in body:
                he_val = _parse_bool(body["hash_edit_enabled"])
                _set_env_setting("HARNESS_HASH_EDIT", "1" if he_val else "0")
            if "verifyCommand" in body:
                vc_val = str(body["verifyCommand"]).strip()
                _cfg.verify_command = vc_val
                _set_env_setting("HARNESS_VERIFY_COMMAND", vc_val)
            if "commandTimeout" in body:
                # seconds; "0"/"off"/"none" = unbounded. Validate before storing.
                raw = str(body["commandTimeout"]).strip().lower()
                if raw in ("0", "off", "none", "unbounded"):
                    _set_env_setting("HARNESS_COMMAND_TIMEOUT", "0")
                else:
                    try:
                        _set_env_setting("HARNESS_COMMAND_TIMEOUT", str(max(1, int(raw))))
                    except (ValueError, TypeError):
                        return self._send(400, json.dumps({"error": "Invalid commandTimeout"}))
            if "maxPilotSteps" in body:
                # Per-message pilot step ceiling; "0"/"unlimited" = no cap. The
                # Settings page always sent this, but the server silently
                # ignored it -- the field looked saved and never took effect.
                raw = str(body["maxPilotSteps"]).strip().lower()
                if raw in ("0", "off", "none", "unlimited"):
                    _set_env_setting("HARNESS_MAX_PILOT_STEPS", "0")
                else:
                    try:
                        _set_env_setting("HARNESS_MAX_PILOT_STEPS", str(max(1, int(raw))))
                    except (ValueError, TypeError):
                        return self._send(400, json.dumps({"error": "Invalid maxPilotSteps"}))

            return self._send(200, json.dumps(_get_settings_dict()))

        if path == "/api/providers/probe":
            pname = body.get("provider", "")
            from .providers import get_provider
            p = get_provider(pname)
            if not p:
                return self._send(400, json.dumps({"error": f"Unknown provider: {pname}"}))
            
            from .registry_wizard import get_provider_key, probe_provider
            key = get_provider_key(p)
            try:
                res = probe_provider(p, key)
                return self._send(200, json.dumps(res))
            except Exception as e:
                return self._send(200, json.dumps({
                    "provider": p.name,
                    "models": [{"id": m} for m in p.pilot_models],
                    "source": "static",
                    "error": str(e)
                }))

        if path == "/api/providers/key":
            # Per-provider key management: set or disconnect a SPECIFIC provider's
            # key independently (e.g. turn OpenRouter off while keeping Anthropic).
            # Distinct from /api/settings, which only touches the active reach.
            pname = str(body.get("provider", "")).strip()
            from .providers import get_provider
            p = get_provider(pname)
            if not p:
                return self._send(400, json.dumps({"error": f"Unknown provider: {pname}"}))
            action = str(body.get("action", "")).strip().lower()
            if action in ("enable", "disable", "toggle"):
                # Non-destructive on/off for env-imported (or stored) keys. Unlike
                # 'clear', this preserves the key so the user can flip a provider
                # off and back on -- e.g. swapping a work key for a personal one.
                from .keys import set_provider_enabled, get_disconnected
                if action == "toggle":
                    enabled = p.name in get_disconnected()
                else:
                    enabled = action == "enable"
                set_provider_enabled(p.name, enabled)
                # Resync agentic registry when a provider is enabled/disabled
                from .auto_registry import sync_agentic_registry_safe
                sync_agentic_registry_safe()
                # Keep the active driver honest: enabling may make a better model
                # reachable; disabling may kill the current one.
                try:
                    if not _driver_provider_available(_cfg.driver):
                        _resolve_available_driver()
                        _rebuild_pilot_and_session()
                except Exception as e:
                    _diag("server.provider_toggle_driver_rebuild", e)
                status = get_api_key_status(p.name)
                return self._send(200, json.dumps({
                    "ok": True,
                    "provider": p.name,
                    "enabled": enabled,
                    "has_key": status["has_key"],
                    "masked": status["masked"],
                }))
            if action == "clear" or body.get("clear") is True:
                clear_api_key(p.name)
                # Resync agentic registry when a provider key is cleared
                from .auto_registry import sync_agentic_registry_safe
                sync_agentic_registry_safe()
                # If the active driver's provider is no longer available (we just
                # disconnected the provider backing it -- whether a 'provider:model'
                # spec OR a bare name routed through the reach), re-resolve to the
                # first available enabled model and rebuild, so the app never sits
                # on a dead driver.
                try:
                    if not _driver_provider_available(_cfg.driver):
                        _resolve_available_driver()
                        _rebuild_pilot_and_session()
                except Exception as e:
                    _diag("server.provider_clear_driver_rebuild", e)
            else:
                val = str(body.get("api_key", "")).strip()
                if not val:
                    return self._send(400, json.dumps({"error": "api_key required to set"}))
                set_api_key(p.name, val)
                # Resync agentic registry when a provider key is set
                from .auto_registry import sync_agentic_registry_safe
                sync_agentic_registry_safe()
            status = get_api_key_status(p.name)
            return self._send(200, json.dumps({
                "ok": True,
                "provider": p.name,
                "has_key": status["has_key"],
                "masked": status["masked"],
            }))

        if path == "/api/registry":
            models = body.get("models")
            if not isinstance(models, list):
                return self._send(400, json.dumps({"error": "models must be a list"}))
            
            validated_models = []
            for m in models:
                if not isinstance(m, dict):
                    return self._send(400, json.dumps({"error": "each model must be a dictionary"}))
                
                model_id = m.get("id")
                if not isinstance(model_id, str) or not model_id.strip():
                    return self._send(400, json.dumps({"error": "id must be a non-empty string"}))
                
                adapter = m.get("adapter")
                if not isinstance(adapter, str):
                    return self._send(400, json.dumps({"error": "adapter must be a string"}))
                
                try:
                    score = int(m.get("capability_score", 0))
                    score = max(0, min(100, score))
                except (ValueError, TypeError):
                    return self._send(400, json.dumps({"error": "capability_score must be an integer"}))
                
                m["id"] = model_id.strip()
                m["adapter"] = adapter
                m["capability_score"] = score
                validated_models.append(m)
                
            from .registry_wizard import get_models_file_path, write_json_atomic
            dest_path = get_models_file_path()
            try:
                write_json_atomic(dest_path, {"models": validated_models})
                return self._send(200, json.dumps({"ok": True, "models": validated_models}))
            except Exception as e:
                return self._send(500, json.dumps({"error": f"Failed to write registry: {str(e)}"}))

        if path == "/api/roles":
            overrides = body.get("overrides", {})
            policy = body.get("routing_policy")
            
            if not isinstance(overrides, dict):
                return self._send(400, json.dumps({"error": "overrides must be a dictionary"}))
            
            validated_overrides = {}
            from .registry_wizard import REAL_BASE_SCORES
            for role, score in overrides.items():
                if role not in REAL_BASE_SCORES:
                    return self._send(400, json.dumps({"error": f"Unknown role: {role}"}))
                try:
                    clamped_score = max(0, min(100, int(score)))
                    validated_overrides[role] = clamped_score
                except (ValueError, TypeError):
                    return self._send(400, json.dumps({"error": f"Invalid score for role {role}: {score}"}))
            
            if policy is not None:
                valid_policies = {"balanced", "cheap", "quality", "escalating"}
                if policy not in valid_policies:
                    return self._send(400, json.dumps({"error": f"Invalid policy: {policy}; expected one of {list(valid_policies)}"}))
            
            from .registry_wizard import get_routing_file_path, write_json_atomic
            dest_path = get_routing_file_path()
            current_data = {}
            if os.path.exists(dest_path):
                try:
                    with open(dest_path, encoding="utf-8", errors="replace") as f:
                        current_data = json.load(f)
                except Exception as e:
                    _diag("server.routing_overrides_load", e)
            
            current_overrides = current_data.get("overrides", {})
            current_overrides.update(validated_overrides)
            current_data["overrides"] = current_overrides
            
            if policy is not None:
                current_data["routing_policy"] = policy
            elif "routing_policy" not in current_data:
                current_data["routing_policy"] = "balanced"
                
            try:
                write_json_atomic(dest_path, current_data, chmod_mode=0o600)
                return self._send(200, json.dumps({"ok": True, "overrides": current_data["overrides"], "routing_policy": current_data["routing_policy"]}))
            except Exception as e:
                return self._send(500, json.dumps({"error": f"Failed to save roles config: {str(e)}"}))

        if path == "/api/pilot/validate":
            driver = body.get("driver")
            if not isinstance(driver, str):
                return self._send(400, json.dumps({"error": "driver must be a string"}))
                
            from .registry_wizard import validate_pilot_driver
            try:
                res = validate_pilot_driver(driver)
                return self._send(200, json.dumps(res))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))

        if path == "/api/worktrees/add":
            from . import worktrees as _wt
            branch = body.get("branch", "").strip()
            base = body.get("base") or "HEAD"
            if not branch or branch.startswith("-") or (base and base.startswith("-")):
                return self._send(400, json.dumps({"error": "invalid branch or base name"}))
            try:
                new_wt = _wt.add_worktree(_cfg.repo, branch, base)
                _wt.cleanup_old_worktrees(_cfg.repo, _wt.get_max_worktrees())
                return self._send(200, json.dumps(new_wt))
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}))
            except Exception as e:
                return self._send(400, json.dumps({"error": f"Failed to add worktree: {str(e)}"}))

        if path == "/api/worktrees/remove":
            from . import worktrees as _wt
            wt_path = body.get("path", "").strip()
            force = _parse_bool(body.get("force"))
            if not wt_path:
                return self._send(400, json.dumps({"error": "missing path"}))
            try:
                _wt.remove_worktree(_cfg.repo, wt_path, force=force)
                return self._send(200, json.dumps({"ok": True}))
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}))
            except Exception as e:
                return self._send(400, json.dumps({"error": f"Failed to remove worktree: {str(e)}"}))

        if path == "/api/worktrees/prune":
            from . import worktrees as _wt
            try:
                _wt.prune_worktrees(_cfg.repo)
                return self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                return self._send(400, json.dumps({"error": f"Failed to prune worktrees: {str(e)}"}))

        if path == "/api/worktrees/prune-edit-branches":
            from . import worktrees as _wt
            try:
                result = _wt.prune_orphan_edit_branches(_cfg.repo)
                return self._send(200, json.dumps({
                    "ok": True,
                    "deleted": result.get("deleted", []),
                    "count": int(result.get("count", 0) or 0),
                }))
            except Exception as e:
                return self._send(400, json.dumps({
                    "error": f"Failed to prune edit branches: {str(e)}",
                }))

        if path == "/api/worktrees/max":
            from . import worktrees as _wt
            try:
                max_val = int(body.get("max") or body.get("max_worktrees") or 25)
                _wt.set_max_worktrees(max_val)
                _wt.cleanup_old_worktrees(_cfg.repo, max_val)
                return self._send(200, json.dumps({"ok": True}))
            except (ValueError, TypeError):
                return self._send(400, json.dumps({"error": "Invalid max value"}))

        if path == "/api/hooks/add":
            from . import hooks as _hk
            event = body.get("event", "").strip()
            command = body.get("command", "").strip()
            if event not in _hk.ALLOWED_EVENTS:
                return self._send(400, json.dumps({"error": f"Invalid event. Allowed: {_hk.ALLOWED_EVENTS}"}))
            if not command:
                return self._send(400, json.dumps({"error": "Command cannot be empty"}))
            
            hooks = _hk.get_hooks()
            new_hook = {
                "id": uuid.uuid4().hex[:12],
                "event": event,
                "command": command,
                "enabled": True
            }
            hooks.append(new_hook)
            _hk.save_hooks(hooks)
            return self._send(200, json.dumps(new_hook))

        if path == "/api/hooks/update":
            from . import hooks as _hk
            hid = body.get("id", "").strip()
            if not hid:
                return self._send(400, json.dumps({"error": "missing hook id"}))
            
            hooks = _hk.get_hooks()
            hook = next((h for h in hooks if h["id"] == hid), None)
            if not hook:
                return self._send(404, json.dumps({"error": "hook not found"}))
            
            if "enabled" in body:
                hook["enabled"] = _parse_bool(body["enabled"])
            if "command" in body:
                cmd = body["command"].strip()
                if not cmd:
                    return self._send(400, json.dumps({"error": "Command cannot be empty"}))
                hook["command"] = cmd
            
            _hk.save_hooks(hooks)
            return self._send(200, json.dumps(hook))

        if path == "/api/hooks/remove":
            from . import hooks as _hk
            hid = body.get("id", "").strip()
            if not hid:
                return self._send(400, json.dumps({"error": "missing hook id"}))
            
            hooks = _hk.get_hooks()
            hooks = [h for h in hooks if h["id"] != hid]
            _hk.save_hooks(hooks)
            return self._send(200, json.dumps({"ok": True}))

        return self._send(404, json.dumps({"error": "not found"}))

    def _handle_upload(self):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self._send(400, json.dumps({"error": "expected multipart/form-data"}))
        # Reject oversized bodies BEFORE parsing. Without a ceiling, a large
        # multipart POST is read straight off the socket into memory on a
        # thread-per-request server -- a cheap memory-exhaustion DoS. Cap by the
        # declared Content-Length (default 10MB, env-tunable).
        max_bytes = int(os.environ.get("HARNESS_UPLOAD_MAX_BYTES", str(10 * 1024 * 1024)))
        try:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            content_length = 0
        if content_length <= 0:
            return self._send(400, json.dumps({"error": "missing or empty body"}))
        if content_length > max_bytes:
            return self._send(413, json.dumps({
                "error": f"upload too large: {content_length} bytes exceeds cap of {max_bytes}"
            }))
        body = self.rfile.read(content_length)
        saved = []
        for filename, data in _parse_multipart_files(body, ctype):
            ext = os.path.splitext(filename)[1].lower() or ".png"
            if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                continue
            path = os.path.join(_UPLOAD_DIR, f"{uuid.uuid4().hex}{ext}")
            with open(path, "wb") as out:
                out.write(data)
            saved.append({"path": path, "name": filename})
        return self._send(200, json.dumps({"saved": saved}))

    # GET endpoints that are intentionally public (the same-origin renderer
    # bootstrap assets, which must load BEFORE the page has the token to make
    # authenticated calls). Everything else under /api requires the token.
    _PUBLIC_GET_PATHS = frozenset({"/", "/index.html", "/app.js", "/app.css"})

    def do_GET(self):
        global _codegraph_status
        u = urlparse(self.path)
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
            from .git_provision import get_status
            return self._send(200, json.dumps(get_status()))
        if u.path == "/api/session/state":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            # resume_pending: explicit one-shot latch armed by the self-edit
            # restart path (/api/session/persist or /api/restart), AND idle.
            # NOT merely "transcript ends on a user turn" -- that heuristic
            # ghost-resumed past sessions on mere open/switch.
            _state = _pilot.state()
            return self._send(200, json.dumps({
                "state": _state,
                "pending_swarms": _pilot.has_pending_swarms(),
                "resume_pending": _consume_resume_pending(_state == "idle"),
                "runners": _runners.statuses(),
            }))
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
            from .turn_context import context_at
            record = context_at(
                _pilot.state_dir,
                getattr(_pilot, "harness_session_id", "") or "default",
                turn,
            )
            if record is None:
                return self._send(404, json.dumps({"error": f"no context recorded for turn {turn}"}))
            return self._send(200, json.dumps(record))
        if u.path == "/api/session/swarm-results":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            results = []
            for ev in _pilot.drain_swarm_results():
                results.append({"kind": ev.kind, "data": ev.data})
            if results:
                # The drain just appended history + display entries (incl. the
                # swarm outcome badge). This poll path runs while the session is
                # idle, so persist now -- otherwise closing the app before the
                # next turn would drop them.
                _checkpoint_transcript()
            return self._send(200, json.dumps({"results": results}))
        if u.path == "/api/session/queue":
            # PROMPT QUEUE snapshot -- the sequential "playlist" of full user
            # prompts that will each run as their own complete turn after the
            # current one finishes. Distinct from /api/session/steer (which is
            # a mid-turn interrupt on the CURRENT running turn).
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            try:
                items = _pilot.list_prompts() if _pilot else []
            except Exception:
                items = []
            return self._send(200, json.dumps({"items": items}))
        if u.path == "/api/checkpoints":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            repo = _cfg.repo
            if not repo or not os.path.exists(repo):
                return self._send(200, json.dumps([]))
            from .checkpoints import CheckpointStore
            active_sid = _sessions.active or ""
            store = CheckpointStore(repo, session_id=active_sid or None)
            return self._send(200, json.dumps(store.list(session_id=active_sid or None)))
        if u.path == "/api/checkpoints/diff":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            repo = _cfg.repo
            if not repo or not os.path.exists(repo):
                return self._send(400, json.dumps({"error": "No open workspace"}))
            checkpoint_id = parse_qs(u.query).get("id", [""])[0].strip()
            if not checkpoint_id:
                return self._send(400, json.dumps({"error": "Missing checkpoint id"}))
            from .checkpoints import CheckpointStore
            active_sid = _sessions.active or ""
            store = CheckpointStore(repo, session_id=active_sid or None)
            result = store.diff(
                checkpoint_id,
                session_id=active_sid or None,
                expected_repo=repo,
            )
            if result.get("ok"):
                return self._send(200, json.dumps(result))
            else:
                return self._send(400, json.dumps({"error": result.get("error", "Diff generation failed")}))
        if u.path == "/api/mcp":
            return self._send(200, json.dumps({"servers": _mcp.status(),
                "tools": [{"server": t.server, "name": t.name, "qualified": t.qualified,
                           "description": t.description} for t in _mcp.tools()]}))
        if u.path == "/api/mcp/catalog":
            return self._send(200, json.dumps({"catalog": CATALOG}))
        if u.path == "/api/commands":
            qargs = parse_qs(u.query)
            repo = qargs.get("repo", [""])[0].strip() or _cfg.repo
            cmds = _commands.list(repo=repo)
            return self._send(200, json.dumps({
                "commands": [
                    {"name": c.name, "description": c.description, "scope": c.scope}
                    for c in cmds
                ]
            }))
        if u.path == "/api/skills":
            return self._send(200, json.dumps([
                {"slug": sk.slug, "name": sk.name, "description": sk.description,
                 "state": sk.state, "source": sk.source, "used_count": sk.used_count,
                 "body": sk.body, "supersedes": getattr(sk, "supersedes", "")}
                for sk in _skills.list()]))
        if u.path == "/api/rules":
            return self._send(200, json.dumps([
                {"slug": r.slug, "text": r.text, "scope": r.scope,
                 "state": r.state, "source": r.source}
                for r in _rules.list()]))
        if u.path == "/api/memory":
            entries = _memory.list()
            return self._send(200, json.dumps({
                "memory": [
                    {"id": e.id, "text": e.text, "category": e.category,
                     "created_at": e.created_at, "source": e.source}
                    for e in entries
                ],
                "total_chars": _memory.total_chars(),
                "limit": MEMORY_CHAR_LIMIT
            }))
        if u.path == "/api/file/read":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            repo = _cfg.repo
            if not repo or not os.path.exists(repo):
                return self._send(400, json.dumps({"error": "No open workspace"}))
            rel_path = parse_qs(u.query).get("path", [""])[0].strip()
            if not rel_path:
                return self._send(400, json.dumps({"error": "Missing path parameter"}))
            full_path = os.path.abspath(os.path.join(repo, rel_path))
            from .conversation import is_safe_path
            if not is_safe_path(full_path, repo):
                return self._send(403, json.dumps({"error": "Access denied: path escapes workspace"}))
            parts = rel_path.split(os.sep)
            if ".git" in parts or any(p.startswith(".git") for p in parts):
                return self._send(403, json.dumps({"error": "Access denied: .git files are restricted"}))
            if not os.path.isfile(full_path):
                return self._send(404, json.dumps({"error": "File not found"}))
            try:
                with open(full_path, "rb") as f:
                    chunk = f.read(1024)
                    if b"\x00" in chunk:
                        return self._send(200, json.dumps({"ok": False, "error": "Cannot read binary files", "binary": True}))
            except Exception as e:
                return self._send(500, json.dumps({"error": f"Failed to check file type: {e}"}))
            try:
                file_size = os.path.getsize(full_path)
                truncated = False
                max_bytes = 1024 * 1024
                if file_size > max_bytes:
                    truncated = True
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read(max_bytes)
                else:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                return self._send(200, json.dumps({
                    "ok": True,
                    "path": rel_path,
                    "content": content,
                    "truncated": truncated
                }))
            except Exception as e:
                return self._send(500, json.dumps({"error": f"Failed to read file: {e}"}))

        if u.path == "/api/image":
            # Serve an uploaded image back to the browser so SENT message
            # thumbnails have a durable src (the composer's blob: preview URL
            # is revoked right after send and never survives a reload). Only
            # ever serve files that live under _UPLOAD_DIR -- this must NOT
            # become an arbitrary-file-read endpoint.
            req_path = parse_qs(u.query).get("path", [""])[0]
            if not req_path:
                return self._send(400, json.dumps({"error": "Missing path parameter"}))
            upload_real = os.path.realpath(_UPLOAD_DIR)
            file_real = os.path.realpath(req_path)
            try:
                is_under_upload_dir = os.path.commonpath([upload_real, file_real]) == upload_real
            except ValueError:
                # commonpath raises on e.g. mixed drives on Windows -- treat as unsafe.
                is_under_upload_dir = False
            if not is_under_upload_dir:
                return self._send(403, json.dumps({"error": "Access denied: path outside upload directory"}))
            ext = os.path.splitext(file_real)[1].lower()
            _IMAGE_CTYPES = {
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif",
            }
            if ext not in _IMAGE_CTYPES:
                return self._send(403, json.dumps({"error": "Access denied: not an image file"}))
            if not os.path.isfile(file_real):
                return self._send(404, json.dumps({"error": "File not found"}))
            try:
                size = os.path.getsize(file_real)
                max_bytes = int(os.environ.get("HARNESS_UPLOAD_MAX_BYTES", str(10 * 1024 * 1024)))
                if size > max_bytes:
                    return self._send(413, json.dumps({"error": "Image too large"}))
                with open(file_real, "rb") as f:
                    data = f.read()
            except Exception as e:
                return self._send(500, json.dumps({"error": f"Failed to read image: {e}"}))
            return self._send(200, data, _IMAGE_CTYPES[ext])

        if u.path == "/api/workspace/files":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            repo = _cfg.repo
            if not repo or not os.path.isdir(repo):
                return self._send(200, json.dumps({"files": []}))
            files_list = []
            skip_dirs = {".git", "node_modules", ".venv", ".codegraph", "dist", "build", ".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache", ".idea", ".vscode", "venv", ".next", "coverage", ".hermes", "release", "backend-dist"}
            repo_abs = os.path.abspath(repo)
            for root, dirs, files in os.walk(repo_abs):
                dirs[:] = [d for d in dirs if d not in skip_dirs]
                for f in files:
                    full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(full_path, repo_abs)
                    if rel_path == "." or rel_path.startswith(".."):
                        continue
                    # Forward slashes: the renderer's file tree and @-mention
                    # matching expect one separator on every platform.
                    files_list.append(rel_path.replace(os.sep, "/"))
                    if len(files_list) >= 2000:
                        break
                if len(files_list) >= 2000:
                    break
            return self._send(200, json.dumps({"files": sorted(files_list)}))

        if u.path == "/api/workspace/symbols":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            
            repo = _cfg.repo
            cg_status = _get_codegraph_status(repo) if repo else "unsupported"
            
            if not repo or not os.path.isdir(repo):
                return self._send(200, json.dumps({"symbols": [], "status": cg_status}))
            
            try:
                import puppetmaster.codegraph as cg
                if not cg.codegraph_available() or not cg.codegraph_ready(repo):
                    return self._send(200, json.dumps({"symbols": [], "status": cg_status}))
            except Exception:
                return self._send(200, json.dumps({"symbols": [], "status": "unsupported"}))
            
            q = parse_qs(u.query).get("q", [""])[0].strip()
            if len(q) < 1:
                return self._send(200, json.dumps({"symbols": [], "status": "ready"}))
            
            try:
                import puppetmaster.codegraph as cg
                res = cg.codegraph_query(search=q, cwd=repo, limit=20)
                symbols_list = []
                if res.get("ok") and res.get("stdout"):
                    try:
                        data = json.loads(res["stdout"])
                        if isinstance(data, list):
                            for item in data:
                                node = item.get("node")
                                if not node:
                                    continue
                                name = node.get("name")
                                kind = node.get("kind")
                                file_path = node.get("filePath")
                                start_line = node.get("startLine")
                                if name and file_path and start_line is not None:
                                    symbols_list.append({
                                        "name": str(name),
                                        "kind": str(kind or "unknown"),
                                        "path": str(file_path),
                                        "line": int(start_line)
                                    })
                                if len(symbols_list) >= 20:
                                    break
                    except Exception:
                        pass
                return self._send(200, json.dumps({"symbols": symbols_list, "status": "ready"}))
            except Exception as e:
                return self._send(200, json.dumps({"symbols": [], "error": str(e), "status": cg_status}))
        if u.path == "/api/workspace":
            repo = _cfg.repo
            is_git = False
            branch = ""
            if repo and os.path.isdir(repo):
                import subprocess
                try:
                    proc = subprocess.run(
                        ["git", "-C", repo, "rev-parse", "--is-inside-work-tree"],
                        capture_output=True, text=True, timeout=5
                    )
                    if proc.returncode == 0:
                        is_git = True
                        proc_branch = subprocess.run(
                            ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True, timeout=5
                        )
                        if proc_branch.returncode == 0:
                            branch = proc_branch.stdout.strip()
                except Exception:
                    pass
            cg_status = _get_codegraph_status(repo) if repo else "unsupported"
            recents = []
            try:
                _ws_path = _workspace_json_path()
                if os.path.exists(_ws_path):
                    with open(_ws_path, encoding="utf-8", errors="replace") as f:
                        recents = json.load(f).get("recents", []) or []
            except Exception:
                recents = []
            # filter temp/dead dirs so ephemeral test state_dirs never show as recents
            _tmproot = os.path.realpath(_tf.gettempdir())
            recents = [
                r for r in recents
                if r and os.path.isdir(r)
                and not os.path.realpath(r).startswith(_tmproot)
                and "/var/folders/" not in os.path.realpath(r)
                and not _is_app_install_root(r)
            ]
            return self._send(200, json.dumps({
                "repo": repo,
                "branch": branch,
                "is_git": is_git,
                "codegraph_status": cg_status,
                "recents": recents
            }))
        if u.path == "/api/models/catalog":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from . import model_visibility as _mv
            return self._send(200, json.dumps({
                "catalog": _mv.catalog(available_only=True),
                "all": _mv.catalog(available_only=False),
                "enabled": _mv.get_enabled(),
            }))
        if u.path == "/api/codegraph":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))

            repo = _cfg.repo
            if not repo or not os.path.isdir(repo):
                return self._send(200, json.dumps({
                    "indexed": False,
                    "status": "none",
                    "nodes": None,
                    "edges": None,
                    "files": None,
                    "languages": None,
                    "last_indexed": None,
                    "repo": ""
                }))

            if not _puppetmaster_available():
                return self._send(200, json.dumps({
                    "indexed": False,
                    "status": "unsupported",
                    "reason": _codegraph_status_reason or "puppetmaster not found -- codegraph/swarm unavailable",
                    "nodes": None,
                    "edges": None,
                    "files": None,
                    "languages": None,
                    "last_indexed": None,
                    "repo": repo
                }))

            # Only report "indexing" while the indexer subprocess is actually
            # alive. If the flag is stale (job finished), fall through to the real
            # status query so the panel shows live metrics instead of nulls --
            # this is what previously stuck the panel on INDEXING until a restart.
            if _codegraph_status == "indexing" and not _codegraph_index_alive():
                _codegraph_status = "ready" if _codegraph_indexed(repo) else "unsupported"
                _codegraph_status_cache.pop(repo, None)

            if _codegraph_status == "indexing" and _codegraph_index_alive():
                last_indexed = None
                try:
                    import puppetmaster.codegraph as cg
                    mtime = cg.codegraph_index_mtime(repo)
                    if mtime:
                        import datetime
                        last_indexed = datetime.datetime.fromtimestamp(mtime).isoformat()
                except Exception:
                    try:
                        db_path = os.path.join(repo, ".codegraph", "db")
                        if not os.path.exists(db_path):
                            db_path = os.path.join(repo, ".codegraph")
                        if os.path.exists(db_path):
                            mtime = os.path.getmtime(db_path)
                            import datetime
                            last_indexed = datetime.datetime.fromtimestamp(mtime).isoformat()
                    except Exception:
                        pass

                return self._send(200, json.dumps({
                    "indexed": False,
                    "status": "indexing",
                    "reason": _codegraph_status_reason or None,
                    "nodes": None,
                    "edges": None,
                    "files": None,
                    "languages": None,
                    "last_indexed": last_indexed,
                    "repo": repo
                }))

            # No built DB yet and no indexer running: start one and report
            # "indexing" rather than shelling out to `codegraph status --json`,
            # which hangs on a config-only checkout until the 20s timeout and then
            # mis-reports "unsupported". This makes a fresh install self-heal.
            if not _codegraph_indexed(repo) and not _codegraph_index_alive():
                def _kick_index():
                    _index_codegraph_bg(repo)
                threading.Thread(target=_kick_index, daemon=True).start()
                return self._send(200, json.dumps({
                    "indexed": False,
                    "status": "indexing",
                    "reason": None,
                    "nodes": None,
                    "edges": None,
                    "files": None,
                    "languages": None,
                    "last_indexed": None,
                    "repo": repo,
                }))

            # Serve a recent cached payload instead of re-spawning the status
            # subprocess on every poll (the main source of panel load lag).
            import time as _time
            cached = _codegraph_status_cache.get(repo)
            if cached and cached[0] > _time.monotonic():
                return self._send(200, json.dumps(cached[1]))

            try:
                import subprocess
                # 20s (not 5s): codegraph status on a large indexed repo
                # (e.g. 60k+ nodes) takes ~5s in the packaged/frozen binary --
                # right at a 5s limit, which intermittently tripped a timeout
                # and showed "UNSUPPORTED" in the panel even though the repo is
                # fully indexed. The 30s status cache means this slower call is
                # only paid on a cache miss, so the panel stays responsive.
                proc = subprocess.run(
                    _puppetmaster_cmd("codegraph", "status", "--json"),
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    timeout=20
                )
                if proc.returncode == 0:
                    data = json.loads(proc.stdout)
                    initialized = data.get("initialized", False)
                    status_val = "ready" if initialized else "unsupported"
                    
                    last_indexed = None
                    try:
                        import puppetmaster.codegraph as cg
                        mtime = cg.codegraph_index_mtime(repo)
                        if mtime:
                            import datetime
                            last_indexed = datetime.datetime.fromtimestamp(mtime).isoformat()
                    except Exception:
                        try:
                            db_path = os.path.join(repo, ".codegraph", "db")
                            if not os.path.exists(db_path):
                                db_path = os.path.join(repo, ".codegraph")
                            if os.path.exists(db_path):
                                mtime = os.path.getmtime(db_path)
                                import datetime
                                last_indexed = datetime.datetime.fromtimestamp(mtime).isoformat()
                        except Exception:
                            pass

                    _cg_payload = {
                        "indexed": initialized,
                        "status": status_val,
                        "nodes": data.get("nodeCount"),
                        "edges": data.get("edgeCount"),
                        "files": data.get("fileCount"),
                        "languages": data.get("languages"),
                        "last_indexed": last_indexed,
                        "repo": repo
                    }
                    _codegraph_status_cache[repo] = (
                        _time.monotonic() + _CODEGRAPH_STATUS_TTL, _cg_payload)
                    return self._send(200, json.dumps(_cg_payload))
                else:
                    return self._send(200, json.dumps({
                        "indexed": False,
                        "status": "unsupported",
                        "nodes": None,
                        "edges": None,
                        "files": None,
                        "languages": None,
                        "last_indexed": None,
                        "repo": repo
                    }))
            except Exception:
                return self._send(200, json.dumps({
                    "indexed": False,
                    "status": "unsupported",
                    "nodes": None,
                    "edges": None,
                    "files": None,
                    "languages": None,
                    "last_indexed": None,
                    "repo": repo
                }))
        if u.path == "/api/config":
            try:
                from .edit_engines import agentic_available, select_edit_engine
                _edit_engine = select_edit_engine(_cfg)
                _agentic_ready = agentic_available()
            except Exception:
                _edit_engine, _agentic_ready = "native", False
            return self._send(200, json.dumps({
                "driver": _cfg.driver, "reach": _cfg.reach,
                "budget": _cfg.budget, "state_dir": _session.state_dir,
                "models": _available_pilots(), "repo": _cfg.repo,
                "swarm_adapter": _cfg.swarm_adapter,
                "edit_engine": _edit_engine, "agentic_ready": _agentic_ready,
                "preflight": _session.preflight()}))
        if u.path == "/api/wiki/config":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            return self._send(200, json.dumps(get_wiki_config()))
        if u.path == "/api/wiki/graph":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            
            # WikiClient auto-detects the gated owner surface (WIKI_API_BASE +
            # WIKI_OWNER_TOKEN, same as the portable-llm-wiki MCP) or the public
            # HARNESS_WIKI_URL. config.wiki_url overrides base_url when set.
            from .wiki import WikiClient
            try:
                client = WikiClient(base_url=_cfg.wiki_url or "", timeout=8)
            except Exception as e:
                client = None
                _client_err = str(e)
            if client is None or not client.base_url:
                return self._send(200, json.dumps({
                    "configured": False,
                    "status": "not_configured",
                    "nodes": [],
                    "edges": [],
                    "base_url": ""
                }))
            import time as _time
            _wiki_cached = _wiki_graph_cache.get(client.base_url)
            if _wiki_cached and _wiki_cached[0] > _time.monotonic():
                return self._send(200, json.dumps(_wiki_cached[1]))
            try:
                res = client.graph()
            except Exception as e:
                res = {"error": f"Unexpected error: {str(e)}", "nodes": [], "edges": []}
            if res.get("error"):
                # Distinguish "wiki host unreachable / not actually set up" from a real
                # API error. An unreachable host (connection refused, DNS failure, timeout)
                # should look like NOT CONNECTED -- neutral -- not a scary red ERROR, so a
                # user who never set up a wiki is not confused by a broken-looking panel.
                _err_l = str(res.get("error", "")).lower()
                _unreachable = any(t in _err_l for t in (
                    "connection refused", "refused", "timed out", "timeout",
                    "name or service not known", "nodename nor servname",
                    "failed to establish", "max retries", "cannot connect",
                    "connection error", "urlopen error", "getaddrinfo",
                    "no route to host", "network is unreachable", "[errno",
                ))
                # If the wiki was NEVER configured (no base_url/token), an
                # unreachable result is just "not set up" -> neutral. But if a
                # base_url IS configured, a transient failure must NOT wipe the
                # connection -- keep configured + base_url and report a retryable
                # error so Refresh recovers instead of showing "not connected".
                _is_configured = bool(client.base_url)
                if _unreachable and not _is_configured:
                    return self._send(200, json.dumps({
                        "configured": False,
                        "status": "not_configured",
                        "nodes": [],
                        "edges": [],
                        "base_url": ""
                    }))
                return self._send(200, json.dumps({
                    "configured": True,
                    "status": "error",
                    "nodes": [],
                    "edges": [],
                    "error": ("Wiki temporarily unreachable -- click Refresh to retry."
                              if _unreachable else res["error"]),
                    "retryable": True,
                    "base_url": client.base_url
                }))
            _wiki_payload = {
                "configured": True,
                "status": "ok",
                "nodes": res.get("nodes") or [],
                "edges": res.get("edges") or [],
                "base_url": client.base_url
            }
            _wiki_graph_cache[client.base_url] = (
                _time.monotonic() + _WIKI_GRAPH_TTL, _wiki_payload)
            return self._send(200, json.dumps(_wiki_payload))
        if u.path == "/api/wiki/status":
            # Lightweight summary for the State pane strip -- counts only, no
            # full node/edge arrays. Reuses the same graph cache as /api/wiki/graph.
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .wiki import WikiClient
            try:
                client = WikiClient(base_url=_cfg.wiki_url or "", timeout=8)
            except Exception:
                client = None
            if client is None or not client.base_url:
                return self._send(200, json.dumps({
                    "configured": False,
                    "status": "not_configured",
                    "page_count": 0,
                    "link_count": 0,
                    "base_url": ""
                }))
            import time as _time
            _wiki_cached = _wiki_graph_cache.get(client.base_url)
            if _wiki_cached and _wiki_cached[0] > _time.monotonic():
                cached = _wiki_cached[1]
                return self._send(200, json.dumps({
                    "configured": cached.get("configured", True),
                    "status": cached.get("status", "ok"),
                    "page_count": len(cached.get("nodes") or []),
                    "link_count": len(cached.get("edges") or []),
                    "error": cached.get("error"),
                    "retryable": cached.get("retryable"),
                    "base_url": cached.get("base_url") or client.base_url,
                }))
            try:
                res = client.graph()
            except Exception as e:
                res = {"error": f"Unexpected error: {str(e)}", "nodes": [], "edges": []}
            if res.get("error"):
                _err_l = str(res.get("error", "")).lower()
                _unreachable = any(t in _err_l for t in (
                    "connection refused", "refused", "timed out", "timeout",
                    "name or service not known", "nodename nor servname",
                    "failed to establish", "max retries", "cannot connect",
                    "connection error", "urlopen error", "getaddrinfo",
                    "no route to host", "network is unreachable", "[errno",
                ))
                _is_configured = bool(client.base_url)
                if _unreachable and not _is_configured:
                    return self._send(200, json.dumps({
                        "configured": False,
                        "status": "not_configured",
                        "page_count": 0,
                        "link_count": 0,
                        "base_url": ""
                    }))
                return self._send(200, json.dumps({
                    "configured": True,
                    "status": "error",
                    "page_count": 0,
                    "link_count": 0,
                    "error": ("Wiki temporarily unreachable -- click Refresh to retry."
                              if _unreachable else res["error"]),
                    "retryable": True,
                    "base_url": client.base_url
                }))
            nodes = res.get("nodes") or []
            edges = res.get("edges") or []
            _wiki_payload = {
                "configured": True,
                "status": "ok",
                "nodes": nodes,
                "edges": edges,
                "base_url": client.base_url
            }
            _wiki_graph_cache[client.base_url] = (
                _time.monotonic() + _WIKI_GRAPH_TTL, _wiki_payload)
            return self._send(200, json.dumps({
                "configured": True,
                "status": "ok",
                "page_count": len(nodes),
                "link_count": len(edges),
                "base_url": client.base_url
            }))
        if u.path == "/api/settings":
            return self._send(200, json.dumps(_get_settings_dict()))
        if u.path == "/api/reviews":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            with _pilot._pending_reviews_lock:
                reviews_list = list(_pilot._pending_reviews.values())
            return self._send(200, json.dumps(reviews_list))
        if u.path == "/api/platform":
            return self._send(200, json.dumps(_get_platform_adapters()))
        if u.path == "/api/jobs":
            q = parse_qs(u.query)
            repo_override = q.get("repo", [""])[0]
            return self._send(200, json.dumps(_scoped_jobs_snapshot(repo_root=repo_override or None)))
        if u.path == "/api/usage":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            # Resolve real per-Mtok pricing for the active driver: eval-catalog
            # native rates first, then the live OpenRouter price map (so picker
            # specs like 'anthropic:claude-opus-4-8' show the true $5/$25 instead
            # of a 0.5/2.0 placeholder).
            try:
                from pmharness.registry import resolve_price
                price_in, price_out = resolve_price(_cfg.driver)
            except Exception:
                price_in, price_out = 0.5, 2.0
            # Boot pill: process-lifetime meters (carry + ALL live runners), not
            # just the active view -- so attaching another session never drops
            # spend that already happened on a background runner.
            _boot_meters = _boot_usage_meters()
            tokens_used = int(_boot_meters.get("_tokens_used", 0) or 0)
            _t_in = int(_boot_meters.get("_tokens_in", 0) or 0)
            _t_out = int(_boot_meters.get("_tokens_out", 0) or 0)
            _t_cached = int(_boot_meters.get("_tokens_cached", 0) or 0)
            _w_in = int(_boot_meters.get("_worker_tokens_in", 0) or 0)
            _w_out = int(_boot_meters.get("_worker_tokens_out", 0) or 0)
            # Price each live runner (and carry) via _session_cost_split so
            # worker dollars stay at each worker's own model rate.
            est_session_cost = _boot_session_cost(price_in, price_out)
            jobs_list = []
            session_total = None
            routing_saved_usd = 0.0
            cache_saved_usd_swarm = 0.0
            swarm_cached = 0
            try:
                # Same merged, workspace-scoped job set the tracker uses
                # (/api/swarm/live): harness store + per-project CLI store, so
                # MCP/CLI-dispatched swarm spend reaches the status bar.
                from .cli_job_merge import (
                    bulk_load_store_artifacts,
                    partition_jobs_by_store,
                )

                repo_override = parse_qs(u.query).get("repo", [""])[0]
                # Boot-pill swarm dollars: merge epoch-windowed jobs across every
                # workspace opened this process (not only active _cfg.repo).
                # session_total below still uses the active-workspace set.
                boot_repos = set(_BOOT_REPOS)
                active_repo = (repo_override or "").strip() or (_cfg.repo or "")
                if active_repo:
                    boot_repos.add(os.path.abspath(active_repo) if os.path.isdir(active_repo) else active_repo)
                if not boot_repos and active_repo:
                    boot_repos.add(active_repo)

                all_jobs_by_id: dict = {}
                store = None
                cli_store = None
                for repo_path in sorted(boot_repos) or [active_repo or None]:
                    scoped, st, cli_st = _scoped_jobs_with_stores(
                        repo_root=repo_path or None
                    )
                    if store is None:
                        store = st
                    if cli_store is None and cli_st is not None:
                        cli_store = cli_st
                    for j in scoped:
                        jid = j.get("id")
                        if jid and jid not in all_jobs_by_id:
                            all_jobs_by_id[jid] = j
                all_jobs = list(all_jobs_by_id.values())

                # Active-workspace set for session_total (unchanged semantics).
                active_jobs, active_store, active_cli = _scoped_jobs_with_stores(
                    repo_root=repo_override or None
                )
                if store is None:
                    store = active_store
                if cli_store is None:
                    cli_store = active_cli

                # Boot pill: only jobs created during THIS app run (epoch window).
                jobs = [j for j in all_jobs
                        if _job_in_cost_window(j.get("created_at"))]
                registry = _swarm_registry()
                jids = [j.get("id") for j in jobs if j.get("id")]
                # Artifacts may live in harness or CLI stores; load from the
                # union of boot-scoped + active-scoped job ids.
                arts_source_jobs = list(all_jobs_by_id.values())
                for j in active_jobs:
                    jid = j.get("id")
                    if jid and jid not in all_jobs_by_id:
                        arts_source_jobs.append(j)
                harness_jids, cli_jids = partition_jobs_by_store(arts_source_jobs)

                arts_by_job: dict = {}
                try:
                    harness_arts = bulk_load_store_artifacts(store, harness_jids)
                    cli_arts = bulk_load_store_artifacts(cli_store, cli_jids)
                    arts_by_job = {**harness_arts, **cli_arts}
                except Exception:
                    arts_by_job = None  # fall back to per-job reads

                # Owning-store lookup: each job is priced from its own store.
                job_by_id = {j.get("id"): j for j in arts_source_jobs if j.get("id")}

                def _owning_store(jid):
                    job = job_by_id.get(jid) or {}
                    if job.get("source") == "cli" and cli_store is not None:
                        return cli_store
                    return store

                def _job_arts(jid):
                    if arts_by_job is not None:
                        return arts_by_job.get(jid, [])
                    owning = _owning_store(jid)
                    if owning is None:
                        return []
                    try:
                        return _retry_on_locked(lambda: owning.list_artifacts(jid))
                    except Exception:
                        return []

                # Lifetime session_total: active-workspace visible set only
                # (filter_store_jobs + CLI merge). Dedupe by job id; harness
                # wins via merge_scoped_cli_jobs order.
                session_jids: list = []
                seen_session: set = set()
                for j in active_jobs:
                    jid = j.get("id")
                    if not jid or jid in seen_session:
                        continue
                    seen_session.add(jid)
                    session_jids.append(jid)

                for jid in jids:
                    try:
                        raw_arts = _job_arts(jid)
                        tokens, est_cost_usd = _job_swarm_accounting(
                            raw_arts, registry
                        )
                        try:
                            swarm_cached += int(_tokens_cached_swarm(raw_arts) or 0)
                        except Exception:
                            pass
                        jobs_list.append({
                            "job_id": jid,
                            "tokens": tokens,
                            "est_cost_usd": est_cost_usd,
                            **_job_savings_fields(jid),
                        })
                    except Exception as e:
                        _diag("server.usage_job_cost", e, msg=f"job={jid}")
                session_total = _active_session_total(
                    session_jids, _job_arts, registry
                )
                # Boot-pill savings: epoch job set across boot repos (jids), not
                # active-workspace-only session_jids -- so dir/session swaps keep
                # routing/cache saved meters process-lifetime.
                routing_saved_usd, cache_saved_usd_swarm = _sum_job_set_savings(
                    jids, _job_arts, registry
                )
            except Exception as e:
                _diag("server.usage_jobs_aggregate", e)
            # Swarm store jobs: dollars come ONLY from here (usage artifacts x
            # registry). Token display = pilot-only meters (boot total minus
            # worker in/out already folded into pilot) + authoritative store
            # job token sums -- mirrors SwarmPane job.tokens without undercount.
            swarm_cost = sum(float(j.get("est_cost_usd") or 0.0) for j in jobs_list)
            est_session_cost += swarm_cost
            pilot_only_tokens = max(0, tokens_used - _w_in - _w_out)
            job_tokens_sum = sum(int(j.get("tokens") or 0) for j in jobs_list)
            tokens_used = pilot_only_tokens + job_tokens_sum
            # Cache tokens: subtract overlapping swarm attribution from pilot
            # meters, then add authoritative store-job cache (avoids double
            # count when harness workers were folded into _tokens_cached).
            pilot_only_cached = max(0, _t_cached - min(_t_cached, swarm_cached))
            tokens_cached = pilot_only_cached + swarm_cached
            _cache_savings_usd = _cache_savings(pilot_only_cached, price_in)
            response_data = {
                "session": {
                    "tokens_used": tokens_used,
                    "est_cost_usd": round(est_session_cost, 6),
                    "driver": _cfg.driver,
                    "price_in": price_in,
                    "price_out": price_out,
                    # Prompt-cache hits (billed at the cache-read discount) and
                    # the USD that discount saved vs full input price (pilot-
                    # only; store-job cache USD is cache_saved_usd_swarm).
                    "tokens_cached": tokens_cached,
                    "cache_savings_usd": round(_cache_savings_usd, 6),
                    # Routing + swarm-cache savings over the boot-repo epoch job
                    # set (additive to the pilot cache/compaction figures).
                    "routing_saved_usd": round(routing_saved_usd, 6),
                    "cache_saved_usd_swarm": round(cache_saved_usd_swarm, 6),
                    **_tool_output_savings_fields(price_in, process_wide=True),
                },
                # Lifetime running total for the active chat session
                # (persisted meters + all-time session-stamped / workspace-
                # visible swarm jobs); unlike "session" above, it survives
                # restarts and updates.
                "session_total": session_total,
                "jobs": jobs_list
            }
            try:
                _persist_boot_usage(fold_live=False)
            except Exception:
                pass
            return self._send(200, json.dumps(response_data))
        if u.path == "/api/artifacts":
            q = parse_qs(u.query)
            jid = q.get("job_id", [""])[0]
            # Resolve harness store first, then the per-project CLI store -- same
            # dual-store contract as cancel/live (CLI jobs 404 otherwise).
            artifacts = []
            state_obj = None
            try:
                state_obj = _session.state()
                artifacts = _retry_on_locked(lambda: state_obj.job_artifacts(jid))
            except Exception:
                artifacts = []
            if not artifacts:
                try:
                    from .cli_job_merge import open_cli_durable_state
                    cli_state = open_cli_durable_state(_cfg.repo or "")
                    if cli_state is not None and hasattr(cli_state, "job_artifacts"):
                        artifacts = _retry_on_locked(lambda: cli_state.job_artifacts(jid))
                    elif cli_state is not None and hasattr(cli_state, "store"):
                        raw = _retry_on_locked(lambda: cli_state.store.list_artifacts(jid))
                        fmt = state_obj or _session.state()
                        if hasattr(fmt, "format_artifacts"):
                            artifacts = fmt.format_artifacts(raw)
                except Exception:
                    pass
            return self._send(200, json.dumps(artifacts))
        if u.path == "/api/swarm/live":
            if self._guard():
                return
            q = parse_qs(u.query)
            qtok = q.get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            repo_override = q.get("repo", [""])[0]
            scoped_repo = (repo_override or "").strip() or (_cfg.repo or "")
            res_jobs = []
            try:
                from pmharness.registry import resolve_price
                price_in, price_out = resolve_price(_cfg.driver)
            except Exception:
                price_in, price_out = 0.5, 2.0
            try:
                from .cli_job_merge import (
                    bulk_load_store_artifacts,
                    bulk_load_store_tasks,
                    partition_jobs_by_store,
                )
                from .job_scoping import filter_local_jobs, resolve_job_model

                state_obj = _session.state()
                registry = _swarm_registry()
                jobs, store, cli_store = _scoped_jobs_with_stores(repo_root=repo_override or None)

                harness_jids, cli_jids = partition_jobs_by_store(jobs)
                jids = [j.get("id") for j in jobs if j.get("id")]
                # Batch all three per-job reads (the old N+1 read artifacts TWICE
                # plus tasks, per job): one bulk artifacts read + one bulk tasks
                # read, regrouped by job_id.
                arts_by_job: dict = {}
                tasks_by_job: dict = {}
                try:
                    harness_arts = bulk_load_store_artifacts(store, harness_jids)
                    cli_arts = bulk_load_store_artifacts(cli_store, cli_jids)
                    arts_by_job = {**harness_arts, **cli_arts}
                except Exception:
                    arts_by_job = None
                try:
                    harness_tasks = bulk_load_store_tasks(store, harness_jids)
                    cli_tasks = bulk_load_store_tasks(cli_store, cli_jids)
                    tasks_by_job = {**harness_tasks, **cli_tasks}
                except Exception:
                    tasks_by_job = None

                for j in jobs:
                    jid = j.get("id")
                    if not jid:
                        continue

                    job_store = cli_store if j.get("source") == "cli" and cli_store else store
                    raw_arts = (arts_by_job.get(jid, []) if arts_by_job is not None
                                else _retry_on_locked(lambda: job_store.list_artifacts(jid)))
                    # Terminal jobs: slim artifact list (routing + verdicts only).
                    # Running jobs keep the full stream so findings appear live.
                    # Expand fetches /api/artifacts for the rest.
                    job_status = j.get("status", "")
                    terminal = _job_status_is_terminal(str(job_status))
                    try:
                        if terminal:
                            artifacts_list = _slim_swarm_list_artifacts(raw_arts, state_obj)
                            artifacts_complete = False
                        elif hasattr(state_obj, "format_artifacts"):
                            artifacts_list = state_obj.format_artifacts(raw_arts)
                            artifacts_complete = True
                        else:
                            artifacts_list = state_obj.job_artifacts(jid)
                            artifacts_complete = True
                    except Exception:
                        artifacts_list = []
                        artifacts_complete = not terminal

                    tokens, est_cost_usd = _job_swarm_accounting(raw_arts, registry)
                    # Per-task meters from raw artifacts (before slim) so worker
                    # rows keep tokens/cost even when the artifact list is slimmed.
                    try:
                        task_accounting = _task_swarm_accounting(raw_arts, registry)
                    except Exception:
                        task_accounting = {}
                    # Per-job savings from raw artifacts (before slim). Terminal
                    # rows still get these meters even when the artifact list is
                    # slimmed -- expand must not be required to see savings.
                    try:
                        job_routing_saved = round(_routing_saved_usd(raw_arts), 6)
                    except Exception:
                        job_routing_saved = 0.0
                    try:
                        job_cache_saved = round(
                            _cache_saved_usd_swarm(raw_arts, registry), 6
                        )
                    except Exception:
                        job_cache_saved = 0.0
                    try:
                        job_tokens_cached = int(_tokens_cached_swarm(raw_arts) or 0)
                    except Exception:
                        job_tokens_cached = 0
                    job_model = resolve_job_model(
                        raw_arts,
                        (tasks_by_job.get(jid, []) if tasks_by_job is not None else []),
                        j.get("adapter", ""),
                    )
                    dead_run = _job_dead_run_failure(raw_arts, str(job_status))

                    tasks_list = []
                    try:
                        raw_tasks = (tasks_by_job.get(jid, []) if tasks_by_job is not None
                                     else _retry_on_locked(lambda: job_store.list_tasks(jid)))
                        for t in raw_tasks:
                            # Finished cards only need role/status/adapter for the
                            # worker strip; skip long instructions until expand.
                            instr = "" if terminal else (getattr(t, "instruction", "") or "")
                            tid = getattr(t, "id", "") or ""
                            entry = {
                                "id": tid,
                                "role": getattr(t, "role", ""),
                                "instruction": instr,
                                "status": str(getattr(t, "status", "")),
                                "adapter": getattr(t, "adapter", ""),
                                "completed_at": getattr(t, "completed_at", None),
                            }
                            acct = task_accounting.get(tid) if tid else None
                            if acct:
                                t_tokens = int(acct.get("tokens") or 0)
                                t_cost = float(acct.get("est_cost_usd") or 0.0)
                                if t_tokens > 0:
                                    entry["tokens"] = t_tokens
                                if t_cost > 0:
                                    entry["est_cost_usd"] = round(t_cost, 6)
                            tasks_list.append(entry)
                    except Exception:
                        pass

                    row = {
                        "id": jid,
                        "goal": j.get("goal", ""),
                        "status": job_status,
                        "role": j.get("role", ""),
                        "adapter": j.get("adapter", ""),
                        "model": job_model,
                        "created_at": j.get("created_at"),
                        "task_count": j.get("task_count", 0),
                        "tokens": tokens,
                        "est_cost_usd": est_cost_usd,
                        "tokens_cached": job_tokens_cached,
                        "routing_saved_usd": job_routing_saved,
                        "cache_saved_usd": job_cache_saved,
                        "artifacts": artifacts_list,
                        "artifacts_complete": artifacts_complete,
                        "tasks": tasks_list,
                        "source": j.get("source", "harness"),
                        **_job_savings_fields(jid),
                    }
                    if dead_run:
                        row["dead_run_failure"] = dead_run
                    res_jobs.append(row)
            except Exception as e:
                _diag("server.jobs_list_aggregate", e)

            # Merge in-process provider-native worker jobs (job_id "local-*").
            # These run on the user's own key rather than a Puppetmaster adapter,
            # so they never enter the durable store above -- without this the panel
            # reads "No swarm jobs yet" while a worker is visibly running.
            try:
                existing_ids = {j.get("id") for j in res_jobs}
                scoped_locals = filter_local_jobs(
                    _pilot.live_local_jobs(),
                    active_session_id=_sessions.active or getattr(_pilot, "harness_session_id", "") or "",
                    repo_root=scoped_repo,
                )
                for lj in scoped_locals:
                    if lj.get("id") not in existing_ids:
                        # Local jobs already carry their (tiny) artifact list
                        # inline; mark complete so the UI never lazy-fetches.
                        if "artifacts_complete" not in lj:
                            lj = {**lj, "artifacts_complete": True}
                        res_jobs.append(lj)
            except Exception as e:
                _diag("server.jobs_list_merge_local", e)
            
            tokens_used = int(getattr(_pilot, "_tokens_used", 0) or 0)
            # Accurate split: input tokens at price_in, output at price_out, with
            # cached prompt tokens re-billed at the cache-read discount. Falls
            # back to a single-rate estimate if the in/out split isn't tracked.
            _t_in = getattr(_pilot, "_tokens_in", 0)
            _t_out = getattr(_pilot, "_tokens_out", 0)
            _t_cached = int(getattr(_pilot, "_tokens_cached", 0) or 0)
            _w_in = int(getattr(_pilot, "_worker_tokens_in", 0) or 0)
            _w_out = int(getattr(_pilot, "_worker_tokens_out", 0) or 0)
            est_session_cost = _session_cost_split(_pilot, price_in, price_out)
            # Add swarm store-job spend from the scoped job list only.
            try:
                est_session_cost += sum(
                    float(j.get("est_cost_usd") or 0.0)
                    for j in res_jobs
                    if not str(j.get("id") or "").startswith("local-")
                )
            except Exception:
                pass

            # Mid-run savings: sum per-job routing/cache meters so the live
            # session block matches /api/usage (pilot cache stays separate).
            live_routing_saved = 0.0
            live_cache_saved = 0.0
            swarm_cached = 0
            job_tokens_sum = 0
            try:
                for j in res_jobs:
                    if str(j.get("id") or "").startswith("local-"):
                        continue
                    live_routing_saved += float(j.get("routing_saved_usd") or 0.0)
                    live_cache_saved += float(j.get("cache_saved_usd") or 0.0)
                    swarm_cached += int(j.get("tokens_cached") or 0)
                    job_tokens_sum += int(j.get("tokens") or 0)
            except Exception:
                pass

            # Same token parity as /api/usage: pilot-only + store job tokens.
            tokens_used = max(0, tokens_used - _w_in - _w_out) + job_tokens_sum
            pilot_only_cached = max(0, _t_cached - min(_t_cached, swarm_cached))
            tokens_cached = pilot_only_cached + swarm_cached
            _cache_savings_usd = _cache_savings(pilot_only_cached, price_in)

            response_data = {
                "session": {
                    "tokens_used": tokens_used,
                    "est_cost_usd": round(est_session_cost, 6),
                    "driver": _cfg.driver,
                    # Prompt-cache hits (billed at the cache-read discount) so the
                    # UI can show how much input was served near-free -- proof the
                    # harness is not token-hungry -- plus the USD it saved.
                    "tokens_cached": tokens_cached,
                    "cache_savings_usd": round(_cache_savings_usd, 6),
                    "routing_saved_usd": round(live_routing_saved, 6),
                    "cache_saved_usd_swarm": round(live_cache_saved, 6),
                    **_tool_output_savings_fields(price_in),
                },
                "jobs": res_jobs
            }
            return self._send(200, json.dumps(response_data))
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
            from .registry_wizard import PROVIDERS, get_provider_key
            from .keys import provider_has_env, get_disconnected
            disconnected = get_disconnected()
            res = []
            for p in PROVIDERS:
                status = get_api_key_status(p.name)
                res.append({
                    "name": p.name,
                    "display_name": getattr(p, "display_name", "") or p.name,
                    "env_var": p.env_vars[0] if p.env_vars else "",
                    "base_url": p.base_url,
                    "has_key": (get_provider_key(p) is not None) or status["has_key"],
                    "masked": status["masked"],
                    "api_mode": p.api_mode,
                    "has_env": provider_has_env(p.name),
                    "disconnected": p.name in disconnected,
                })
            return self._send(200, json.dumps(res))

        if u.path == "/api/registry":
            from .registry_wizard import get_models_file_path
            path = get_models_file_path()
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        return self._send(200, f.read())
                except Exception as e:
                    return self._send(500, json.dumps({"error": f"Failed to read registry: {str(e)}"}))
            return self._send(200, json.dumps({"models": []}))

        if u.path == "/api/roles":
            from .registry_wizard import REAL_BASE_SCORES, get_routing_file_path
            path = get_routing_file_path()
            overrides = {}
            policy = "balanced"
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        data = json.load(f)
                        overrides = data.get("overrides", {})
                        policy = data.get("routing_policy", "balanced")
                except Exception as e:
                    _diag("server.roles_routing_load", e)
            
            roles_mapping = {}
            for k, v in REAL_BASE_SCORES.items():
                roles_mapping[k] = overrides.get(k, v)
                
            return self._send(200, json.dumps({
                "roles": roles_mapping,
                "policies": ["balanced", "cheap", "quality", "escalating"],
                "routing_policy": policy,
                "overrides": overrides
            }))

        if u.path == "/api/registry/recommend":
            from .registry_wizard import get_recommendations
            try:
                rec = get_recommendations()
                return self._send(200, json.dumps(rec))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
        if u.path == "/api/run":
            q = parse_qs(u.query)
            imgs = []
            upload_dir_real = os.path.realpath(_UPLOAD_DIR)
            for p in q.get("images", [""])[0].split("|"):
                if not p:
                    continue
                real_p = os.path.realpath(p)
                try:
                    if os.path.commonpath([upload_dir_real, real_p]) == upload_dir_real:
                        imgs.append(p)
                    else:
                        return self._send(400, json.dumps({"error": f"Invalid image path: {p}"}))
                except ValueError:
                    return self._send(400, json.dumps({"error": f"Invalid image path: {p}"}))
            return self._stream_run(q.get("prompt", [""])[0], imgs)
        if u.path == "/api/chat":
            q = parse_qs(u.query)
            # A stashed message (see POST /api/chat/stash) takes precedence: it
            # exists precisely because the real message/images were too big for
            # this URL. Falls back to the query-param message for small chats,
            # keeping today's behavior unchanged when no ?mid= is present.
            mid = q.get("mid", [""])[0]
            raw_images = q.get("images", [""])[0]
            message = q.get("message", [""])[0]
            if mid:
                stashed = _stash_pop(mid)
                if stashed is not None:
                    message = stashed.get("message", "")
                    stashed_images = stashed.get("images") or []
                    if stashed_images and not raw_images:
                        raw_images = "|".join(stashed_images)
                # unknown/expired mid: fall through gracefully with whatever
                # message/images (if any) were also on the query string.
            imgs = []
            upload_dir_real = os.path.realpath(_UPLOAD_DIR)
            for p in raw_images.split("|"):
                if not p:
                    continue
                real_p = os.path.realpath(p)
                try:
                    if os.path.commonpath([upload_dir_real, real_p]) == upload_dir_real:
                        imgs.append(p)
                    else:
                        return self._send(400, json.dumps({"error": f"Invalid image path: {p}"}))
                except ValueError:
                    return self._send(400, json.dumps({"error": f"Invalid image path: {p}"}))
            plan_val = q.get("plan", ["false"])[0].lower() in ("true", "1", "yes")
            resume_val = q.get("resume", ["false"])[0].lower() in ("true", "1", "yes")
            return self._stream_chat(message, imgs, plan=plan_val, resume=resume_val)
        if u.path == "/api/terminal/stream":
            q = parse_qs(u.query)
            return self._stream_terminal(q.get("id", [""])[0])
        if u.path == "/api/pilot":
            q = parse_qs(u.query)
            return self._swap_pilot(q.get("model", [""])[0])
        if u.path == "/api/context/usage":
            try:
                usage = _pilot.get_context_usage()
                return self._send(200, json.dumps(usage))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
        if u.path == "/api/workspaces":
            return self._send(200, json.dumps(_ws.list_workspaces(_cfg.repo)))
        if u.path == "/api/worktrees":
            from . import worktrees as _wt
            return self._send(200, json.dumps({
                "worktrees": _wt.list_worktrees(_cfg.repo),
                "max": _wt.get_max_worktrees()
            }))
        if u.path == "/api/hooks":
            from . import hooks as _hk
            return self._send(200, json.dumps({
                "hooks": _hk.get_hooks(),
                "events": _hk.ALLOWED_EVENTS
            }))
        if u.path == "/api/sessions/transcript":
            q = parse_qs(u.query)
            sid = q.get("session", [None])[0] or _sessions.active or ""
            data = load_transcript(_cfg.state_dir or _tf.gettempdir(), sid)
            if isinstance(data, dict):
                history_list = data.get("history", [])
                display_list = data.get("display", [])
                job_ids_list = data.get("job_ids", [])
            else:
                history_list = data
                display_list = []
                job_ids_list = []
            return self._send(200, json.dumps({
                "history": history_list,
                "display": display_list,
                "job_ids": job_ids_list
            }))
        if u.path == "/api/sessions/export":
            q = parse_qs(u.query)
            sid = q.get("session", [None])[0] or _sessions.active or ""
            fmt = q.get("format", ["json"])[0]
            
            meta = next((s for s in _sessions._sessions if s["id"] == sid), None)
            data = load_transcript(_cfg.state_dir or _tf.gettempdir(), sid)
            if isinstance(data, dict):
                history = data.get("history", [])
            else:
                history = data
            
            title = meta.get("title", "Unknown Session") if meta else "Unknown Session"
            filename_base = meta.get("title") if meta else ""
            if not filename_base:
                filename_base = sid or "session"
            
            import re
            safe_title = re.sub(r'[^a-zA-Z0-9\-_]', '_', filename_base)
            safe_title = re.sub(r'_+', '_', safe_title)
            safe_title = safe_title.strip('_-')
            if not safe_title:
                safe_title = sid or "session"
                
            if fmt == "md":
                import datetime
                import time
                created = meta.get("created") if meta else None
                created_str = datetime.datetime.fromtimestamp(created).strftime('%Y-%m-%d %H:%M:%S') if created else "Unknown"
                exported_str = datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S')
                
                md_lines = []
                md_lines.append(f"# {title or 'Unknown Session'}")
                md_lines.append("")
                md_lines.append(f"**Session ID:** {sid}  ")
                md_lines.append(f"**Created:** {created_str}  ")
                md_lines.append(f"**Exported:** {exported_str}")
                md_lines.append("")
                
                for msg in history:
                    role = msg.get("role", "").capitalize()
                    content = msg.get("content", "")
                    md_lines.append(f"## {role}")
                    md_lines.append("")
                    md_lines.append(content)
                    md_lines.append("")
                
                body = "\n".join(md_lines)
                data = body.encode("utf-8")
                filename = f"{safe_title}.md"
                ctype = "text/markdown"
            else:
                import time
                created = meta.get("created") if meta else None
                export_data = {
                    "session_id": sid,
                    "title": title or "Unknown Session",
                    "created": created,
                    "exported_at": time.time(),
                    "messages": history
                }
                body = json.dumps(export_data, indent=2)
                data = body.encode("utf-8")
                filename = f"{safe_title}.json"
                ctype = "application/json"
                
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return
        if u.path == "/api/sessions":
            # Optional ?repo=<path> lists sessions for that root WITHOUT switching
            # the active workspace (LeftRail prefetches every project row).
            q = parse_qs(u.query)
            repo_override = (q.get("repo", [""])[0] or "").strip()
            root = repo_override or (_cfg.repo or "")
            return self._send(200, json.dumps(_sessions.list(
                workspace_root=root,
                state_dir=_sessions_state_dir(),
            )))
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
        """Write one SSE frame. Returns False if the client has detached.

        View detach (EventSource close / navigate away) must NOT cancel the
        in-flight turn -- only /api/session/interrupt does. Callers drain the
        generator after a False return so _busy still releases via the
        generator's own finally.
        """
        try:
            self.wfile.write(payload)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # ConnectionAbortedError is the common Windows EventSource/nav-close
            # path; it is not a subclass of BrokenPipe/Reset. Treat it as detach
            # so the pump can keep draining instead of gen.close()-aborting mid-yield.
            return False

    def _sse_pump(self, gen, frame_for_event, *, on_event=None, write_done: bool = True) -> bool:
        """Pump a turn generator over SSE with Hermes-style detach semantics.

        While the UI is attached, each event is written. On client disconnect we
        keep consuming the generator (dropping frames) so the pilot turn finishes
        and releases _busy -- we never call _pilot.cancel() here. Explicit Stop
        still goes through /api/session/interrupt.

        Returns True if the client detached mid-stream.
        """
        detached = False
        try:
            for ev in gen:
                if on_event is not None:
                    on_event(ev)
                if detached:
                    continue
                if not self._sse_write(frame_for_event(ev)):
                    detached = True
            if write_done and not detached:
                self._sse_write(b"data: {\"kind\": \"done\"}\n\n")
        finally:
            # Exhausted generators are a no-op; if the turn raised, close still
            # runs the generator finally so the session lock cannot leak.
            try:
                gen.close()
            except Exception:
                pass
        return detached

    def _stream_run(self, prompt: str, images=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        
        if _sessions.active and prompt:
            from .sessions import derive_title
            _sessions.set_title_if_default(_sessions.active, derive_title(prompt))

        if _cfg.repo and os.path.isdir(_cfg.repo):
            _maybe_refresh_codegraph(_cfg.repo)

        pre = _session.preflight()
        if pre:
            self.wfile.write(f"data: {json.dumps({'kind':'error','turn':0,'data':{'error':pre}})}\n\n".encode())
            self.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
            self.wfile.flush()
            return

        from .hooks import run_hooks
        # Bind turn identity before any view switch can reassign globals.
        turn_pilot = _pilot
        turn_sid = _sessions.active or getattr(turn_pilot, "harness_session_id", "") or ""
        ctx = {"session_id": turn_sid, "prompt": prompt, "pilot": turn_pilot}
        run_hooks("preRun", ctx)
        gen = _session.run(prompt, images=images or None)
        try:
            self._sse_pump(
                gen,
                lambda ev: (
                    f"data: {json.dumps({'kind': ev.kind, 'turn': ev.turn, 'data': ev.data})}\n\n"
                ).encode(),
            )
        finally:
            run_hooks("postRun", ctx)

    def _stream_auto(self, objective: str):
        """Stream the fully-auto loop (governor-bounded) over SSE."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        
        if _sessions.active and objective:
            from .sessions import derive_title
            _sessions.set_title_if_default(_sessions.active, derive_title(objective))

        if _cfg.repo and os.path.isdir(_cfg.repo):
            _maybe_refresh_codegraph(_cfg.repo)

        from .hooks import run_hooks
        # Bind turn identity before any view switch can reassign globals.
        turn_pilot = _pilot
        turn_sid = _sessions.active or getattr(turn_pilot, "harness_session_id", "") or ""
        ctx = {"session_id": turn_sid, "objective": objective, "pilot": turn_pilot}
        run_hooks("preRun", ctx)
        budget = AutoBudget.from_env()
        gen = turn_pilot.run_auto(objective, budget)
        last_ckpt = time.monotonic()

        def _maybe_checkpoint(ev):
            nonlocal last_ckpt
            # Incremental checkpoint: flush immediately after an appended action
            # result, else on a 2s throttle, so a crash mid governor-loop can't
            # lose the last chunk of transcript before _finalize_turn runs.
            if ev.kind in _CHECKPOINT_KINDS:
                _checkpoint_transcript(ctx)
                last_ckpt = time.monotonic()
            elif time.monotonic() - last_ckpt >= 2.0:
                _checkpoint_transcript(ctx)
                last_ckpt = time.monotonic()

        try:
            # Detach != cancel: closing the EventSource must not stop the
            # governor. Explicit Stop uses /api/session/interrupt -> cancel().
            self._sse_pump(
                gen,
                lambda ev: f"data: {json.dumps({'kind': ev.kind, 'data': ev.data})}\n\n".encode(),
                on_event=_maybe_checkpoint,
            )
        finally:
            _finalize_turn(ctx)

    def _swap_pilot(self, model: str):
        """Hot-swap the pilot model (the whole point: your key -> your pilot).

        Preserves the in-flight conversation: history, auto-distill, and MCP are
        carried onto the rebuilt pilot (mirrors _rebuild_pilot_and_session). A
        bare rebuild dropped history, so swapping mid-conversation silently reset
        the context to empty. We also refuse a swap while a turn is streaming, so
        the old pilot's busy stream is never orphaned underneath a fresh object."""
        global _pilot
        if not model:
            return self._send(400, json.dumps({"error": "model required"}))
        # Do not swap underneath a live stream -- let it finish or be cancelled.
        if getattr(_pilot, "_busy", None) is not None and _pilot._busy.locked():
            return self._send(409, json.dumps({
                "error": "a turn is in progress; stop it before switching models"}))
        try:
            with _pilot_swap_lock:
                old_history = getattr(_pilot, "_history", None)
                old_auto_distill = getattr(_pilot, "_auto_distill", False)
                old_pilot = _pilot
                _cfg.driver = model
                _apply_model_context_window()
                # Frozen per-runner config; meters copied because this replaces
                # the SAME view's runner (not a new attach).
                _pilot = ConversationalSession(_runner_config_snapshot())
                if old_history is not None:
                    _pilot._history = old_history
                _pilot._auto_distill = old_auto_distill
                _copy_pilot_meters(old_pilot, _pilot)
                _pilot._mcp = _mcp
                active_id = _sessions.active or _runners.active_view_id
                if active_id:
                    _runners.drop(active_id, notify=False)
                    _runners.get_or_create(active_id, lambda: _pilot)
                    _runners.set_active_view(active_id)
            # Remember this model for the current workspace so switching dirs and
            # coming back restores it.
            _save_workspace_driver(_cfg.repo, model)
            return self._send(200, json.dumps({"ok": True, "driver": model}))
        except Exception as e:
            return self._send(500, json.dumps({"error": str(e)}))

    def _stream_terminal(self, sid: str):
        """Stream PTY output over SSE. Client sends keystrokes via POST /api/terminal/write."""
        sess = _pty.get(sid)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        if not sess:
            try:
                self.wfile.write(b"data: {\"kind\": \"exit\"}\n\n")
                self.wfile.flush()
            except Exception:
                pass
            return
        offset = 0
        try:
            while sess.alive():
                data, offset = sess.read_since(offset)
                if data:
                    import base64 as _b64
                    payload = json.dumps({"kind": "data", "b64": _b64.b64encode(data).decode("ascii")})
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                else:
                    time.sleep(0.05)
            # flush any final bytes after exit
            data, offset = sess.read_since(offset)
            if data:
                import base64 as _b64
                payload = json.dumps({"kind": "data", "b64": _b64.b64encode(data).decode("ascii")})
                self.wfile.write(f"data: {payload}\n\n".encode())
            self.wfile.write(b"data: {\"kind\": \"exit\"}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream_chat(self, message: str, images=None, plan: bool = False, resume: bool = False):
        """Stream the conversational PILOT loop: prose messages + collapsible
        action cards (run_swarm) + assistant_done.

        ``resume=True`` runs a keep-alive continuation turn: no new user message
        is appended -- the pilot generates off the history that drain_swarm_results
        already extended with the finished job's result + continuation."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        
        if _sessions.active and message:
            from .sessions import derive_title
            _sessions.set_title_if_default(_sessions.active, derive_title(message))

        # Self-healing CodeGraph: debounced staleness check at the start of every
        # turn, so an index that drifted (files edited/added/DELETED since the last
        # build) reindexes in the background before it misleads the pilot. The
        # debounce in _maybe_refresh_codegraph prevents thrash during rapid turns.
        if _cfg.repo and os.path.isdir(_cfg.repo):
            _maybe_refresh_codegraph(_cfg.repo)

        # Resolve @-file and @symbol mentions in message
        resolved_files = []
        resolved_symbols = []
        total_size = 0
        repo = _cfg.repo
        if repo and os.path.isdir(repo) and message:
            import re
            tokens = re.findall(r'@([a-zA-Z0-9_\-\.\/:]+)', message)
            seen_tokens = set()
            for token in tokens:
                if token in seen_tokens:
                    continue
                seen_tokens.add(token)
                
                is_symbol_prefix = token.startswith("symbol:")
                symbol_name = token[7:] if is_symbol_prefix else token
                
                is_file = False
                file_to_read = None
                if not is_symbol_prefix:
                    full_path = os.path.abspath(os.path.join(repo, token))
                    repo_real = os.path.realpath(repo)
                    full_real = os.path.realpath(full_path)
                    try:
                        common = os.path.commonpath([repo_real, full_real])
                        if common == repo_real and os.path.isfile(full_real):
                            is_file = True
                            file_to_read = full_real
                    except Exception:
                        pass
                    # Also accept files dropped from OUTSIDE the workspace: the
                    # composer uploads those into the trusted upload dir and
                    # references them by absolute path. Allow reading that path
                    # too (drag-and-drop of external files).
                    if not is_file:
                        try:
                            upload_real = os.path.realpath(_UPLOAD_DIR)
                            abs_token = os.path.realpath(os.path.abspath(token))
                            if (os.path.commonpath([upload_real, abs_token]) == upload_real
                                    and os.path.isfile(abs_token)):
                                is_file = True
                                file_to_read = abs_token
                        except Exception:
                            pass
                
                if is_file and file_to_read:
                    try:
                        size = os.path.getsize(file_to_read)
                        read_size = min(size, 50 * 1024)
                        if total_size + read_size <= 150 * 1024:
                            with open(file_to_read, 'r', encoding='utf-8', errors='replace') as f:
                                content = f.read(read_size)
                            resolved_files.append(f"--- File: {token} ---\n{content}\n")
                            total_size += len(content.encode('utf-8'))
                    except Exception:
                        pass
                else:
                    try:
                        import puppetmaster.codegraph as cg
                        if cg.codegraph_available() and cg.codegraph_ready(repo):
                            res = cg.codegraph_query(search=symbol_name, cwd=repo, limit=1)
                            if res.get("ok") and res.get("stdout"):
                                data = json.loads(res["stdout"])
                                if isinstance(data, list) and len(data) > 0:
                                    node = data[0].get("node")
                                    if node:
                                        file_path = node.get("filePath")
                                        start_line = node.get("startLine")
                                        end_line = node.get("endLine")
                                        name = node.get("name")
                                        
                                        if file_path and start_line is not None:
                                            sym_full_path = os.path.abspath(os.path.join(repo, file_path))
                                            repo_real = os.path.realpath(repo)
                                            sym_full_real = os.path.realpath(sym_full_path)
                                            common = os.path.commonpath([repo_real, sym_full_real])
                                            if common == repo_real and os.path.isfile(sym_full_real):
                                                with open(sym_full_real, 'r', encoding='utf-8', errors='replace') as f:
                                                    lines = f.readlines()
                                                
                                                start_idx = max(0, int(start_line) - 1)
                                                if end_line is not None:
                                                    end_idx = min(len(lines), int(end_line))
                                                else:
                                                    end_idx = min(len(lines), start_idx + 60)
                                                
                                                snippet_lines = lines[start_idx:end_idx]
                                                snippet = "".join(snippet_lines)
                                                if len(snippet.encode('utf-8')) > 8 * 1024:
                                                    snippet = snippet.encode('utf-8')[:8 * 1024].decode('utf-8', errors='ignore')
                                                
                                                read_size = len(snippet.encode('utf-8'))
                                                if total_size + read_size <= 150 * 1024:
                                                    resolved_symbols.append(f"--- Symbol: {name} ({file_path}:{start_line}) ---\n{snippet}\n")
                                                    total_size += read_size
                    except Exception:
                        pass
            
            context_blocks = []
            if resolved_files:
                context_blocks.append("Referenced files:\n" + "\n".join(resolved_files))
            if resolved_symbols:
                context_blocks.append("Referenced symbols:\n" + "\n".join(resolved_symbols))
            
            if context_blocks:
                message = "\n\n".join(context_blocks) + "\n\n" + message
 
        pre = _pilot_preflight()
        if pre:
            self.wfile.write(f"data: {json.dumps({'kind':'error','data':{'error':pre}})}\n\n".encode())
            self.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
            self.wfile.flush()
            return
 
        from .hooks import run_hooks
        # Bind turn identity before any view switch can reassign globals.
        turn_pilot = _pilot
        turn_sid = _sessions.active or getattr(turn_pilot, "harness_session_id", "") or ""
        ctx = {"session_id": turn_sid, "message": message, "pilot": turn_pilot}
        run_hooks("preRun", ctx)
        # Detach != cancel: if the client closes the EventSource mid-turn we keep
        # draining send() so its finally releases _busy. Closing the generator
        # early (old behavior) aborted the turn via GeneratorExit; cancel() on
        # BrokenPipe (auto path) stopped the governor for a mere view switch.
        # Explicit Stop still uses /api/session/interrupt.
        gen = turn_pilot.send(message, images=images or None, plan=plan, resume=resume)
        last_ckpt = time.monotonic()

        def _maybe_checkpoint(ev):
            nonlocal last_ckpt
            # Incremental checkpoint: flush the transcript immediately when an
            # action result was just appended to history, else on a 2s throttle
            # so a mid-turn crash can't lose the last chunk of transcript.
            if ev.kind in _CHECKPOINT_KINDS:
                _checkpoint_transcript(ctx)
                last_ckpt = time.monotonic()
            elif time.monotonic() - last_ckpt >= 2.0:
                _checkpoint_transcript(ctx)
                last_ckpt = time.monotonic()

        try:
            detached = self._sse_pump(
                gen,
                lambda ev: f"data: {json.dumps({'kind': ev.kind, 'data': ev.data})}\n\n".encode(),
                on_event=_maybe_checkpoint,
                write_done=False,
            )
            # After a chat turn streams its events, also drain ready swarm results
            # (drop frames if the UI already detached).
            for ev in turn_pilot.drain_swarm_results():
                _maybe_checkpoint(ev)
                if detached:
                    continue
                if not self._sse_write(
                    f"data: {json.dumps({'kind': ev.kind, 'data': ev.data})}\n\n".encode()
                ):
                    detached = True
            if not detached:
                self._sse_write(b"data: {\"kind\": \"done\"}\n\n")
        finally:
            _finalize_turn(ctx)


# Event kinds that mean a tool result / action completion has just been appended
# to _history -- checkpoint immediately (ignoring throttle) when we see one so a
# crash right after an action never loses that appended chunk of the transcript.
_CHECKPOINT_KINDS = frozenset({"action_result", "swarm_result"})


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

    The current driver is forced first so the picker shows it selected. If the
    user has not curated anything yet, enabled_pilots() falls back to the full
    available set."""
    from . import model_visibility as _mv
    cur = _cfg.driver
    pilots = _mv.enabled_pilots()
    # ensure the current driver appears first (it may already be in the list)
    ordered = [cur] + [p for p in pilots if p != cur]
    # De-dup while preserving order.
    seen = set()
    out = []
    for s in ordered:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out or [cur]


def _get_settings_dict():
    from harness.hash_edit import hash_edit_enabled

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
        "state_dir": _session.state_dir,
        "repo": _cfg.repo,
        "has_api_key": status["has_key"],
        "api_key_masked": status["masked"],
        "masked": status["masked"],
        "key_env_var": get_env_var_for_reach(reach),
        "preflight_ok": preflight_ok,
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
