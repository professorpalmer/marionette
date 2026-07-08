from __future__ import annotations

"""DurableState: a clean read layer over Puppetmaster's SwarmStore. This is the
data the GUI renders -- jobs, artifacts, and the live event stream. Read-only;
the Session does the writing by driving the Orchestrator.
"""

from typing import Any, Optional

from puppetmaster.store_factory import create_store


def _normalize_rejected(rejected: Any) -> Optional[list]:
    """Router rejected-alternatives entries are {"id", "reason"}; the GUI renders
    {"model", "reason"}. Map "id" -> "model" so the model name shows. Tolerant of
    already-normalized entries and non-list inputs."""
    if not isinstance(rejected, list):
        return rejected
    out = []
    for r in rejected:
        if isinstance(r, dict):
            out.append({
                "model": r.get("model") or r.get("id") or "",
                "reason": r.get("reason") or "",
            })
        else:
            out.append({"model": str(r), "reason": ""})
    return out


def _diffstat(unified_diff: str) -> Optional[dict]:
    """Parse a unified diff into {files, insertions, deletions}. Best-effort and
    stdlib-only: counts +/- body lines (ignoring the +++/--- file headers) and
    the number of distinct files touched. Returns None on empty/invalid input so
    callers can test truthiness. Never raises."""
    if not unified_diff or not isinstance(unified_diff, str):
        return None
    try:
        files = 0
        insertions = 0
        deletions = 0
        for line in unified_diff.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("diff --git ") or (line.startswith("--- ") is False and line[:6] == "diff -"):
                files += 1
                continue
            if line.startswith("+"):
                insertions += 1
            elif line.startswith("-"):
                deletions += 1
        # Fall back to counting "--- " header pairs when there are no
        # "diff --git" markers (e.g. plain `diff -u` or `git diff` without
        # the extended header).
        if files == 0:
            files = sum(1 for ln in unified_diff.splitlines() if ln.startswith("--- "))
        if files == 0 and (insertions or deletions):
            files = 1
        if not (files or insertions or deletions):
            return None
        return {"files": files, "insertions": insertions, "deletions": deletions}
    except Exception:
        return None


class DurableState:
    def __init__(self, state_dir: str, backend: str = "sqlite") -> None:
        self.state_dir = state_dir
        self.store = create_store(backend, state_dir)

    def list_jobs(self) -> list:
        jobs = self.store.list_jobs()
        jids = [j.id for j in jobs]
        # Batch the per-job lookups instead of one query per job (the old N+1:
        # count_artifacts + list_tasks per job, scaling with history size).
        # Tasks: one bulk read regrouped by job_id. Artifact counts: one bulk
        # count when the store supports it, else fall back to per-job counts.
        tasks_by_job: dict = {}
        try:
            all_tasks = self.store.list_tasks_for_jobs(jids)
            for t in all_tasks:
                tasks_by_job.setdefault(getattr(t, "job_id", None), []).append(t)
        except Exception:
            tasks_by_job = None  # signal per-job fallback below
        counts_by_job: dict = {}
        try:
            if hasattr(self.store, "count_artifacts_for_jobs"):
                counts_by_job = self.store.count_artifacts_for_jobs(jids)
            else:
                counts_by_job = None
        except Exception:
            counts_by_job = None

        out = []
        for j in jobs:
            if counts_by_job is not None:
                arts = counts_by_job.get(j.id, 0)
            else:
                arts = self.store.count_artifacts(j.id)
            role = ""
            adapter = ""
            task_count = 0
            try:
                if tasks_by_job is not None:
                    tasks = tasks_by_job.get(j.id, [])
                else:
                    tasks = self.store.list_tasks(j.id)
                task_count = len(tasks)
                if tasks:
                    for t in tasks:
                        if getattr(t, "role", ""):
                            role = t.role
                            break
                    if not role:
                        role = getattr(tasks[0], "role", "")
                    for t in tasks:
                        if getattr(t, "adapter", ""):
                            adapter = t.adapter
                            break
                    if not adapter:
                        adapter = getattr(tasks[0], "adapter", "")
            except Exception:
                pass
            role = getattr(j, "role", None) or role
            adapter = getattr(j, "adapter", None) or adapter
            out.append({
                "id": j.id,
                "goal": getattr(j, "goal", ""),
                "status": str(getattr(j, "status", "")),
                "artifacts": arts,
                "created_at": getattr(j, "created_at", None),
                "role": role,
                "adapter": adapter,
                "task_count": task_count,
                "label": getattr(j, "label", None),
            })
        return out

    def format_artifacts(self, artifacts: list) -> list:
        """Format already-loaded artifact objects for the GUI. Split out of
        job_artifacts so callers that already hold a (batched) artifact list can
        format them without a second per-job store read."""
        out = []
        for a in artifacts:
            payload = getattr(a, "payload", {}) or {}
            headline = (payload.get("claim") or payload.get("decision")
                        or payload.get("risk") or payload.get("check")
                        or payload.get("summary") or payload.get("change") or "")
            # Patch artifacts carry the concrete edit result (files + unified
            # diff). Surface a compact file list and a parsed diffstat so the
            # GUI can show "3 files, +40 -12" in a job card instead of a lone
            # truncated goal line. Best-effort: never let a malformed patch
            # payload break artifact listing.
            files = None
            diffstat = None
            if str(getattr(a, "type", "")).lower().endswith("patch"):
                try:
                    files = payload.get("files") or []
                    if not isinstance(files, list):
                        files = []
                    diffstat = _diffstat(payload.get("unified_diff") or "")
                    if files and not headline:
                        shown = ", ".join(str(f) for f in files[:4])
                        more = f" +{len(files) - 4} more" if len(files) > 4 else ""
                        headline = f"Patch: {shown}{more}"
                except Exception:
                    files, diffstat = None, None
            out.append({
                "id": getattr(a, "id", ""),
                "type": str(getattr(a, "type", "")),
                "headline": str(headline)[:300],
                "confidence": getattr(a, "confidence", None),
                "created_by": getattr(a, "created_by", ""),
                # Puppetmaster's router stamps the chosen model under "model_id"
                # (to_artifact_payload); the older keys are kept as fallbacks so
                # non-router artifacts still resolve a model when they carry one.
                "model": (payload.get("model_id") or payload.get("model")
                          or payload.get("model_chosen") or payload.get("driver")),
                "est_cost_usd": payload.get("estimated_cost_usd") or payload.get("nominal_cost_usd"),
                "role": payload.get("role") or payload.get("worker_role"),
                # Rejected alternatives arrive as {"id", "reason"}; normalize to the
                # {"model", "reason"} shape the GUI renders so the model name shows
                # instead of "undefined".
                "rejected": _normalize_rejected(payload.get("rejected")),
                "detail": payload.get("reason") or payload.get("detail"),
                # Only present for patch artifacts; None elsewhere so the GUI can
                # cheaply test truthiness before rendering a diffstat row.
                "files": files,
                "diffstat": diffstat,
            })
        return out

    def job_artifacts(self, job_id: str) -> list:
        return self.format_artifacts(self.store.list_artifacts(job_id))

    def events_since(self, job_id: str, cursor: int = 0) -> dict:
        try:
            events = self.store.read_events_since(job_id, cursor)
            new_cursor = self.store.event_cursor(job_id)
        except Exception:
            events, new_cursor = [], cursor
        return {"events": events, "cursor": new_cursor}
