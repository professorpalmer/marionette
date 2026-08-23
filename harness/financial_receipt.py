"""Receipt-first spend labels for local jobs and consumed PM cost reports.

Not a master economics object and not a second ledger. Local jobs mint one
terminal receipt; PM jobs carry the structured report already produced by
``puppetmaster cost <job_id> --json``. Forecasts never become spend.
"""
from __future__ import annotations

from typing import Any, Optional

SPEND_PROVIDER = "provider"
SPEND_MEASURED = "measured"
SPEND_ESTIMATED = "estimated"
SPEND_UNAVAILABLE = "unavailable"

LABEL_PROVIDER = "Provider-reported cost"
LABEL_MEASURED = "Measured usage cost"
LABEL_ESTIMATED = "Estimated cost"
LABEL_FORECAST = "Route forecast"
LABEL_SAVINGS = "Estimated savings"
LABEL_UNAVAILABLE = "Cost unavailable"


def format_job_identifier(job_id: str) -> str:
    """One copyable identifier: ``Job local-...`` or ``Job job_...``."""
    jid = str(job_id or "").strip()
    if not jid:
        return "Job"
    return "Job %s" % jid


def _money(amount: float, estimated: bool) -> str:
    body = "$%.4f" % float(amount)
    return "~%s" % body if estimated else body


def format_spend_label(
    amount: Optional[float],
    basis: str,
) -> str:
    """Named spend string. Unknown never paints as measured $0."""
    if basis == SPEND_UNAVAILABLE or amount is None:
        return LABEL_UNAVAILABLE
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return LABEL_UNAVAILABLE
    if basis == SPEND_PROVIDER:
        return "%s %s" % (LABEL_PROVIDER, _money(value, False))
    if basis == SPEND_MEASURED:
        return "%s %s" % (LABEL_MEASURED, _money(value, False))
    return "%s %s" % (LABEL_ESTIMATED, _money(value, True))


def format_forecast_label(amount: Optional[float]) -> str:
    if amount is None:
        return LABEL_UNAVAILABLE
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return LABEL_UNAVAILABLE
    if value <= 0:
        return LABEL_UNAVAILABLE
    return "%s %s" % (LABEL_FORECAST, _money(value, True))


def format_savings_label(amount: Optional[float]) -> str:
    if amount is None:
        return LABEL_UNAVAILABLE
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return LABEL_UNAVAILABLE
    if value <= 0:
        return LABEL_UNAVAILABLE
    return "%s %s" % (LABEL_SAVINGS, _money(value, True))


def spend_basis_from_provenance(
    *,
    estimated: bool,
    cost_provenance: str,
    has_amount: bool,
) -> str:
    if not has_amount:
        return SPEND_UNAVAILABLE
    provenance = str(cost_provenance or "").strip().lower()
    if provenance == "provider" and not estimated:
        return SPEND_PROVIDER
    if provenance in ("live", "static") or (
        provenance == "provider" and estimated
    ):
        return SPEND_MEASURED if provenance == "live" and not estimated else (
            SPEND_MEASURED if provenance in ("live", "static") and not estimated
            else SPEND_ESTIMATED
        )
    if estimated:
        return SPEND_ESTIMATED
    if provenance == "unknown":
        return SPEND_UNAVAILABLE
    return SPEND_ESTIMATED


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _route_forecast_from_artifacts(artifacts: Any) -> Optional[float]:
    if not isinstance(artifacts, list):
        return None
    total = 0.0
    found = False
    for art in artifacts:
        if not isinstance(art, dict):
            continue
        if str(art.get("type") or "").strip().upper() != "ROUTING":
            continue
        amount = _as_float(art.get("est_cost_usd") or art.get("estimated_cost_usd"))
        if amount is None:
            continue
        total += amount
        found = True
    if not found:
        return None
    return round(total, 6)


