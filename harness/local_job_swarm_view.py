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
})
_RUNNING_STATUSES = frozenset({"running", "in_progress", "pending", "started"})


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


def _normalize_tasks(raw: Any, *, terminal: bool) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for task in raw:
        if not isinstance(task, dict):
            continue
        entry = {
            "id": str(task.get("id") or ""),
            "role": str(task.get("role") or ""),
            "instruction": "" if terminal else str(task.get("instruction") or ""),
            "status": str(task.get("status") or ""),
            "adapter": str(task.get("adapter") or ""),
        }
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
        "tasks": _normalize_tasks(job.get("tasks"), terminal=terminal),
        "source": str(job.get("source") or "harness"),
        "actions": _normalize_actions(job.get("actions")),
    }

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

    return row


def merge_local_jobs_into_swarm_live(
    store_jobs: Iterable[dict],
    local_jobs: Iterable[dict],
) -> list[dict]:
    """Append projected local rows without duplicating store job ids."""
    out = list(store_jobs or [])
    existing_ids = {j.get("id") for j in out if j.get("id")}
    for job in local_jobs or []:
        jid = job.get("id") if isinstance(job, dict) else None
        if not jid or jid in existing_ids:
            continue
        out.append(project_local_job_for_swarm_live(job))
        existing_ids.add(jid)
    return out


def local_job_is_running(status: Any) -> bool:
    return str(status or "").strip().lower() in _RUNNING_STATUSES
