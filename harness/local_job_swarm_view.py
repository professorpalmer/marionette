from __future__ import annotations

"""Read-model projection: local job dict -> /api/swarm/live store-shaped row.

In-process ``local-*`` workers stay owned by ``swarm_local_jobs.json`` via
``LocalJobsMixin`` — this module never writes SwarmStore. It is used only at
the merge boundary in ``harness.api.jobs.get_swarm_live`` so SwarmPane sees the
same job/event/artifact vocabulary as durable store jobs.
"""

from typing import Any, Iterable, Optional

from harness.job_scoping import ACCOUNTING_SCOPE_MARIONETTE
from harness.local_job_artifacts import artifacts_are_complete

_TERMINAL_STATUSES = frozenset({
    "completed", "failed", "cancelled", "complete", "done",
    "timeout", "truncated", "partial", "timed_out",
})
_RUNNING_STATUSES = frozenset({
    "running", "in_progress", "pending", "started", "registered",
})
_NONTERMINAL_TASK_STATUSES = frozenset({
    "", "pending", "running", "in_progress", "started", "registered", "queued",
})


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_artifacts(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for art in raw:
        if isinstance(art, dict):
            out.append(dict(art))
    return out


def _artifacts_complete(raw: Any) -> bool:
    """True only when the sidecar carries a substantive, readable artifact."""
    return artifacts_are_complete(_normalize_artifacts(raw))


def _receipt_child_status_by_id(job: dict) -> dict[str, str]:
    """Map child id -> status from a durable terminal_receipt when present."""
    out: dict[str, str] = {}
    receipt = job.get("terminal_receipt")
    if not isinstance(receipt, dict):
        return out
    children = receipt.get("children")
    if not isinstance(children, list):
        return out
    for child in children:
        if not isinstance(child, dict):
            continue
        cid = str(child.get("id") or "").strip()
        status = str(child.get("status") or "").strip()
        if cid and status:
            out[cid] = status
    return out


def _parent_lifecycle_task_status(parent_status: str) -> str:
    """Normalize a stale nonterminal child when the parent is already terminal."""
    st = str(parent_status or "").strip().lower()
    if st in {"failed", "cancelled", "timeout", "truncated", "timed_out"}:
        return "failed"
    if st in {"partial"}:
        return "partial"
    if st in {"completed", "complete", "done"}:
        return "completed"
    return "completed"


def _normalize_tasks(raw: Any, *, terminal: bool, job: Optional[dict] = None) -> list[dict]:
    if not isinstance(raw, list):
        return []
    parent_status = str((job or {}).get("status") or "")
    receipt_by_id = _receipt_child_status_by_id(job or {})
    out: list[dict] = []
    for task in raw:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "")
        status = str(task.get("status") or "")
        receipt_status = receipt_by_id.get(task_id.strip())
        if receipt_status:
            status = receipt_status
        elif terminal and status.strip().lower() in _NONTERMINAL_TASK_STATUSES:
            # Authoritative live projection: hollow/pending children must not
            # disagree with a terminal parent (Composer Tasks / Swarm Tracker).
            status = _parent_lifecycle_task_status(parent_status)
        entry = {
            "id": task_id,
            "role": str(task.get("role") or ""),
            "instruction": "" if terminal else str(task.get("instruction") or ""),
            "status": status,
            "adapter": str(task.get("adapter") or ""),
        }
        task_model = str(task.get("model") or "").strip()
        if task_model:
            entry["model"] = task_model
        if task.get("completed_at") is not None:
            entry["completed_at"] = task.get("completed_at")
        if task.get("tokens") is not None:
            entry["tokens"] = _as_int(task.get("tokens"))
        if task.get("est_cost_usd") is not None:
            entry["est_cost_usd"] = round(_as_float(task.get("est_cost_usd")), 6)
        if task.get("cost_provenance"):
            entry["cost_provenance"] = task.get("cost_provenance")
        if "estimated" in task:
            entry["estimated"] = bool(task.get("estimated"))
        out.append(entry)
    return out