def build_local_financial_receipt(
    job_id: str,
    *,
    spend_usd: float = 0.0,
    estimated: bool = True,
    cost_provenance: str = "default",
    tokens: int = 0,
    artifacts: Optional[list] = None,
    routing_saved_usd: float = 0.0,
    routing_savings_basis: str = "estimated",
    provider_cost_usd: Optional[float] = None,
) -> dict:
    """One terminal receipt for a Marionette-owned local job."""
    has_provider = provider_cost_usd is not None and float(provider_cost_usd or 0.0) > 0
    amount = float(spend_usd or 0.0)
    if has_provider:
        amount = float(provider_cost_usd or 0.0)
        basis = SPEND_PROVIDER
        estimated = False
        cost_provenance = "provider"
    else:
        basis = spend_basis_from_provenance(
            estimated=estimated,
            cost_provenance=cost_provenance,
            has_amount=amount > 0 or (int(tokens or 0) > 0 and amount == 0 and cost_provenance == "provider" and not estimated),
        )
        if amount <= 0 and int(tokens or 0) <= 0:
            basis = SPEND_UNAVAILABLE
        elif amount <= 0 and estimated:
            basis = SPEND_UNAVAILABLE
    forecast = _route_forecast_from_artifacts(artifacts)
    savings = _as_float(routing_saved_usd) or 0.0
    receipt = {
        "job_id": str(job_id or "").strip(),
        "owner": "local",
        "spend_usd": round(amount, 6) if basis != SPEND_UNAVAILABLE else None,
        "spend_basis": basis,
        "estimated": bool(estimated) if basis != SPEND_PROVIDER else False,
        "cost_provenance": (
            "provider" if basis == SPEND_PROVIDER
            else ("unknown" if basis == SPEND_UNAVAILABLE else str(cost_provenance or "default"))
        ),
        "tokens": int(tokens or 0),
        "route_forecast_usd": forecast,
        "estimated_savings_usd": round(savings, 6) if savings > 0 else None,
        "estimated_savings_basis": (
            str(routing_savings_basis or "estimated") if savings > 0 else None
        ),
        "label": format_spend_label(
            amount if basis != SPEND_UNAVAILABLE else None,
            basis,
        ),
        "forecast_label": format_forecast_label(forecast),
        "savings_label": format_savings_label(savings if savings > 0 else None),
        "identifier": format_job_identifier(job_id),
    }
    return receipt


def apply_local_receipt(job: dict, receipt: dict) -> dict:
    """Persist the receipt and project the same spend onto workers.

    One worker: job cost equals worker cost.
    Multiple workers without per-task receipts: keep the job figure only.
    """
    if not isinstance(job, dict) or not isinstance(receipt, dict):
        return job
    job["financial_receipt"] = receipt
    spend = receipt.get("spend_usd")
    basis = str(receipt.get("spend_basis") or SPEND_UNAVAILABLE)
    if spend is not None:
        job["est_cost_usd"] = round(float(spend), 6)
    elif basis == SPEND_UNAVAILABLE:
        # Do not keep a leftover routing forecast as spend.
        if float(job.get("est_cost_usd") or 0.0) > 0 and _route_forecast_from_artifacts(
            job.get("artifacts")
        ) == float(job.get("est_cost_usd") or 0.0):
            job["est_cost_usd"] = 0.0
    job["estimated"] = bool(receipt.get("estimated"))
    job["cost_provenance"] = str(receipt.get("cost_provenance") or job.get("cost_provenance") or "default")
    if receipt.get("route_forecast_usd") is not None:
        job["route_forecast_usd"] = receipt.get("route_forecast_usd")
    if receipt.get("estimated_savings_usd") is not None:
        job["routing_saved_usd"] = receipt.get("estimated_savings_usd")
        job["routing_savings_basis"] = receipt.get("estimated_savings_basis") or "estimated"
    tasks = job.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return job
    concrete = [
        t for t in tasks
        if isinstance(t, dict) and t.get("est_cost_usd") is not None
    ]
    if len(tasks) == 1 and isinstance(tasks[0], dict):
        _copy_spend_onto_task(tasks[0], job, receipt)
    elif len(tasks) > 1 and not concrete:
        # Do not invent a split. Job estimate stays on the job only.
        pass
    return job


def _copy_spend_onto_task(task: dict, job: dict, receipt: dict) -> None:
    spend = receipt.get("spend_usd")
    if spend is not None:
        task["est_cost_usd"] = round(float(spend), 6)
    elif job.get("est_cost_usd") is not None:
        task["est_cost_usd"] = round(float(job.get("est_cost_usd") or 0.0), 6)
    task["estimated"] = bool(receipt.get("estimated", job.get("estimated", True)))
    task["cost_provenance"] = str(
        receipt.get("cost_provenance") or job.get("cost_provenance") or "default"
    )
    if not task.get("tokens") and job.get("tokens"):
        task["tokens"] = int(job.get("tokens") or 0)


