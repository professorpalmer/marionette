from __future__ import annotations

"""DurableState: a clean read layer over Puppetmaster's SwarmStore. This is the
data the GUI renders -- jobs, artifacts, and the live event stream. Read-only;
the Session does the writing by driving the Orchestrator.
"""

import json
import os
import sqlite3
from typing import Any, Optional

from puppetmaster.store_factory import create_store
from .diag import note as _diag

_EVENT_INCLUDE_MODES = frozenset({"lifecycle", "quiet", "all"})


def normalize_event_include(include: Any) -> str:
    """Unknown or empty ``include`` fails closed to lifecycle (drop heartbeats)."""
    mode = str(include or "lifecycle").strip() or "lifecycle"
    if mode not in _EVENT_INCLUDE_MODES:
        return "lifecycle"
    return mode


def read_job_events_since(
    store: Any,
    job_id: str,
    cursor: int = 0,
    include: str = "lifecycle",
) -> list:
    """Read job events with lifecycle-default filtering.

    Prefer ``store.read_lifecycle_events`` when present. Otherwise filter
    ``read_events_since`` via ``puppetmaster.host_lifecycle.filter_events``.
    Missing helpers fail-soft to empty (except ``include=all``, which keeps
    the unfiltered ``read_events_since`` list).
    """
    if store is None or not job_id:
        return []
    mode = normalize_event_include(include)
    try:
        since = int(cursor or 0)
    except (TypeError, ValueError):
        since = 0

    reader = getattr(store, "read_lifecycle_events", None)
    if callable(reader):
        try:
            return list(reader(job_id, since=since, include=mode) or [])
        except TypeError:
            try:
                return list(reader(job_id, since, mode) or [])
            except Exception:
                pass
        except Exception:
            pass

    try:
        raw = store.read_events_since(job_id, since)
    except Exception:
        raw = None
    if raw is None:
        try:
            raw = store.read_events(job_id) if hasattr(store, "read_events") else []
        except Exception:
            raw = []
        if since and isinstance(raw, list):
            raw = [
                rec for rec in raw
                if int(rec.get("id") or 0) > since
            ] if raw and isinstance(raw[0], dict) else raw

    filter_events = None
    try:
        from puppetmaster.host_lifecycle import filter_events as _filter_events
        filter_events = _filter_events
    except Exception:
        filter_events = None

    if filter_events is not None:
        try:
            return list(filter_events(raw or [], include=mode))
        except Exception:
            pass
    if mode == "all":
        return list(raw or [])
    return []


