"""Read-only Economics projection of Puppetmaster savings / cost / receipt.

Owns ``GET /api/economics``. Auth stays on ``server.Handler``; this module
never imports ``harness.server``.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Union

from harness.job_scoping import (
    ACCOUNTING_SCOPE_MARIONETTE,
    apply_job_economics_policy,
    filter_accountable_jobs,
    parse_job_session_id,
)


SCOPES = ("repo", "window30", "all_projects", "conversation")
RECENT_JOB_LIMIT = 12
ALL_PROJECTS_DIR_CAP = 32
COST_BASIS = "measured_usage_x_registry_price"
COUNTERFACTUAL_LABEL = (
    "list-price vs the named reference model, not a cash refund"
)
LABELS = {
    "routing_saved_usd": "measured when the router snapshotted a baseline",
    "codegraph_dollars_saved_est": "estimated",
    "plan_routed": "$0 marginal, not measured cash",
    "counterfactual": COUNTERFACTUAL_LABEL,
    "unknown": "unknown stays unknown; never measured $0",
}


@dataclass
class EconomicsServices:
    """Explicit deps for the economics HTTP handler (injected by ``server.py``)."""

    cfg: Any
    scoped_jobs_with_stores: Callable[..., tuple]
    diag: Callable[..., Any]
    active_session_id: Callable[[], str]


JsonPayload = Union[dict, list]


def get_economics(qs: dict, svc: EconomicsServices) -> tuple[int, JsonPayload]:
    """GET /api/economics?scope=repo|window30|all_projects|conversation&period=30|all.

    ``scope=window30`` remains a back-compat alias for ownership=repo + 30 days.
    ``period=30`` is a date cutoff on the chosen ownership, not a third ownership.
    """
    scope = _qs_scope(qs)
    if scope not in SCOPES:
        return 400, {
            "error": "scope must be repo, window30, all_projects, or conversation",
        }
    window_days = _qs_window_days(qs, scope)
    try:
        return 200, _project_economics(scope, svc, window_days)
    except Exception as exc:
        try:
            svc.diag("server.economics", exc)
        except Exception:
            pass
        payload = _unavailable_payload(scope, exc, window_days)
        payload["repo"] = _workspace_root(svc)
        return 200, payload


def _qs_scope(qs: Optional[dict]) -> str:
    if not qs:
        return "repo"
    values = qs.get("scope") or [""]
    raw = (values[0] if values else "") or ""
    return str(raw).strip() or "repo"


def _workspace_root(svc: EconomicsServices) -> str:
    repo = str(getattr(svc.cfg, "repo", None) or "").strip()
    return repo or os.getcwd()


def _short_error(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    if len(text) > 200:
        return text[:197] + "..."
    return text


def _scope_window_days(scope: str) -> Optional[float]:
    if scope == "window30":
        return 30.0
    return None


def _qs_window_days(qs: Optional[dict], scope: str) -> Optional[float]:
    """30-day cutoff from ``scope=window30`` or ``period=30``; else no window."""
    if scope == "window30":
        return 30.0
    if not qs:
        return None
    values = qs.get("period") or [""]
    raw = (values[0] if values else "") or ""
    text = str(raw).strip().lower()
    if text in ("30", "30.0"):
        return 30.0
    return None


def _ownership_from_scope(scope: str) -> str:
    if scope == "window30":
        return "repo"
    return scope


def _unavailable_payload(
    scope: str,
    exc: BaseException,
    window_days: Optional[float] = None,
) -> dict:
    return {
        "scope": scope,
        "window_days": window_days if window_days is not None else _scope_window_days(scope),
        "all_projects": scope == "all_projects",
        "savings_scope": "repo" if scope == "conversation" else scope,
        "available": False,
        "error": _short_error(exc),
        "savings": None,
        "counterfactual": None,
        "recent_jobs": [],
        "owned_jobs_considered": 0,
        "owned_actual_marginal_usd": None,
        "owned_avoided_usd": None,
        "labels": dict(LABELS),
    }


def _as_dict(value: Any) -> Optional[dict]:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    try:
        return asdict(value)
    except TypeError:
        raw = getattr(value, "__dict__", None)
        if isinstance(raw, dict):
            return {k: v for k, v in raw.items() if not str(k).startswith("_")}
        return None


def _routing_pct_cheaper(routing: Any) -> float:
    if routing is None:
        return 0.0
    pct = getattr(routing, "pct_cheaper", None)
    if pct is not None:
        try:
            return round(float(pct), 1)
        except (TypeError, ValueError):
            return 0.0
    if isinstance(routing, dict):
        if routing.get("pct_cheaper") is not None:
            try:
                return round(float(routing.get("pct_cheaper") or 0.0), 1)
            except (TypeError, ValueError):
                return 0.0
        try:
            baseline = float(routing.get("baseline_usd") or 0.0)
            saved = float(routing.get("saved_usd") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return round((saved / baseline * 100.0) if baseline else 0.0, 1)
    return 0.0


def _serialize_savings(report: Optional[dict]) -> Optional[dict]:
    if not report:
        return None
    routing = report.get("routing")
    heal = report.get("self_heal")
    cf = report.get("counterfactual")
    return {
        "window_days": report.get("window_days"),
        "jobs_considered": report.get("jobs_considered"),
        "routing": _as_dict(routing) or {},
        "routing_pct_cheaper": _routing_pct_cheaper(routing),
        "self_heal": _as_dict(heal) or {},
        "codegraph": report.get("codegraph"),
        "reads": report.get("reads"),
        "memory_cost": report.get("memory_cost"),
        "tool_offload": report.get("tool_offload")
        or {"offloads": 0, "tokens_saved": 0, "chars_saved": 0},
        "metrics": report.get("metrics"),
        "counterfactual": _as_dict(cf),
    }


def _with_counterfactual_label(cf: Optional[dict]) -> Optional[dict]:
    if not cf:
        return None
    out = dict(cf)
    out["label"] = COUNTERFACTUAL_LABEL
    return out


def _path_exists(path: Any) -> bool:
    try:
        exists = getattr(path, "exists", None)
        if callable(exists):
            return bool(exists())
        return os.path.isdir(str(path))
    except Exception:
        return False


def _path_resolved(path: Any) -> str:
    try:
        resolve = getattr(path, "resolve", None)
        if callable(resolve):
            return str(resolve())
        return os.path.abspath(os.path.expanduser(str(path)))
    except Exception:
        return str(path)


def _savings_state_dirs(scope: str, workspace: str) -> list:
    from harness.cli_job_merge import (
        is_marionette_host_scratch_dir,
        resolve_cli_state_dir,
    )

    dirs: list = []
    seen: set = set()

    primary = resolve_cli_state_dir(workspace)
    if primary and not is_marionette_host_scratch_dir(primary):
        key = _path_resolved(primary)
        seen.add(key)
        dirs.append(primary)

    if scope == "all_projects":
        from puppetmaster.state import list_project_state_dirs

        for extra in list_project_state_dirs() or []:
            if extra is None:
                continue
            if not _path_exists(extra):
                continue
            if is_marionette_host_scratch_dir(extra):
                continue
            key = _path_resolved(extra)
            if key in seen:
                continue
            seen.add(key)
            dirs.append(extra)
            if len(dirs) >= ALL_PROJECTS_DIR_CAP:
                break
    return dirs


def _open_savings_stores(dirs: list) -> list:
    from puppetmaster.store_factory import create_store

    stores = []
    for state_dir in dirs:
        try:
            stores.append(create_store("sqlite", state_dir))
        except Exception:
            # Skip a locked or unreadable extra store. A total miss still
            # yields an empty report rather than failing the pane.
            continue
    return stores


def _build_savings(
    ownership: str,
    workspace: str,
    window_days: Optional[float] = None,
) -> Optional[dict]:
    from puppetmaster.savings import build_report

    dirs = _savings_state_dirs(ownership, workspace)
    stores = _open_savings_stores(dirs)
    return build_report(stores, window_days=window_days)


def _created_at_key(job: dict) -> float:
    raw = job.get("created_at") if isinstance(job, dict) else None
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.timestamp()
    except Exception:
        return 0.0


def _owning_store(job: dict, harness_store: Any, cli_store: Any) -> Any:
    if str(job.get("source") or "").strip().lower() == "cli":
        return cli_store or harness_store
    return harness_store or cli_store


def _conversation_jobs(jobs: list, active_session_id: str) -> list:
    """Keep only Marionette-owned jobs stamped for the active session.

    Fail closed when the session id is missing: unstamped owned jobs must not
    leak other conversations into this scope.
    """
    active = (active_session_id or "").strip()
    if not active:
        return []
    owned = filter_accountable_jobs(jobs)
    out = []
    for job in owned:
        sid = parse_job_session_id(
            job.get("label"), job.get("tasks") or []
        ) or str(job.get("session_id") or "").strip()
        if sid == active:
            out.append(job)
    return out


def _serialize_job_counterfactual(cf: Any) -> Optional[dict]:
    data = _as_dict(cf)
    if not data:
        return None
    return {
        "reference_model_id": data.get("reference_model_id"),
        "reference_priced": data.get("reference_priced"),
        "naive_cost_usd": data.get("naive_cost_usd"),
        "actual_cost_usd": data.get("actual_cost_usd"),
        "avoided_usd": data.get("avoided_usd"),
        "tasks": data.get("tasks"),
    }


def _models_from_job_cost(job_cost: Any) -> list:
    by_model = (
        job_cost.get("by_model")
        if isinstance(job_cost, dict)
        else getattr(job_cost, "by_model", None)
    ) or {}
    rows = []
    for model_id, bucket in by_model.items():
        info = bucket if isinstance(bucket, dict) else {}
        rows.append({
            "model_id": model_id,
            "billing": info.get("billing"),
            "calls": info.get("calls"),
            "tokens_in": info.get("tokens_in"),
            "tokens_out": info.get("tokens_out"),
        })
    return rows


def _job_in_window(job: dict, window_days: Optional[float]) -> bool:
    if not window_days:
        return True
    created = _created_at_key(job)
    if created <= 0:
        return False
    cutoff = datetime.now(timezone.utc).timestamp() - float(window_days) * 86400.0
    return created >= cutoff


def _project_job_row(
    job: dict,
    store: Any,
    registry: list,
) -> dict:
    jid = job.get("id")
    owned = bool(apply_job_economics_policy(job).get("accounting_owned"))
    row = {
        "job_id": jid,
        "status": job.get("status"),
        "source": job.get("source"),
        "accounting_owned": owned,
        "accounting_scope": job.get("accounting_scope"),
        "models": [],
        "billing": [],
        "actual_marginal_usd": None,
        "measured_cost_usd": None,
        "estimated_cost_usd": None,
        "cost_basis": None,
        "counterfactual": None,
    }
    # Visibility-only jobs are navigation evidence, not accounting input. Do not
    # build and discard an expensive PM financial report for them.
    if not owned:
        return row

    pm_receipt = None
    if store is not None and jid and str(jid).startswith("job_"):
        try:
            from harness.financial_receipt import (
                load_pm_cost_report,
                persistable_pm_receipt,
            )
            pm_receipt = persistable_pm_receipt(
                load_pm_cost_report(store, jid, registry=registry)
            )
        except Exception:
            row["financial_error"] = True
            return row

    if pm_receipt is not None:
        actual = (pm_receipt.get("pm_report") or {}).get("actual_cost") or {}
        models = _models_from_job_cost(actual)
        spend_available = pm_receipt.get("spend_basis") != "unavailable"
        row.update({
            "financial_receipt": pm_receipt,
            "models": models,
            "billing": [
                model.get("billing") for model in models
                if model.get("billing") is not None
            ],
            "actual_marginal_usd": pm_receipt.get("spend_usd"),
            "measured_cost_usd": (
                actual.get("measured_cost_usd") if spend_available else None
            ),
            "estimated_cost_usd": (
                actual.get("estimated_cost_usd") if spend_available else None
            ),
            "priced_tasks": actual.get("priced_tasks"),
            "unpriced_tasks": actual.get("unpriced_tasks"),
            "measured_runs": actual.get("measured_runs"),
            "estimated_runs": actual.get("estimated_runs"),
            "cost_basis": (
                "plan"
                if pm_receipt.get("cost_provenance") == "plan"
                else "unknown"
                if not spend_available
                else pm_receipt.get("spend_basis")
            ),
            "counterfactual": _serialize_job_counterfactual(
                pm_receipt.get("counterfactual")
            ),
        })
        if row["counterfactual"] is not None:
            row["counterfactual"]["label"] = "Estimated savings"
        return row

    row["financial_error"] = True
    return row


def _fallback_job_row(job: dict) -> dict:
    return {
        "job_id": job.get("id"),
        "status": job.get("status"),
        "source": job.get("source"),
        "accounting_owned": bool(job.get("accounting_owned")),
        "accounting_scope": job.get("accounting_scope"),
        "models": [],
        "billing": [],
        "tokens": None,
        "actual_marginal_usd": None,
        "measured_cost_usd": None,
        "estimated_cost_usd": None,
        "cost_basis": None,
        "counterfactual": None,
        "typed_artifacts": None,
        "tokens_per_typed_artifact": None,
        "degraded_rate": None,
        "degraded_tasks": None,
        "financial_error": True,
    }


class _PrefetchedArtifacts:
    """Request-local bulk artifact view for canonical PM report builders."""

    def __init__(self, store: Any, job_ids: list[str]):
        self._store = store
        self._by_job: dict[str, list] = {}
        self._complete = False
        bulk = getattr(store, "list_artifacts_for_jobs", None)
        if not callable(bulk):
            return
        artifacts = bulk(job_ids)
        if not isinstance(artifacts, list):
            return
        self._complete = True
        for artifact in artifacts:
            job_id = str(getattr(artifact, "job_id", "") or "")
            if job_id:
                self._by_job.setdefault(job_id, []).append(artifact)

    def list_artifacts(self, job_id: str) -> list:
        if self._complete:
            return list(self._by_job.get(job_id, []))
        return self._store.list_artifacts(job_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


def _prefetch_owned_stores(
    jobs: list,
    harness_store: Any,
    cli_store: Any,
) -> tuple[Any, Any]:
    harness_ids = []
    cli_ids = []
    for job in jobs:
        job_id = str(job.get("id") or "")
        if not job.get("accounting_owned") or not job_id.startswith("job_"):
            continue
        if str(job.get("source") or "").strip().lower() == "cli":
            cli_ids.append(job_id)
        else:
            harness_ids.append(job_id)
    return (
        _PrefetchedArtifacts(harness_store, harness_ids),
        _PrefetchedArtifacts(cli_store, cli_ids),
    )


def _sum_known(values: list) -> Optional[float]:
    known = []
    for value in values:
        if value is None:
            continue
        try:
            known.append(float(value))
        except (TypeError, ValueError):
            continue
    if not known:
        return None
    return round(sum(known), 6)


def _owned_totals(rows: list) -> dict:
    owned = filter_accountable_jobs(rows)
    return {
        "owned_jobs_considered": len(owned),
        "owned_actual_marginal_usd": _sum_known([
            row.get("actual_marginal_usd") for row in owned
        ]),
        "owned_avoided_usd": _sum_known([
            (row.get("counterfactual") or {}).get("avoided_usd")
            for row in owned
            if isinstance(row.get("counterfactual"), dict)
        ]),
    }


def _aggregate_job_counterfactual(rows: list) -> tuple[Optional[dict], str]:
    """Sum the same owned job reports projected into Recent jobs."""
    comparable = []
    references = set()
    measured_cost = 0.0
    estimated_cost = 0.0
    plan_jobs = 0
    for row in filter_accountable_jobs(rows):
        if row.get("financial_error"):
            return None, "incomplete"
        cf = row.get("counterfactual")
        try:
            priced_tasks = int(row.get("priced_tasks") or 0)
            row_measured = float(row.get("measured_cost_usd") or 0.0)
            row_estimated = float(row.get("estimated_cost_usd") or 0.0)
        except (TypeError, ValueError):
            return None, "incomplete"
        if not isinstance(cf, dict) or cf.get("reference_priced") is not True:
            if priced_tasks > 0 or row_measured > 0 or row_estimated > 0:
                return None, "incomplete"
            continue
        reference = str(cf.get("reference_model_id") or "").strip()
        try:
            naive = float(cf["naive_cost_usd"])
            actual = float(cf["actual_cost_usd"])
            avoided = float(cf["avoided_usd"])
            tasks = int(cf.get("tasks") or row.get("priced_tasks") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if not reference or tasks <= 0:
            continue
        if abs((row_measured + row_estimated) - actual) > 0.000001:
            return None, "receipt_mismatch"
        references.add(reference)
        comparable.append((naive, actual, avoided, tasks))
        if row.get("cost_basis") == "plan":
            plan_jobs += 1
        measured_cost += row_measured
        estimated_cost += row_estimated
    if len(references) > 1:
        return None, "mixed_reference"
    if not comparable:
        return None, "unavailable"
    return {
        "reference_model_id": next(iter(references)),
        "reference_priced": True,
        "naive_cost_usd": round(sum(item[0] for item in comparable), 6),
        "actual_cost_usd": round(sum(item[1] for item in comparable), 6),
        "avoided_usd": round(sum(item[2] for item in comparable), 6),
        "tasks": sum(item[3] for item in comparable),
        "jobs": len(comparable),
        "measured_cost_usd": round(measured_cost, 6),
        "estimated_cost_usd": round(estimated_cost, 6),
        "spend_basis": (
            "plan" if plan_jobs == len(comparable)
            else "mixed" if measured_cost > 0 and estimated_cost > 0
            else "estimated" if estimated_cost > 0
            else COST_BASIS
        ),
        "label": COUNTERFACTUAL_LABEL,
    }, "ok"


def _scoped_job_rows(
    scope: str,
    svc: EconomicsServices,
    window_days: Optional[float] = None,
    all_project_stores: Optional[list] = None,
) -> tuple[list, list, int]:
    """Return recent rows, every owned row, and the full visible job count."""
    repo = str(getattr(svc.cfg, "repo", None) or "").strip()
    jobs, harness_store, cli_store = svc.scoped_jobs_with_stores(
        repo_root=repo or None
    )
    jobs = list(jobs or [])
    store_by_job: dict[str, Any] = {}
    if scope == "conversation":
        jobs = _conversation_jobs(jobs, svc.active_session_id())
    elif scope in ("repo", "window30"):
        jobs = [job for job in jobs if not job.get("cross_project")]
        jobs = [
            {
                **job,
                "accounting_owned": True,
                "accounting_scope": ACCOUNTING_SCOPE_MARIONETTE,
            }
            if str(job.get("id") or "").startswith("job_")
            else job
            for job in jobs
        ]
    elif scope == "all_projects":
        jobs = []
        seen_jobs: set[str] = set()
        seen_stores: set[str] = set()
        stores = [harness_store, cli_store]
        stores.extend(all_project_stores or [])
        for raw_store in stores:
            if raw_store is None:
                continue
            store_key = str(
                getattr(raw_store, "db_path", None)
                or getattr(raw_store, "root", None)
                or id(raw_store)
            )
            if store_key in seen_stores:
                continue
            seen_stores.add(store_key)
            try:
                raw_jobs = raw_store.list_jobs() or []
            except Exception:
                continue
            store_jobs = []
            for raw_job in raw_jobs:
                job_id = str(getattr(raw_job, "id", "") or "")
                if not job_id.startswith("job_") or job_id in seen_jobs:
                    continue
                row = {
                    "id": job_id,
                    "status": str(getattr(raw_job, "status", "") or ""),
                    "source": "harness" if raw_store is harness_store else "cli",
                    "accounting_owned": True,
                    "accounting_scope": ACCOUNTING_SCOPE_MARIONETTE,
                    "created_at": getattr(raw_job, "created_at", None),
                    "label": getattr(raw_job, "label", None),
                }
                if window_days and not _job_in_window(row, window_days):
                    continue
                seen_jobs.add(job_id)
                store_jobs.append(row)
            prefetched = _PrefetchedArtifacts(
                raw_store, [job["id"] for job in store_jobs]
            )
            for job in store_jobs:
                store_by_job[job["id"]] = prefetched
            jobs.extend(store_jobs)
    if window_days:
        jobs = [job for job in jobs if _job_in_window(job, window_days)]
    jobs.sort(key=_created_at_key, reverse=True)

    registry: list = []
    if jobs:
        from puppetmaster.model_registry import load_registry

        try:
            registry = load_registry() or []
        except Exception:
            registry = []

    if scope != "all_projects":
        harness_store, cli_store = _prefetch_owned_stores(
            jobs, harness_store, cli_store
        )
    recent = []
    owned = []
    for index, job in enumerate(jobs):
        is_recent = index < RECENT_JOB_LIMIT
        if not is_recent and not job.get("accounting_owned"):
            continue
        store = store_by_job.get(str(job.get("id") or "")) or _owning_store(
            job, harness_store, cli_store
        )
        try:
            row = _project_job_row(job, store, registry)
        except Exception as exc:
            try:
                svc.diag("server.economics_job", exc, msg="job=%s" % job.get("id"))
            except Exception:
                pass
            row = _fallback_job_row(job)
        if is_recent:
            recent.append(row)
        if row.get("accounting_owned"):
            owned.append(row)
    return recent, owned, len(jobs)


def _project_economics(
    scope: str,
    svc: EconomicsServices,
    window_days: Optional[float] = None,
) -> dict:
    workspace = _workspace_root(svc)
    ownership = _ownership_from_scope(scope)
    all_project_stores = []
    # Conversation is jobs-only. Do not open repo-lifetime savings stores.
    if scope == "conversation":
        savings = None
        savings_scope = "repo"
    elif scope == "all_projects":
        from puppetmaster.savings import build_report

        all_project_stores = _open_savings_stores(
            _savings_state_dirs(ownership, workspace)
        )
        savings = _serialize_savings(
            build_report(all_project_stores, window_days=window_days)
        )
        savings_scope = scope
    else:
        report = _build_savings(ownership, workspace, window_days)
        savings = _serialize_savings(report)
        savings_scope = scope

    recent_jobs, owned_rows, recent_jobs_total = _scoped_job_rows(
        scope, svc, window_days, all_project_stores
    )
    job_counterfactual, counterfactual_status = _aggregate_job_counterfactual(owned_rows)
    if job_counterfactual is not None:
        counterfactual = job_counterfactual
        counterfactual_source = "job_financial_reports"
    elif not owned_rows and savings:
        # Backward-compatible read-only fallback when this scope has no
        # Marionette-owned jobs to aggregate.
        counterfactual = _with_counterfactual_label(
            (savings or {}).get("counterfactual")
        )
        counterfactual_source = "routing_report"
        counterfactual_status = "routing_report"
    else:
        counterfactual = None
        counterfactual_source = "unavailable"
    totals = _owned_totals(owned_rows)
    return {
        "repo": workspace,
        "scope": scope,
        "ownership": ownership,
        "period": "30" if window_days else "all",
        "savings_scope": savings_scope,
        "window_days": window_days,
        "all_projects": scope == "all_projects",
        "available": True,
        "savings": savings,
        "counterfactual": counterfactual,
        "counterfactual_source": counterfactual_source,
        "counterfactual_status": counterfactual_status,
        "recent_jobs": recent_jobs,
        "recent_jobs_total": recent_jobs_total,
        "labels": dict(LABELS),
        **totals,
    }