def project_historical_one_worker_spend(job: dict) -> dict:
    """Historical one-worker jobs reuse the sound job estimate as Estimated cost.

    Never copies a ROUTING forecast onto the worker as spend.
    """
    if not isinstance(job, dict):
        return job
    tasks = job.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
        return job
    task = tasks[0]
    if task.get("est_cost_usd") is not None:
        return job
    job_spend = _as_float(job.get("est_cost_usd"))
    forecast = _route_forecast_from_artifacts(job.get("artifacts"))
    receipt = job.get("financial_receipt")
    if isinstance(receipt, dict) and receipt.get("spend_usd") is not None:
        _copy_spend_onto_task(task, job, receipt)
        return job
    if job_spend is None:
        return job
    # A lone job figure that is only the routing forecast is not spend.
    if forecast is not None and abs(job_spend - forecast) < 1e-9 and job.get("estimated") is not False:
        if not job.get("tokens"):
            return job
    if job_spend > 0 or job.get("tokens"):
        task["est_cost_usd"] = round(job_spend, 6)
        task["estimated"] = bool(job.get("estimated", True))
        task["cost_provenance"] = str(job.get("cost_provenance") or "default")
        if not task.get("tokens") and job.get("tokens"):
            task["tokens"] = int(job.get("tokens") or 0)
    return job


def consume_pm_cost_report(report: dict) -> dict:
    """Normalize PM ``cost --json`` output. Does not recalculate prices."""
    if not isinstance(report, dict):
        return {}
    actual = report.get("actual_cost") if isinstance(report.get("actual_cost"), dict) else {}
    counterfactual = report.get("counterfactual")
    if counterfactual is not None and not isinstance(counterfactual, dict):
        counterfactual = None
    spend_usd, basis, estimated, provenance = _spend_from_actual(actual)
    forecast = _as_float(report.get("total_estimated_cost_usd"))
    savings = None
    if isinstance(counterfactual, dict):
        savings = _as_float(counterfactual.get("avoided_usd"))
    job_id = str(report.get("job_id") or "").strip()
    return {
        "job_id": job_id,
        "owner": "pm",
        "source": "puppetmaster_cost_json",
        "pm_report": report,
        "spend_usd": spend_usd,
        "spend_basis": basis,
        "estimated": estimated,
        "cost_provenance": provenance,
        "route_forecast_usd": forecast,
        "estimated_savings_usd": savings,
        "estimated_savings_basis": "estimated" if savings else None,
        "counterfactual": counterfactual,
        "label": format_spend_label(spend_usd, basis),
        "forecast_label": format_forecast_label(forecast),
        "savings_label": format_savings_label(savings),
        "identifier": format_job_identifier(job_id),
    }


def _spend_from_actual(actual: dict) -> tuple:
    if not actual:
        return None, SPEND_UNAVAILABLE, True, "unknown"
    measured = _as_float(actual.get("measured_cost_usd"))
    estimated_cost = _as_float(actual.get("estimated_cost_usd"))
    total = _as_float(actual.get("total_marginal_cost_usd"))
    measured_runs = int(actual.get("measured_runs") or 0)
    estimated_runs = int(actual.get("estimated_runs") or 0)
    priced = int(actual.get("priced_tasks") or 0)
    plan = False
    by_model = actual.get("by_model") or {}
    if isinstance(by_model, dict) and by_model:
        billings = [
            str((bucket or {}).get("billing") or "").strip().lower()
            for bucket in by_model.values()
            if isinstance(bucket, dict)
        ]
        plan = bool(billings) and all(b == "plan" for b in billings)
    if not priced and total is None:
        return None, SPEND_UNAVAILABLE, True, "unknown"
    if plan:
        return 0.0, SPEND_PROVIDER, False, "plan"
    if measured_runs and not estimated_runs and measured is not None:
        return measured, SPEND_MEASURED, False, "provider"
    if estimated_runs and not measured_runs and estimated_cost is not None:
        return estimated_cost, SPEND_ESTIMATED, True, "static"
    if total is not None:
        mixed = bool(estimated_runs and measured_runs)
        return total, (SPEND_ESTIMATED if mixed or estimated_runs else SPEND_MEASURED), mixed or bool(estimated_runs), (
            "mixed" if mixed else "static"
        )
    return None, SPEND_UNAVAILABLE, True, "unknown"


def try_pm_build_cost_report():
    """Reusable PM function if the leftover extract has landed; else None."""
    try:
        from puppetmaster.cost import build_cost_report  # type: ignore
    except Exception:
        return None
    if callable(build_cost_report):
        return build_cost_report
    return None


