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
        return 200, _unavailable_payload(scope, exc, window_days)


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


def _receipt_tokens(receipt: dict) -> Optional[int]:
    tokens = receipt.get("tokens") if isinstance(receipt, dict) else None
    if isinstance(tokens, dict) and tokens.get("total_tokens") is not None:
        try:
            return int(tokens.get("total_tokens") or 0)
        except (TypeError, ValueError):
            return None
    if isinstance(tokens, dict):
        measured = (tokens.get("measured_tokens_in"), tokens.get("measured_tokens_out"))
        estimated = (tokens.get("estimated_tokens_in"), tokens.get("estimated_tokens_out"))
        try:
            if any(p is not None for p in measured):
                return int(sum(int(p or 0) for p in measured))
            if any(p is not None for p in estimated):
                return int(sum(int(p or 0) for p in estimated))
        except (TypeError, ValueError):
            return None
    return None


def _job_in_window(job: dict, window_days: Optional[float]) -> bool:
    if not window_days:
        return True
    created = _created_at_key(job)
    if created <= 0:
        return False
    cutoff = datetime.now(timezone.utc).timestamp() - float(window_days) * 86400.0
    return created >= cutoff


def _int_attr(obj: Any, name: str) -> Optional[int]:
    raw = getattr(obj, name, None)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _cost_basis_for_job(job_cost: Any) -> Optional[str]:
    """Unknown / empty / unpriced ledgers must not claim measured cash."""
    priced = _int_attr(job_cost, "priced_tasks")
    unpriced = _int_attr(job_cost, "unpriced_tasks")
    if priced is None and unpriced is None:
        return COST_BASIS
    if not priced:
        return None
    measured_runs = _int_attr(job_cost, "measured_runs") or 0
    estimated_runs = _int_attr(job_cost, "estimated_runs") or 0
    billings = []
    by_model = getattr(job_cost, "by_model", None) or {}
    for bucket in by_model.values():
        if isinstance(bucket, dict) and bucket.get("billing"):
            billings.append(str(bucket.get("billing") or "").strip().lower())
    if billings and all(item == "plan" for item in billings):
        return "plan"
    if estimated_runs and not measured_runs:
        return "estimated"
    if estimated_runs and measured_runs:
        return "mixed"
    if unpriced:
        return "mixed"
    return COST_BASIS


def _project_job_row(
    job: dict,
    store: Any,
    registry: list,
) -> dict:
    from puppetmaster.cost import job_counterfactual, price_job
    from puppetmaster.receipt import build_job_receipt

    jid = job.get("id")
    gated = apply_job_economics_policy(job)
    owned = bool(gated.get("accounting_owned"))
    artifacts: list = []
    receipt: dict = {}
    job_cost = None
    cf = None
    pm_receipt = None
    if store is not None and jid and str(jid).startswith("job_"):
        try:
            from harness.financial_receipt import (
                load_pm_cost_report,
                persistable_pm_receipt,
            )
            raw_report = load_pm_cost_report(store, jid, registry=registry)
            pm_receipt = persistable_pm_receipt(raw_report)
        except Exception:
            pm_receipt = None
    if store is not None and jid:
        try:
            artifacts = store.list_artifacts(jid) or []
        except Exception:
            artifacts = []
        try:
            receipt = build_job_receipt(store, jid) or {}
        except Exception:
            receipt = {}
        if pm_receipt is None:
            try:
                job_cost = price_job(artifacts, registry)
                cf = job_counterfactual(job_cost, registry)
            except Exception:
                job_cost = None
                cf = None

    if pm_receipt is not None:
        models = _models_from_job_cost(
            ((pm_receipt.get("pm_report") or {}).get("actual_cost") or {})
        )
    else:
        models = _models_from_job_cost(job_cost) if job_cost is not None else []
    arts = receipt.get("artifacts") if isinstance(receipt, dict) else None
    efficiency = receipt.get("efficiency") if isinstance(receipt, dict) else None
    tasks = receipt.get("tasks") if isinstance(receipt, dict) else None
    typed = arts.get("typed_total") if isinstance(arts, dict) else None
    tokens_per = (
        efficiency.get("tokens_per_typed_artifact")
        if isinstance(efficiency, dict)
        else None
    )
    degraded_rate = (
        efficiency.get("degraded_rate") if isinstance(efficiency, dict) else None
    )
    degraded_tasks = tasks.get("degraded") if isinstance(tasks, dict) else None

    row = {
        "job_id": jid,
        "status": job.get("status"),
        "source": job.get("source"),
        "accounting_owned": owned,
        "accounting_scope": job.get("accounting_scope"),
        "models": models,
        "billing": [m.get("billing") for m in models if m.get("billing") is not None],
        "tokens": _receipt_tokens(receipt),
        "actual_marginal_usd": None,
        "measured_cost_usd": None,
        "estimated_cost_usd": None,
        "cost_basis": None,
        "counterfactual": None,
        "typed_artifacts": typed,
        "tokens_per_typed_artifact": tokens_per,
        "degraded_rate": degraded_rate,
        "degraded_tasks": degraded_tasks,
    }
    # Visibility-only / unstamped CLI jobs stay listed but cannot be summed.
    # Empty or unpriced JobCost objects are 0.0 — that is not measured cash.
    if owned and pm_receipt is not None:
        row["financial_receipt"] = pm_receipt
        row["actual_marginal_usd"] = pm_receipt.get("spend_usd")
        actual = (pm_receipt.get("pm_report") or {}).get("actual_cost") or {}
        row["measured_cost_usd"] = actual.get("measured_cost_usd")
        row["estimated_cost_usd"] = actual.get("estimated_cost_usd")
        row["cost_basis"] = pm_receipt.get("spend_basis")
        row["counterfactual"] = _serialize_job_counterfactual(pm_receipt.get("counterfactual"))
        if row["counterfactual"] is not None:
            row["counterfactual"]["label"] = "Estimated savings"
    elif owned and job_cost is not None:
        basis = _cost_basis_for_job(job_cost)
        if basis is None:
            row["cost_basis"] = "unknown"
        else:
            row["actual_marginal_usd"] = getattr(job_cost, "total_marginal_cost_usd", None)
            row["measured_cost_usd"] = getattr(job_cost, "measured_cost_usd", None)
            row["estimated_cost_usd"] = getattr(job_cost, "estimated_cost_usd", None)
            row["cost_basis"] = basis
            row["counterfactual"] = _serialize_job_counterfactual(cf)
    return row


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


