"""Shared cost / usage / swarm-accounting helpers (peeled from ``harness.server``).

Facade over :mod:`harness.api.cost_accounting`, :mod:`harness.api.usage_meters`,
and :mod:`harness.api.swarm_cost`. ``server.py`` re-exports historical ``_``
names; inject live globals via :func:`bind_deps`.
"""

from __future__ import annotations

import os
import sys
import types
from dataclasses import dataclass
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Dep injection (submodules import these accessors; define before re-exports)
# ---------------------------------------------------------------------------


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
        srv = sys.modules.get("harness.server")
        if srv is not None:
            return getattr(srv, name)
    except Exception:
        pass
    return fallback


# ---------------------------------------------------------------------------
# Submodule ownership + historical re-exports
# ---------------------------------------------------------------------------

from .cost_accounting import (  # noqa: E402
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_5M_MULTIPLIER,
    CACHE_WRITE_1H_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    CACHE_SAVINGS_CATALOG,
    CACHE_SAVINGS_CAPPED,
    CACHE_SAVINGS_UNKNOWN,
    PRICE_SOURCE_DEFAULT,
    PRICE_SOURCE_LIVE,
    PRICE_SOURCE_STATIC,
    _cache_savings,
    _cache_savings_gross,
    _cache_savings_with_basis,
    _cost_source_label,
    _job_cost,
    _job_cost_is_unsplit,
    _normalize_price_source,
    _pilot_write_buckets,
    _resolve_active_prices,
    _resolve_active_prices_with_source,
    _resolve_prices_for_runner,
    _resolve_prices_for_runner_with_source,
    _session_cost,
    _session_cost_split,
    _spend_is_estimated,
)
from .swarm_cost import (  # noqa: E402
    ROUTING_SAVINGS_ACTUAL,
    ROUTING_SAVINGS_ESTIMATED,
    ROUTING_SAVINGS_UNKNOWN,
    _COST_OPTIMIZING_POLICIES,
    _arts_for_swarm_usage,
    _cache_saved_usd_swarm,
    _job_swarm_accounting,
    _job_swarm_accounting_detail,
    _live_price_task,
    _live_price_unpriced_tasks,
    _registry_input_per_mtok,
    _routing_estimate_by_task,
    _routing_estimate_cost,
    _routing_saved_usd,
    _routing_saved_usd_detail,
    _sum_job_set_savings,
    _sum_job_set_savings_detail,
    _swarm_registry,
    _task_swarm_accounting,
    _tokens_cached_swarm,
)
from .usage_meters import (  # noqa: E402
    _BOOT_METER_ATTRS,
    _BOOT_METER_CARRY,
    _BOOT_REPOS,
    _BOOT_USAGE_PERSIST_LOCK,
    _USAGE_RESPONSE_TTL,
    _active_session_total,
    _app_run_id,
    _boot_cost_source,
    _boot_session_cost,
    _boot_usage_meters,
    _boot_usage_path,
    _fold_all_live_runners_into_boot_carry,
    _fold_runner_meters_into_boot_carry,
    _freeze_pilot_meters_into_boot_carry,
    _job_in_cost_window,
    _job_savings_fields,
    _note_boot_repo,
    _persist_boot_usage,
    _repo_session_stamped_meters,
    _restore_boot_usage,
    _tool_output_savings_fields,
    _boot_usage_reset_for_tests,
    _usage_cache_clear_for_tests,
    _usage_cache_get,
    _usage_cache_put,
    _usage_response_cache,
    _usage_response_lock,
)

# Boot scalars live in usage_meters; read/write through module aliases so
# ``harness.api.cost._COST_EPOCH = ...`` and server write-through stay coherent.
_COST_SCALAR_ALIASES = frozenset(
    {
        "_COST_EPOCH",
        "_BOOT_CARRY_COST_USD",
        "_BOOT_PLAN_BILLING",
        "_BOOT_USAGE_RESTORED",
        "_BOOT_USAGE_LAST_PERSIST",
    }
)


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


class _CostFacadeModule(types.ModuleType):
    """Write-through for boot scalars owned by :mod:`harness.api.usage_meters`."""

    def __getattr__(self, name: str):  # type: ignore[override]
        if name in _COST_SCALAR_ALIASES:
            from . import usage_meters as _um

            return getattr(_um, name)
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _COST_SCALAR_ALIASES:
            from . import usage_meters as _um

            setattr(_um, name, value)
            return
        types.ModuleType.__setattr__(self, name, value)


sys.modules[__name__].__class__ = _CostFacadeModule