def assemble_pm_cost_report(store: Any, job_id: str, registry: Optional[list] = None) -> dict:
    """Consume PM primitives into the CLI ``cost --json`` shape.

    Leftover: Puppetmaster should expose this as one reusable function shared
    by CLI, MCP, and Mari. This assembler mirrors ``_run_cost_command`` and
    does not invent pricing.
    """
    import dataclasses

    from puppetmaster.cost import job_counterfactual, price_job
    from puppetmaster.usage import aggregate_token_usage

    artifacts = store.list_artifacts(job_id) if store is not None else []
    if registry is None:
        try:
            from puppetmaster.model_registry import default_registry_path, load_registry

            registry = load_registry(default_registry_path()) or []
        except Exception:
            registry = []
    routing_rows, routing_by_model, routing_total = _routing_estimate_rows(artifacts)
    job_cost = price_job(artifacts, registry)
    counterfactual = job_counterfactual(job_cost, registry)
    actual_by_model = {
        mid: {
            "calls": v["calls"],
            "tokens_in": v["tokens_in"],
            "tokens_out": v["tokens_out"],
            "marginal_cost_usd": v["marginal_cost_usd"],
            "billing": v["billing"],
        }
        for mid, v in (getattr(job_cost, "by_model", None) or {}).items()
    }
    return {
        "job_id": job_id,
        "cost_basis": "preflight_routing_estimate",
        "total_estimated_cost_usd": routing_total,
        "by_model": {
            mid: {"calls": v["calls"], "estimated_cost_usd": round(v["cost"], 6)}
            for mid, v in routing_by_model.items()
        },
        "token_usage": aggregate_token_usage(artifacts),
        "actual_cost": {
            "cost_basis": "measured_usage_x_registry_price",
            "total_marginal_cost_usd": job_cost.total_marginal_cost_usd,
            "measured_cost_usd": job_cost.measured_cost_usd,
            "estimated_cost_usd": job_cost.estimated_cost_usd,
            "measured_runs": job_cost.measured_runs,
            "estimated_runs": job_cost.estimated_runs,
            "priced_tasks": job_cost.priced_tasks,
            "unpriced_tasks": job_cost.unpriced_tasks,
            "by_model": actual_by_model,
            "tasks": [dataclasses.asdict(t) for t in job_cost.tasks],
        },
        "counterfactual": (
            dataclasses.asdict(counterfactual) if counterfactual is not None else None
        ),
        "tasks": routing_rows if routing_rows else [dataclasses.asdict(t) for t in job_cost.tasks],
    }


def _routing_estimate_rows(artifacts) -> tuple:
    try:
        from puppetmaster.cli.commands_gate import _routing_estimate_rows as pm_rows

        return pm_rows(artifacts)
    except Exception:
        pass
    try:
        from puppetmaster.models import ArtifactType
    except Exception:
        return [], {}, 0.0
    rows: list = []
    by_model: dict = {}
    total = 0.0
    seen: set = set()
    for artifact in artifacts or []:
        if getattr(artifact, "type", None) != ArtifactType.ROUTING:
            continue
        if getattr(artifact, "created_by", None) != "router":
            continue
        task_id = getattr(artifact, "task_id", None)
        if task_id:
            if task_id in seen:
                continue
            seen.add(task_id)
        payload = getattr(artifact, "payload", None) or {}
        model_id = payload.get("model_id", "<unknown>")
        cost = float(payload.get("estimated_cost_usd") or 0.0)
        total += cost
        rows.append({
            "task_id": task_id,
            "role": payload.get("role"),
            "model_id": model_id,
            "adapter": payload.get("adapter"),
            "policy": payload.get("policy"),
            "capability_needed": payload.get("capability_needed"),
            "estimated_cost_usd": cost,
        })
        bucket = by_model.setdefault(model_id, {"calls": 0, "cost": 0.0})
        bucket["calls"] += 1
        bucket["cost"] += cost
    return rows, by_model, round(total, 6)


def load_pm_cost_report(store: Any, job_id: str, registry: Optional[list] = None) -> dict:
    """Return the canonical PM cost report, preferring a reusable PM function."""
    builder = try_pm_build_cost_report()
    if builder is not None:
        try:
            return builder(store, job_id, registry=registry)  # type: ignore[misc]
        except TypeError:
            try:
                return builder(store, job_id)
            except Exception:
                pass
        except Exception:
            pass
    return assemble_pm_cost_report(store, job_id, registry=registry)


def persistable_pm_receipt(report: dict) -> dict:
    """Thin carry-through object stored on terminal result / live row / Economics."""
    return consume_pm_cost_report(report)