def _owned_totals(recent_jobs: list) -> dict:
    owned = filter_accountable_jobs(recent_jobs)
    actuals = [row.get("actual_marginal_usd") for row in owned]
    avoided = []
    for row in owned:
        cf = row.get("counterfactual") or {}
        if isinstance(cf, dict):
            avoided.append(cf.get("avoided_usd"))
        else:
            avoided.append(None)
    return {
        "owned_jobs_considered": len(owned),
        "owned_actual_marginal_usd": _sum_known(actuals),
        "owned_avoided_usd": _sum_known(avoided),
    }


def _recent_job_rows(
    scope: str,
    svc: EconomicsServices,
    window_days: Optional[float] = None,
) -> list:
    repo = str(getattr(svc.cfg, "repo", None) or "").strip()
    jobs, harness_store, cli_store = svc.scoped_jobs_with_stores(
        repo_root=repo or None
    )
    jobs = list(jobs or [])
    if scope == "conversation":
        jobs = _conversation_jobs(jobs, svc.active_session_id())
    elif scope in ("repo", "window30"):
        jobs = [job for job in jobs if not job.get("cross_project")]
    if window_days:
        jobs = [job for job in jobs if _job_in_window(job, window_days)]
    jobs.sort(key=_created_at_key, reverse=True)
    jobs = jobs[:RECENT_JOB_LIMIT]
    registry: list = []
    if jobs:
        from puppetmaster.model_registry import load_registry

        try:
            registry = load_registry() or []
        except Exception:
            registry = []
    rows = []
    for job in jobs:
        store = _owning_store(job, harness_store, cli_store)
        try:
            rows.append(_project_job_row(job, store, registry))
        except Exception as exc:
            try:
                svc.diag("server.economics_job", exc, msg="job=%s" % job.get("id"))
            except Exception:
                pass
            rows.append({
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
            })
    return rows


def _project_economics(
    scope: str,
    svc: EconomicsServices,
    window_days: Optional[float] = None,
) -> dict:
    workspace = _workspace_root(svc)
    ownership = _ownership_from_scope(scope)
    # Conversation is jobs-only. Do not open repo-lifetime savings stores.
    if scope == "conversation":
        savings = None
        counterfactual = None
        savings_scope = "repo"
    else:
        report = _build_savings(ownership, workspace, window_days)
        savings = _serialize_savings(report)
        counterfactual = _with_counterfactual_label(
            (savings or {}).get("counterfactual") if savings else None
        )
        savings_scope = scope
    recent_jobs = _recent_job_rows(scope, svc, window_days)
    totals = _owned_totals(recent_jobs)
    return {
        "scope": scope,
        "ownership": ownership,
        "period": "30" if window_days else "all",
        "savings_scope": savings_scope,
        "window_days": window_days,
        "all_projects": scope == "all_projects",
        "available": True,
        "savings": savings,
        "counterfactual": counterfactual,
        "recent_jobs": recent_jobs,
        "labels": dict(LABELS),
        **totals,
    }