def _normalize_actions(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for row in raw:
        if isinstance(row, dict):
            out.append(dict(row))
    return out


def _routing_fields(job: dict) -> tuple[float, str, int, bool]:
    routing_saved = round(_as_float(job.get("routing_saved_usd")), 6)
    basis = str(job.get("routing_savings_basis") or "").strip()
    tokens_compared = _as_int(job.get("routing_tokens_compared"))
    if routing_saved > 0:
        if basis not in ("actual_usage", "estimated"):
            basis = "estimated"
        return routing_saved, basis, tokens_compared, True
    if basis in ("actual_usage", "estimated"):
        return routing_saved, basis, tokens_compared, routing_saved > 0
    return 0.0, "unknown", tokens_compared, False


def _cost_fields(job: dict) -> tuple[str, bool]:
    if "cost_provenance" in job:
        provenance = str(job.get("cost_provenance") or "default")
    elif job.get("estimated") is False:
        provenance = "provider"
    else:
        provenance = "default"
    if "estimated" in job:
        estimated = bool(job.get("estimated"))
    else:
        est = _as_float(job.get("est_cost_usd"))
        estimated = not (est > 0 and provenance == "provider")
    return provenance, estimated


def project_local_job_for_swarm_live(job: dict) -> dict:
    """Project one persisted/in-memory local job into a store-shaped live row."""
    if not isinstance(job, dict):
        return {}
    jid = str(job.get("id") or "").strip()
    if not jid:
        return {}

    status = str(job.get("status") or "")
    terminal = status.strip().lower() in _TERMINAL_STATUSES
    cost_provenance, estimated = _cost_fields(job)
    routing_saved, routing_basis, routing_tokens, routing_counted = _routing_fields(job)

    row: dict[str, Any] = {
        "id": jid,
        "goal": str(job.get("goal") or ""),
        "status": status,
        "role": str(job.get("role") or ""),
        "adapter": str(job.get("adapter") or ""),
        "model": str(job.get("model") or ""),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "task_count": _as_int(job.get("task_count")),
        "tokens": _as_int(job.get("tokens")),
        "est_cost_usd": round(_as_float(job.get("est_cost_usd")), 6),
        "cost_provenance": cost_provenance,
        "estimated": estimated,
        "tokens_in": _as_int(job.get("tokens_in")),
        "tokens_cached": _as_int(job.get("tokens_cached")),
        "routing_saved_usd": routing_saved,
        "routing_savings_basis": routing_basis,
        "routing_tokens_compared": routing_tokens,
        "routing_savings_counted": routing_counted,
        "delegation_saved_usd": round(_as_float(job.get("delegation_saved_usd")), 6),
        "delegation_savings_basis": str(
            job.get("delegation_savings_basis") or "unknown"
        ),
        "delegation_tokens_compared": _as_int(job.get("delegation_tokens_compared")),
        "delegation_savings_counted": bool(job.get("delegation_savings_counted")),
        "cache_saved_usd": round(_as_float(job.get("cache_saved_usd")), 6),
        "swarm_cache_savings_basis": str(
            job.get("swarm_cache_savings_basis") or "unknown"
        ),
        "swarm_cache_unpriced_tokens": _as_int(
            job.get("swarm_cache_unpriced_tokens")
        ),
        "artifacts": _normalize_artifacts(job.get("artifacts")),
        "artifacts_complete": _artifacts_complete(job.get("artifacts")),
        "tasks": _normalize_tasks(job.get("tasks"), terminal=terminal, job=job),
        "source": str(job.get("source") or "harness"),
        "actions": _normalize_actions(job.get("actions")),
    }
    if isinstance(job.get("terminal_receipt"), dict):
        row["terminal_receipt"] = dict(job["terminal_receipt"])
    if isinstance(job.get("financial_receipt"), dict):
        row["financial_receipt"] = dict(job["financial_receipt"])
    if job.get("route_forecast_usd") is not None:
        row["route_forecast_usd"] = job.get("route_forecast_usd")

    # Preserve pre-merge accounting from annotate_job_accounting; stamp harness
    # locals when absent so /api/swarm/live session savings count them once.
    if "accounting_owned" in job:
        row["accounting_owned"] = bool(job.get("accounting_owned"))
    else:
        row["accounting_owned"] = True
    if job.get("accounting_scope"):
        row["accounting_scope"] = str(job.get("accounting_scope"))
    elif row["accounting_owned"]:
        row["accounting_scope"] = ACCOUNTING_SCOPE_MARIONETTE

    # Scoping metadata — not on store rows but harmless for merge/debug.
    for key in ("session_id", "cwd", "label"):
        if job.get(key) is not None:
            row[key] = job.get(key)

    # Background run_command / command-batch jobs: project fingerprint/receipt
    # fields without reclassifying them as provider-swarm workers.
    try:
        from harness.command_jobs import project_command_job_fields
        from harness.command_batches import project_command_batch_fields

        batch_fields = project_command_batch_fields(job)
        if batch_fields:
            row.update(batch_fields)
            row["adapter"] = str(job.get("adapter") or "command_batch")
            row["role"] = str(job.get("role") or "command_batch")
        else:
            cmd_fields = project_command_job_fields(job)
            if cmd_fields:
                row.update(cmd_fields)
                # Keep adapter/role as command — never rewrite to agentic/native.
                row["adapter"] = str(job.get("adapter") or "command")
                row["role"] = str(job.get("role") or "command")
                if job.get("batch_id"):
                    row["batch_id"] = str(job.get("batch_id"))
                if job.get("batch_index") is not None:
                    row["batch_index"] = int(job.get("batch_index"))
    except Exception:
        pass

    # Optional validation-reuse provenance (back-compat: omit when absent).
    try:
        from harness.validation_reuse import provenance_fields_from_job
        row.update(provenance_fields_from_job(job))
    except Exception:
        for key in (
            "reuse_status",
            "source_job_id",
            "validation_fingerprint",
            "invalidated_paths",
            "reuse_reason",
        ):
            if job.get(key) not in (None, "", [], {}):
                row[key] = job.get(key)

    from harness.financial_receipt import project_historical_one_worker_spend
    return project_historical_one_worker_spend(row)


def merge_local_jobs_into_swarm_live(
    store_jobs: Iterable[dict],
    local_jobs: Iterable[dict],
) -> list[dict]:
    """Merge local rows while durable store identities remain authoritative."""
    host_local_ids = {
        str(job.get("id") or "").strip()
        for job in (local_jobs or [])
        if isinstance(job, dict)
        and str(job.get("id") or "").startswith("local-")
        and str(job.get("id") or "").strip()
    }
    # Exact ownership only: a store/cli row stamped dispatch_id=<local-*>
    # is the host implement's internal Orchestrator job, not a second card.
    # No goal / time / origin / session heuristic.
    out = []
    for job in store_jobs or []:
        if not isinstance(job, dict):
            continue
        dispatch_id = str(job.get("dispatch_id") or "").strip()
        if dispatch_id and dispatch_id in host_local_ids:
            continue
        out.append(job)
    existing_ids = {j.get("id") for j in out if j.get("id")}
    replaced_local_ids = {
        f"local-swarm-{str(job.get('dispatch_id') or '').strip()}"
        for job in out
        if isinstance(job, dict)
        and str(job.get("id") or "").startswith("job_")
        and str(job.get("dispatch_id") or "").strip()
    }
    for job in local_jobs or []:
        jid = job.get("id") if isinstance(job, dict) else None
        if not jid or jid in existing_ids or jid in replaced_local_ids:
            continue
        out.append(project_local_job_for_swarm_live(job))
        existing_ids.add(jid)
    return out


def local_job_is_running(status: Any) -> bool:
    return str(status or "").strip().lower() in _RUNNING_STATUSES