def _normalize_rejected(
    rejected: Any,
    *,
    selected_model: Any = None,
) -> Optional[list]:
    """Router rejected-alternatives entries are {"id", "reason"}; the GUI renders
    {"model", "reason"}. Map "id" -> "model" so the model name shows. Tolerant of
    already-normalized entries and non-list inputs.

    When ``selected_model`` is provided, drop identity-equal rows so the final
    ROUTING card cannot list its winner among rejected candidates.
    """
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
    selected = str(selected_model or "").strip()
    if selected:
        try:
            from harness.model_identity import filter_rejected_excluding_selected
            return filter_rejected_excluding_selected(out, selected)
        except Exception:
            pass
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
        try:
            jobs = self.store.list_jobs()
        except Exception as e:
            # One poisoned row (e.g. a status string this puppetmaster build
            # doesn't know) must not blank the whole jobs feed -- that is how
            # the swarm tracker went permanently empty. Degrade to a raw
            # row-tolerant read that skips only the bad rows.
            _diag("state.list_jobs_fallback", e)
            return self._list_jobs_raw_tolerant()
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
            elif hasattr(self.store, "list_artifacts_for_jobs"):
                counts_by_job = {jid: 0 for jid in jids}
                for artifact in self.store.list_artifacts_for_jobs(jids):
                    job_id = getattr(artifact, "job_id", None)
                    if job_id in counts_by_job:
                        counts_by_job[job_id] += 1
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

    def _list_jobs_raw_tolerant(self) -> list:
        """Read the sqlite jobs table directly, skipping rows the installed
        puppetmaster cannot deserialize. Returns the same dict shape as
        list_jobs, minus task/artifact enrichment (the GUI tolerates zeros)."""
        db_path = os.path.join(self.state_dir, "state.sqlite3")
        if not os.path.exists(db_path):
            return []
        out = []
        try:
            uri = "file:" + db_path.replace(os.sep, "/") + "?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                rows = con.execute("SELECT id, data FROM jobs").fetchall()
            finally:
                con.close()
        except Exception as e:
            _diag("state.list_jobs_raw", e)
            return []
        for job_id, data in rows:
            try:
                d = json.loads(data)
                out.append({
                    "id": job_id,
                    "goal": d.get("goal", ""),
                    "status": str(d.get("status", "")),
                    "artifacts": 0,
                    "created_at": d.get("created_at"),
                    "role": "",
                    "adapter": "",
                    "task_count": 0,
                    "label": d.get("label"),
                })
            except Exception:
                continue
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
            art_type = str(getattr(a, "type", ""))
            task_id = getattr(a, "task_id", "") or None
            job_id = getattr(a, "job_id", "") or None
            detail = payload.get("reason") or payload.get("detail")
            result = payload.get("result")
            failure = payload.get("failure")
            kind = art_type.strip().lower()
            if kind.endswith("verification") and (
                failure or str(result or "").strip().lower() in ("failed", "blocked", "error")
            ):
                source_reason = (
                    payload.get("turn_failure_message")
                    or payload.get("message")
                    or payload.get("stderr")
                )
                if source_reason:
                    from harness.api.redaction import redact_secret_text
                    detail = redact_secret_text(str(source_reason).strip())[:500] or None
            row = {
                "id": getattr(a, "id", ""),
                "sha256": getattr(a, "sha256", ""),
                "type": art_type,
                "headline": str(headline)[:300],
                "confidence": getattr(a, "confidence", None),
                "created_by": getattr(a, "created_by", ""),
                # Surface task_id so the GUI can group ROUTING duplicates
                # (router + router-fallback per task) into one display row.
                "task_id": task_id,
                # Puppetmaster's router stamps the chosen model under "model_id"
                # (to_artifact_payload); the older keys are kept as fallbacks so
                # non-router artifacts still resolve a model when they carry one.
                "model": (payload.get("model_id") or payload.get("model")
                          or payload.get("model_chosen") or payload.get("driver")),
                "est_cost_usd": payload.get("estimated_cost_usd") or payload.get("nominal_cost_usd"),
                "role": payload.get("role") or payload.get("worker_role"),
                # Pin/route attribution for the swarm tracker. Without these the
                # GUI cannot fail-closed-display explicit pins vs auto-routes.
                "policy": payload.get("policy"),
                "provider": payload.get("provider"),
                "adapter": payload.get("adapter"),
                # Wire/provider slug when distinct from registry model_id
                # (e.g. model_id=agentic/meta/muse-spark-1.1,
                # adapter_model_name=meta/muse-spark-1.1).
                "adapter_model_name": payload.get("adapter_model_name"),
                # Rejected alternatives arrive as {"id", "reason"}; normalize to the
                # {"model", "reason"} shape the GUI renders so the model name shows
                # instead of "undefined". Filter out the selected model under
                # identity equality so the ledger cannot contradict the winner.
                "rejected": _normalize_rejected(
                    payload.get("rejected"),
                    selected_model=(
                        payload.get("model_id") or payload.get("model")
                        or payload.get("model_chosen") or payload.get("driver")
                    ),
                ),
                "detail": detail,
                # Verification verdicts. "result" is failed/blocked/pass;
                # "failure" is the machine class (no_model, billing_or_quota).
                # The GUI needs these to render a swarm whose every worker
                # fast-failed as a red failed run instead of a green "done".
                "result": result,
                "failure": failure,
                # Only present for patch artifacts; None elsewhere so the GUI can
                # cheaply test truthiness before rendering a diffstat row.
                "files": files,
                "diffstat": diffstat,
            }
            # Lightweight parent-execution pointer for signal rows. Prefer an
            # explicit payload stamp; otherwise synthesize from job/task ids so
            # legacy store rows still resolve without copying spend fields.
            if kind in ("finding", "risk", "decision"):
                ref = payload.get("execution_ref")
                if not isinstance(ref, dict):
                    ref = {}
                if job_id or ref.get("job_id"):
                    from harness.local_job_artifacts import execution_ref_for
                    row["execution_ref"] = execution_ref_for(
                        str(ref.get("job_id") or job_id or ""),
                        task_id=ref.get("task_id") or task_id,
                        terminal_artifact_id=ref.get("terminal_artifact_id"),
                    )
            out.append(row)
        return out

    def job_artifacts(self, job_id: str) -> list:
        return self.format_artifacts(self.store.list_artifacts(job_id))

    def events_since(
        self,
        job_id: str,
        cursor: int = 0,
        include: str = "lifecycle",
    ) -> dict:
        """Job event poll. Default ``include=lifecycle`` drops per-turn heartbeats."""
        try:
            events = read_job_events_since(
                self.store, job_id, cursor=cursor, include=include,
            )
            new_cursor = self.store.event_cursor(job_id)
        except Exception:
            events, new_cursor = [], cursor
        return {"events": events, "cursor": new_cursor}


# Brief / tracker name for the job-event read facade.
JobStore = DurableState
