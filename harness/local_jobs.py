from __future__ import annotations

"""Local-jobs mixin: register/finish/persist/cancel helpers for in-process workers.

Extracted mechanically from harness/conversation.py to continue decomposing the
ConversationalSession god-object, matching ToolDispatchMixin / PromptQueueMixin
contract: these methods operate through `self` (``_local_jobs``,
``_local_jobs_lock``, ``_local_job_cancels``, ``_local_jobs_path``, ``config``,
``harness_session_id``) provided by the concrete class -- the mixin defines no
state and no __init__.

``drain_swarm_results`` / ``_await_and_apply_job`` /
``_run_provider_worker_background`` live on ConversationJobsMixin. Busy
lifecycle stays on BusyControlMixin; swarm submit stays on SendLoopMixin.
Session-level ``cancel`` stays on ConversationalSession; ``interrupt`` on
BusyControlMixin. This mixin owns only per-job local-job bookkeeping.

Method Resolution Order keeps behavior identical: ``_register_local_job``,
``live_local_jobs``, ``cancel_local_job``, etc. still resolve via inheritance.
"""

import copy
import os
import threading
from typing import Any, Iterable, Optional

from .job_actions import (
    MAX_JOB_ACTIONS,
    ingest_worker_events,
    sanitize_actions_list,
    sanitize_worker_event,
    settle_running_actions,
    snapshot_actions,
    upsert_action_row,
)
from .model_identity import (
    collapse_engine_prefixes,
    envelope_model_id,
    filter_rejected_excluding_selected,
    is_engine_only_model_id,
    model_ids_equal,
    price_lookup_id,
)
from .provenance_sanitize import sanitize_clean_tree_claims

# Job statuses that must never accept a fresh status=running nested row.
# Includes command-job terminals (timeout/truncated) from Wave 2 durability.
_TERMINAL_LOCAL_JOB_STATUSES = frozenset({
    "completed", "failed", "cancelled", "timeout", "truncated",
})


def _reconcile_routing_artifact(art: dict, selected_model: str) -> dict:
    """Make a final ROUTING card agree with the realized selected model.

    Rewrites model/headline, drops the selected id from rejected[], and clears
    stale winner/rejection prose when the finish-time model differs from the
    preflight preview pick.
    """
    updated = dict(art)
    selected = collapse_engine_prefixes(selected_model) or (selected_model or "").strip()
    if not selected:
        return updated
    preview = str(updated.get("model") or "").strip()
    model_changed = bool(preview) and not model_ids_equal(preview, selected)
    updated["model"] = selected
    updated["headline"] = f"Routed to {selected}"
    also = []
    adapter_name = str(updated.get("adapter_model_name") or "").strip()
    if adapter_name:
        also.append(adapter_name)
    updated["rejected"] = filter_rejected_excluding_selected(
        updated.get("rejected"), selected, also_exclude=also,
    )
    if model_changed:
        # Preview winner/rejection prose belongs to the old pick — do not leave
        # "chose flash because cheaper than pro" under a pro-selected card.
        updated["detail"] = ""
        if "reason" in updated:
            updated["reason"] = ""
    return updated


class LocalJobsMixin:
    """Mixin holding in-process local-job register/finish/persist/cancel helpers.

    The concrete class (ConversationalSession) supplies the state these
    methods read/write via `self`. This mixin defines no __init__ and no
    instance state of its own.
    """

    def _register_command_job(
        self,
        job_id: str,
        *,
        command: str,
        action_id: str,
        command_fingerprint: str = "",
        command_preview: str = "",
        cwd: str = "",
        batch_id: str = "",
        batch_index: Optional[int] = None,
    ) -> dict:
        """Persist a durable background ``run_command`` row *before* launch.

        Status starts as ``registered`` (contract durable_job_states). Stamped
        ``accounting_owned=True``, ``accounting_scope='marionette'``,
        ``source='harness'``. Role/adapter are ``command`` so projections never
        misclassify the row as a provider-swarm worker.
        """
        import time
        from harness.command_jobs import (
            COMMAND_JOB_ADAPTER,
            COMMAND_JOB_KIND,
            COMMAND_JOB_ROLE,
            command_fingerprint as _fingerprint,
            secret_free_command_preview,
        )
        from harness.job_scoping import ACCOUNTING_SCOPE_MARIONETTE, job_label_for_session

        effective_cwd = cwd or self.config.repo or ""
        session_id = self.harness_session_id or ""
        fp = (command_fingerprint or "").strip() or _fingerprint(command or "")
        preview = (command_preview or "").strip() or secret_free_command_preview(
            command or ""
        )
        now = time.time()
        with self._local_jobs_lock:
            self._local_job_cancels[job_id] = threading.Event()
            row = {
                "id": job_id,
                "goal": preview,
                "status": "registered",
                "role": COMMAND_JOB_ROLE,
                "adapter": COMMAND_JOB_ADAPTER,
                "model": COMMAND_JOB_ADAPTER,
                "job_kind": COMMAND_JOB_KIND,
                "session_id": session_id,
                "action_id": str(action_id or ""),
                "command_fingerprint": fp,
                "command_preview": preview,
                # Never persist the raw command string (may contain secrets).
                "cwd": effective_cwd,
                "label": job_label_for_session(session_id),
                "created_at": now,
                "started_at": now,
                "updated_at": now,
                "task_count": 1,
                "tokens": 0,
                "est_cost_usd": 0.0,
                "artifacts": [],
                "tasks": [{
                    "id": f"{job_id}-w0",
                    "role": COMMAND_JOB_ROLE,
                    "instruction": preview,
                    "status": "registered",
                    "adapter": COMMAND_JOB_ADAPTER,
                }],
                "actions": [],
                "source": "harness",
                "accounting_owned": True,
                "accounting_scope": ACCOUNTING_SCOPE_MARIONETTE,
                "terminal_receipt": None,
                "output": "",
                "output_preview": "",
                "output_chars": 0,
            }
            if batch_id:
                row["batch_id"] = str(batch_id)
            if batch_index is not None:
                row["batch_index"] = int(batch_index)
            self._local_jobs[job_id] = row
            self._persist_local_jobs_locked()
            return copy.deepcopy(row)

    def _register_command_batch_job(
        self,
        batch_id: str,
        *,
        action_id: str,
        children: list,
        child_job_ids: list,
        cwd: str = "",
        max_concurrency: int = 1,
    ) -> dict:
        """Persist an aggregate command-batch row that references durable children.

        The aggregate never owns child stdout/exit truth — children keep their
        own terminal receipts. Role/adapter are ``command_batch`` so this is
        never projected as a provider-swarm worker.
        """
        import time
        from harness.command_batches import (
            COMMAND_BATCH_ADAPTER,
            COMMAND_BATCH_KIND,
            COMMAND_BATCH_ROLE,
        )
        from harness.job_scoping import ACCOUNTING_SCOPE_MARIONETTE, job_label_for_session

        effective_cwd = cwd or self.config.repo or ""
        session_id = self.harness_session_id or ""
        child_meta = [dict(c) for c in (children or []) if isinstance(c, dict)]
        ids = [str(x) for x in (child_job_ids or []) if str(x)]
        now = time.time()
        preview = f"command batch ({len(ids)} commands)"
        with self._local_jobs_lock:
            self._local_job_cancels[batch_id] = threading.Event()
            row = {
                "id": batch_id,
                "goal": preview,
                "status": "registered",
                "role": COMMAND_BATCH_ROLE,
                "adapter": COMMAND_BATCH_ADAPTER,
                "model": COMMAND_BATCH_ADAPTER,
                "job_kind": COMMAND_BATCH_KIND,
                "session_id": session_id,
                "action_id": str(action_id or ""),
                "cwd": effective_cwd,
                "label": job_label_for_session(session_id),
                "created_at": now,
                "started_at": now,
                "updated_at": now,
                "task_count": len(ids),
                "tokens": 0,
                "est_cost_usd": 0.0,
                "artifacts": [],
                "tasks": [{
                    "id": f"{batch_id}-w0",
                    "role": COMMAND_BATCH_ROLE,
                    "instruction": preview,
                    "status": "registered",
                    "adapter": COMMAND_BATCH_ADAPTER,
                }],
                "actions": [],
                "source": "harness",
                "accounting_owned": True,
                "accounting_scope": ACCOUNTING_SCOPE_MARIONETTE,
                "terminal_receipt": None,
                "children": child_meta,
                "child_job_ids": ids,
                "child_count": len(ids),
                "max_concurrency": max(1, int(max_concurrency or 1)),
                "mixed_terminal": False,
            }
            self._local_jobs[batch_id] = row
            self._persist_local_jobs_locked()
            return copy.deepcopy(row)

    def _mark_command_batch_running(self, batch_id: str) -> None:
        """Flip aggregate batch status to ``running`` once the supervisor starts."""
        import time
        with self._local_jobs_lock:
            job = self._local_jobs.get(batch_id)
            if not job:
                return
            if job.get("status") in _TERMINAL_LOCAL_JOB_STATUSES:
                return
            job["status"] = "running"
            job["updated_at"] = time.time()
            if job.get("tasks"):
                try:
                    job["tasks"][0]["status"] = "running"
                except Exception:
                    pass
            self._persist_local_jobs_locked()

    def _update_command_batch_children(
        self,
        batch_id: str,
        *,
        children: list,
        child_job_ids: list,
        max_concurrency: Optional[int] = None,
    ) -> None:
        """Replace aggregate child references after an idempotent replay."""
        import time
        with self._local_jobs_lock:
            job = self._local_jobs.get(batch_id)
            if not job:
                return
            job["children"] = [dict(c) for c in (children or []) if isinstance(c, dict)]
            job["child_job_ids"] = [str(x) for x in (child_job_ids or []) if str(x)]
            job["child_count"] = len(job["child_job_ids"])
            job["task_count"] = job["child_count"]
            if max_concurrency is not None:
                job["max_concurrency"] = max(1, int(max_concurrency))
            job["updated_at"] = time.time()
            if job.get("status") in _TERMINAL_LOCAL_JOB_STATUSES:
                # Replay may reopen work; clear aggregate terminal until sync.
                job["status"] = "running"
                job["terminal_receipt"] = None
                job["mixed_terminal"] = False
            self._persist_local_jobs_locked()

    def _sync_command_batch_from_children(
        self,
        batch_id: str,
        *,
        parent_cancelled: bool = False,
    ) -> None:
        """Refresh aggregate status from durable child rows (children own truth)."""
        import time
        from harness.command_jobs import COMMAND_TERMINAL_STATES

        with self._local_jobs_lock:
            job = self._local_jobs.get(batch_id)
            if not job:
                return
            child_ids = [str(x) for x in (job.get("child_job_ids") or [])]
            children_meta = [
                dict(c) for c in (job.get("children") or []) if isinstance(c, dict)
            ]
            by_id = {str(c.get("job_id") or ""): c for c in children_meta}
            statuses: list[str] = []
            for cid in child_ids:
                child = self._local_jobs.get(cid) or {}
                st = str(child.get("status") or "registered")
                statuses.append(st)
                meta = by_id.get(cid)
                if meta is None:
                    meta = {
                        "job_id": cid,
                        "command_fingerprint": str(child.get("command_fingerprint") or ""),
                        "command_preview": str(child.get("command_preview") or ""),
                    }
                    children_meta.append(meta)
                    by_id[cid] = meta
                meta["status"] = st
                if child.get("terminal_receipt") is not None:
                    meta["terminal_receipt"] = copy.deepcopy(child.get("terminal_receipt"))
                if child.get("exit_code") is not None:
                    meta["exit_code"] = child.get("exit_code")
            job["children"] = children_meta
            job["updated_at"] = time.time()

            all_terminal = bool(statuses) and all(
                s in COMMAND_TERMINAL_STATES for s in statuses
            )
            unique_terminal = {s for s in statuses if s in COMMAND_TERMINAL_STATES}
            mixed = all_terminal and len(unique_terminal) > 1
            job["mixed_terminal"] = mixed

            if not statuses:
                aggregate = "failed"
            elif not all_terminal:
                # Children own truth: parent cancel must not mark the aggregate
                # terminal (or write a durable receipt) while any child is still
                # running/registered. Stay running/cancelling-equivalent until
                # every child reaches a terminal command state.
                if any(s == "running" for s in statuses):
                    aggregate = "running"
                else:
                    aggregate = "running" if any(
                        s == "registered" for s in statuses
                    ) else str(job.get("status") or "running")
            else:
                if any(s in ("failed", "timeout", "truncated") for s in statuses):
                    aggregate = "failed"
                elif any(s == "cancelled" for s in statuses) or parent_cancelled:
                    aggregate = "cancelled"
                else:
                    aggregate = "completed"

            job["status"] = aggregate
            if job.get("tasks"):
                try:
                    job["tasks"][0]["status"] = aggregate
                except Exception:
                    pass

            counts: dict[str, int] = {}
            for s in statuses:
                counts[s] = counts.get(s, 0) + 1
            summary_parts = [f"{n} {name}" for name, n in sorted(counts.items())]
            summary = (
                f"batch {aggregate}: " + ", ".join(summary_parts)
                if summary_parts
                else f"batch {aggregate}"
            )
            # Durable terminal receipt only after every child is terminal.
            if all_terminal:
                job["terminal_receipt"] = {
                    "status": aggregate,
                    "summary": summary,
                    "finished_at": job["updated_at"],
                    "child_statuses": dict(counts),
                    "mixed_terminal": mixed,
                    "child_job_ids": list(child_ids),
                }
                job["artifacts"] = [{
                    "type": "command_batch",
                    "headline": summary[:240],
                }]
            self._persist_local_jobs_locked()

    def _checkpoint_command_job_launch(self, job_id: str) -> bool:
        """Persist a launch checkpoint *before* the child process can start.

        Returns True when launch may proceed. False when the job is missing,
        already terminal, or already has a terminal receipt (late/duplicate
        launch must not reopen settled work). Idempotent: a second call on a
        non-terminal row keeps the original checkpoint and still returns True.
        """
        import time

        with self._local_jobs_lock:
            job = self._local_jobs.get(job_id)
            if not job:
                return False
            if job.get("status") in _TERMINAL_LOCAL_JOB_STATUSES:
                return False
            if job.get("terminal_receipt") is not None:
                return False
            if isinstance(job.get("launch_checkpoint"), dict):
                return True
            now = time.time()
            job["launch_checkpoint"] = {
                "at": now,
                "phase": "pre_launch",
                "session_id": str(job.get("session_id") or ""),
                "action_id": str(job.get("action_id") or ""),
                "command_fingerprint": str(job.get("command_fingerprint") or ""),
                "batch_id": str(job.get("batch_id") or ""),
            }
            job["updated_at"] = now
            self._persist_local_jobs_locked()
            return True

    def _mark_command_job_running(self, job_id: str) -> None:
        """Flip a registered command job to ``running`` once the process starts."""
        import time
        with self._local_jobs_lock:
            job = self._local_jobs.get(job_id)
            if not job:
                return
            if job.get("status") in _TERMINAL_LOCAL_JOB_STATUSES:
                return
            if job.get("terminal_receipt") is not None:
                return
            now = time.time()
            job["status"] = "running"
            job["updated_at"] = now
            prior_ckpt = job.get("launch_checkpoint")
            if isinstance(prior_ckpt, dict):
                ckpt = dict(prior_ckpt)
                ckpt["phase"] = "running"
                ckpt["running_at"] = now
                job["launch_checkpoint"] = ckpt
            else:
                # Launch should have checkpointed first; stamp a recovery fact
                # so restart healing still knows the child had begun.
                job["launch_checkpoint"] = {
                    "at": now,
                    "phase": "running",
                    "running_at": now,
                    "session_id": str(job.get("session_id") or ""),
                    "action_id": str(job.get("action_id") or ""),
                    "command_fingerprint": str(job.get("command_fingerprint") or ""),
                    "batch_id": str(job.get("batch_id") or ""),
                    "inferred": True,
                }
            if job.get("tasks"):
                try:
                    job["tasks"][0]["status"] = "running"
                except Exception:
                    pass
            self._persist_local_jobs_locked()

    def _finish_command_job(
        self,
        job_id: str,
        *,
        status: str,
        summary: str = "",
        exit_code: int = -1,
        output: str = "",
        spill_uri: str = "",
        spill_path: str = "",
        output_chars: int = 0,
        output_preview: str = "",
        run_status: str = "",
    ) -> bool:
        """Persist exactly one terminal command-job receipt (first write wins).

        Duplicate terminal callbacks and late worker results must not reopen or
        overwrite a settled child. Returns True when this call recorded the
        receipt; False when the job was missing or already terminal.
        """
        import time
        from harness.command_jobs import COMMAND_TERMINAL_STATES
        from harness.local_job_artifacts import (
            ERROR_ARTIFACT_TYPE,
            ANALYSIS_ARTIFACT_TYPE,
            stamp_provenance,
            terminal_artifact_id,
        )

        terminal = str(status or "failed").strip().lower()
        if terminal not in COMMAND_TERMINAL_STATES:
            terminal = "failed" if terminal != "ok" else "completed"
        if terminal == "ok":
            terminal = "completed"

        with self._local_jobs_lock:
            job = self._local_jobs.get(job_id)
            if not job:
                return False
            # First durable terminal receipt wins — never overwrite or reopen.
            if job.get("terminal_receipt") is not None:
                return False
            if job.get("status") in _TERMINAL_LOCAL_JOB_STATUSES:
                return False
            # Do not reopen a user-cancelled row as a later timeout/etc.
            if job.get("status") == "cancelled" and terminal != "cancelled":
                terminal = "cancelled"
            job["status"] = terminal
            job["updated_at"] = time.time()
            job["exit_code"] = int(exit_code)
            job["run_status"] = str(run_status or terminal)
            inline = output if isinstance(output, str) else str(output or "")
            preview = (output_preview or "").strip() or inline[:4096]
            job["output"] = inline if not spill_uri else ""
            job["output_preview"] = preview
            job["output_chars"] = int(output_chars or len(inline))
            if spill_uri:
                job["spill_uri"] = spill_uri
            if spill_path:
                job["spill_path"] = spill_path
            if job.get("tasks"):
                try:
                    job["tasks"][0]["status"] = terminal
                except Exception:
                    pass
            headline = (summary or f"Command {terminal}").strip().splitlines()[0][:240]
            art_type = (
                ANALYSIS_ARTIFACT_TYPE
                if terminal == "completed"
                else ERROR_ARTIFACT_TYPE
            )
            terminal_art = stamp_provenance(
                {
                    "id": terminal_artifact_id(job_id),
                    "type": art_type,
                    "headline": headline,
                },
                {
                    "adapter": job.get("adapter"),
                    "model": job.get("model"),
                    "result": terminal,
                },
            )
            job["artifacts"] = [terminal_art]
            job["terminal_receipt"] = {
                "status": terminal,
                "run_status": str(run_status or terminal),
                "exit_code": int(exit_code),
                "summary": headline,
                "finished_at": job["updated_at"],
                "output_chars": job["output_chars"],
                "spill_uri": spill_uri or "",
                "output_spilled": bool(spill_uri),
            }
            parent_batch_id = str(job.get("batch_id") or "")
            self._persist_local_jobs_locked()
        # Refresh aggregate after releasing the lock (children own truth).
        if parent_batch_id:
            try:
                self._sync_command_batch_from_children(parent_batch_id)
            except Exception:
                pass
        return True

    def get_local_job(self, job_id: str) -> Optional[dict]:
        """Deep-copy one local job by id (restart-safe after ``_load_local_jobs``)."""
        if not job_id:
            return None
        with self._local_jobs_lock:
            job = self._local_jobs.get(job_id)
            if not isinstance(job, dict):
                return None
            snap = copy.deepcopy(job)
            snap["actions"] = snapshot_actions(snap.get("actions"))
            return snap

    def _register_local_job(self, job_id: str, goal: str, role: str = "implement",
                            cwd: str = "", engine: str = "", model: str = "",
                            *, skip_routing_preview: bool = False) -> None:
        """Record a dispatched in-process edit worker so it appears in the swarm
        panel while it runs (the panel otherwise only sees Puppetmaster store
        jobs). Shaped like a store job: a single synthesized worker task carries
        the live status the UI renders.

        ``engine`` is ``agentic`` or ``native`` (never the pilot provider slug).
        When known, ``model`` is the routed/driver model id; the panel shows
        ``{engine}/{model}``. Task role is ``{role} ({engine})`` -- never
        ``provider worker``.

        For agentic jobs with no model yet, dry-run the router and stamp a
        ROUTING artifact + estimate so the tracker shows model/cost mid-flight
        instead of a bare ``agentic`` badge. Zero-work full reuse passes
        ``skip_routing_preview=True`` so preview economics are never stamped
        or exported for a job that performs no adapter work.
        """
        import time
        from harness.job_scoping import job_label_for_session

        effective_cwd = cwd or self.config.repo or ""
        session_id = self.harness_session_id or ""
        engine_label = (engine or "").strip().lower()
        if engine_label not in ("agentic", "native"):
            # Callers that have not yet picked an engine get native semantics
            # (Marionette pilot / ProviderWorker) without stamping the openrouter
            # pilot slug as the adapter -- that lied when the run was agentic.
            engine_label = "native"
        model_id = collapse_engine_prefixes((model or "").strip()) or (model or "").strip()
        if not model_id and engine_label == "native":
            model_id = (self.config.driver or "").strip()
        routing_arts: list = []
        est_cost = 0.0
        routing_saved = 0.0
        routing_basis = ""
        preview: dict = {}
        if engine_label == "agentic" and not model_id and not skip_routing_preview:
            try:
                from harness.local_job_routing import preview_agentic_route
                preview = preview_agentic_route(goal, role=role or "implement")
            except Exception:
                preview = {}
            model_id = collapse_engine_prefixes(
                (preview.get("model_id") or "").strip()
            ) or (preview.get("model_id") or "").strip()
            est_cost = float(preview.get("est_cost_usd") or 0.0)
            routing_saved = float(preview.get("routing_saved_usd") or 0.0)
            routing_basis = str(preview.get("routing_savings_basis") or "")
            art = preview.get("artifact")
            if isinstance(art, dict):
                from .local_job_artifacts import routing_artifact_id

                # Explicit id at creation: artifact://local-*/<id> must survive
                # the headline/model rewrite _finish_local_job performs later.
                routing_arts.append({**art, "id": routing_artifact_id(job_id)})
        # Never stamp bare agentic/native as job.model — that reads as a chosen
        # model in the Swarm Tracker. Leave empty until a real id is known.
        if model_id and not is_engine_only_model_id(model_id):
            display_model = envelope_model_id(engine_label, model_id)
        else:
            display_model = ""
            model_id = ""
        task_role = f"{role} ({engine_label})" if role else f"implement ({engine_label})"
        task_row = {
            "id": f"{job_id}-w0",
            "role": task_role,
            "instruction": goal,
            "status": "running",
            "adapter": engine_label,
        }
        if display_model:
            task_row["model"] = display_model
        with self._local_jobs_lock:
            self._local_job_cancels[job_id] = threading.Event()
            now = time.time()
            row = {
                "id": job_id,
                "goal": goal,
                "status": "running",
                "role": role,
                "adapter": engine_label,
                "model": display_model,
                "session_id": session_id,
                "cwd": effective_cwd,
                "label": job_label_for_session(session_id),
                "created_at": now,
                "updated_at": now,
                "task_count": 1,
                "tokens": 0,
                "est_cost_usd": round(est_cost, 6) if est_cost else 0.0,
                "artifacts": list(routing_arts),
                "tasks": [task_row],
                # Bounded nested tool rows (kind/goal/status only). Filled from
                # ProviderWorker action events; never carries stdout/args/env.
                "actions": [],
            }
            # Preflight routing value — estimated only; survives reload so the
            # SwarmPane chip shows the same basis label as a live job.
            if routing_saved > 0:
                row["routing_saved_usd"] = round(routing_saved, 6)
                row["routing_savings_basis"] = routing_basis or "estimated"
            self._local_jobs[job_id] = row
            self._persist_local_jobs_locked()
            if routing_saved > 0:
                try:
                    from harness.observability_export import export_routing_savings

                    export_routing_savings(
                        job_id=job_id,
                        session_id=session_id,
                        routing_saved_usd=routing_saved,
                        routing_savings_basis=routing_basis or "estimated",
                        model_id=model_id,
                        baseline_model_id=str(preview.get("baseline_model_id") or ""),
                        tokens_compared=int(
                            (preview.get("tokens_in") or 0) + (preview.get("tokens_out") or 0)
                        ),
                    )
                except Exception:
                    pass

    def _finish_local_job(self, job_id: str, ok: bool, summary: str = "",
                          files: Optional[list] = None, tokens: int = 0,
                          est_cost_usd: float = 0.0,
                          status: str = "",
                          engine: str = "", model: str = "",
                          findings: Optional[list] = None,
                          diff: str = "",
                          worker_provenance: Optional[dict] = None,
                          reuse_status: str = "",
                          source_job_id: str = "",
                          validation_fingerprint: str = "",
                          invalidated_paths: Optional[list] = None,
                          reuse_reason: str = "",
                          environment_fingerprint: str = "",
                          environment_fingerprint_schema: Optional[int] = None,
                          acceptance_criteria: Optional[list] = None) -> None:
        """Flip a live local job to its terminal state so the panel stops showing
        a spinner and surfaces the outcome (files touched + a one-line summary).

        When ``engine`` / ``model`` are known (from WorkerResult), overwrite the
        provisional register-time labels so an agentic run never keeps a native
        or pilot-slug stamp after it finishes.

        The terminal artifact's TYPE is derived, never assumed: a read-only /
        explore / analysis job (or any job that touched no files and produced no
        diff) settles as an ``analysis`` finding summary, so the sidecar cannot
        claim a ``patch`` that was never written. ``findings`` carries the
        worker's real structured rows through to the artifact read surfaces.
        """
        from .local_job_artifacts import (
            normalize_finding_artifacts,
            routing_artifact_id,
            stamp_provenance,
            terminal_artifact_id,
            terminal_artifact_type,
        )
        import time
        with self._local_jobs_lock:
            job = self._local_jobs.get(job_id)
            if not job:
                return
            # A user-cancelled job settles into a distinct 'cancelled' state so the
            # UI can render it differently from a natural completion/failure.
            cancelled = bool(job.get("status") == "cancelled" or status == "cancelled")
            if cancelled:
                terminal = "cancelled"
            else:
                terminal = "completed" if ok else "failed"
            job["status"] = terminal
            job["updated_at"] = time.time()
            engine_label = (engine or "").strip().lower()
            model_id = collapse_engine_prefixes((model or "").strip()) or (model or "").strip()
            if engine_label in ("agentic", "native"):
                job["adapter"] = engine_label
                if job.get("tasks"):
                    job["tasks"][0]["adapter"] = engine_label
                    base_role = (job.get("role") or "implement").strip() or "implement"
                    job["tasks"][0]["role"] = f"{base_role} ({engine_label})"
            # Never downgrade a real ROUTING/preview model to bare agentic/native.
            # Empty finish-time model promotes from ROUTING artifact or keeps the
            # existing non-engine-only stamp; prefer empty over eng-only honesty.
            if is_engine_only_model_id(model_id):
                promoted = ""
                for art in (job.get("artifacts") or []):
                    if not isinstance(art, dict):
                        continue
                    if (art.get("type") or "").strip().upper() != "ROUTING":
                        continue
                    art_mid = collapse_engine_prefixes(
                        str(art.get("model") or art.get("model_id") or "").strip()
                    ) or str(art.get("model") or art.get("model_id") or "").strip()
                    if art_mid and not is_engine_only_model_id(art_mid):
                        promoted = art_mid
                if not promoted:
                    existing = (job.get("model") or "").strip()
                    if existing and not is_engine_only_model_id(existing):
                        promoted = (
                            collapse_engine_prefixes(existing) or existing
                        )
                model_id = promoted
            if model_id and not is_engine_only_model_id(model_id):
                eng = engine_label if engine_label in ("agentic", "native") else ""
                if not eng:
                    existing_adapter = (job.get("adapter") or "").strip().lower()
                    if existing_adapter in ("agentic", "native"):
                        eng = existing_adapter
                job["model"] = envelope_model_id(eng, model_id)
                if job.get("tasks"):
                    job["tasks"][0]["model"] = job["model"]
            elif is_engine_only_model_id(job.get("model") or ""):
                job["model"] = ""
            # Zero-work full reuse: force durable economics to zero and drop
            # any register-time preview leftovers (defense in depth when
            # skip_routing_preview was not set). Narrow_verify (partial) that
            # actually executes may retain measured costs below.
            zero_work_reuse = (reuse_status or "").strip().lower() == "reused"
            if zero_work_reuse:
                tokens = 0
                est_cost_usd = 0.0
                job["tokens"] = 0
                job["est_cost_usd"] = 0.0
                job.pop("routing_saved_usd", None)
                job.pop("routing_savings_basis", None)
                job.pop("routing_tokens_compared", None)
            if tokens:
                job["tokens"] = tokens
            real_cost = float(est_cost_usd or 0.0)
            cost_unsplit = False
            price_source = "default"
            if not real_cost and tokens and not zero_work_reuse:
                # Provider-worker jobs only carry a combined token total (no
                # in/out split). Price at the output rate so output-heavy runs
                # are not systematically under-priced — and mark estimated so
                # we never imply a fabricated in/out split. Prefer the
                # worker's own model when stamped; else fall back to pilot.
                try:
                    from pmharness.registry import resolve_price_with_source
                    from harness.server import _job_cost, _normalize_price_source
                    price_spec = price_lookup_id(
                        model_id or (job.get("model") or "")
                    ) or self.config.driver
                    price_in, price_out, _src = resolve_price_with_source(price_spec)
                    if price_in is None or price_out is None:
                        # Explicit OpenRouter unknown: fail closed at $0.
                        real_cost = 0.0
                        price_source = "unknown"
                    else:
                        price_source = _normalize_price_source(_src)
                        real_cost = _job_cost(0, 0, tokens, price_in, price_out)
                        cost_unsplit = True
                except Exception:
                    real_cost = 0.0
            if real_cost:
                job["est_cost_usd"] = round(real_cost, 6)
            if cost_unsplit or (tokens and not est_cost_usd) or price_source == "unknown":
                # Unsplit catalog/default/unknown totals are estimates, never receipts.
                job["estimated"] = True
                job["cost_provenance"] = (
                    "static" if price_source == "static"
                    else (
                        "live" if price_source == "live"
                        else ("unknown" if price_source == "unknown" else "default")
                    )
                )
            elif real_cost and est_cost_usd:
                job["estimated"] = False
                job["cost_provenance"] = "provider"
            if job.get("tasks"):
                job["tasks"][0]["status"] = terminal
            if isinstance(worker_provenance, dict):
                job["worker_provenance"] = copy.deepcopy(worker_provenance)
            summary_text = summary or ""
            if isinstance(worker_provenance, dict):
                summary_text = sanitize_clean_tree_claims(
                    summary_text, provenance=worker_provenance,
                )
            if cancelled and not summary_text:
                headline = "Cancelled by user"
            else:
                headline = summary_text.strip().splitlines()[0] if summary_text else (
                    "Patch applied" if ok else "Worker failed")
            if files:
                headline = f"{headline} ({len(files)} file{'s' if len(files) != 1 else ''})"
            # Keep any pre-stamped ROUTING card (immutable preflight estimate).
            # Realized spend stays on job["est_cost_usd"] — never overwrite
            # ROUTING.est_cost_usd with provider receipts. Selected model and
            # rejected[] are reconciled so the final card cannot contradict itself.
            # Zero-work full reuse drops ROUTING cards entirely — no phantom
            # preview spend / routing-savings export surface.
            keep_routing = []
            if not zero_work_reuse:
                for art in (job.get("artifacts") or []):
                    if not isinstance(art, dict):
                        continue
                    if (art.get("type") or "").strip().upper() != "ROUTING":
                        continue
                    updated = dict(art)
                    updated["id"] = routing_artifact_id(job_id)
                    if model_id:
                        updated = _reconcile_routing_artifact(updated, model_id)
                    # Preserve attested policy; default balanced for router stamps.
                    if not (updated.get("policy") or "").strip():
                        if updated.get("created_by") == "router":
                            updated["policy"] = "balanced"
                    keep_routing.append(updated)
            terminal_art: dict = {
                "id": terminal_artifact_id(job_id),
                "type": terminal_artifact_type(
                    ok=ok,
                    cancelled=cancelled,
                    role=job.get("role"),
                    has_file_evidence=bool(files) or bool((diff or "").strip()),
                ),
                "headline": headline[:240],
            }
            if job.get("tasks") and job["tasks"][0].get("id"):
                terminal_art["task_id"] = job["tasks"][0]["id"]
            # Provenance the artifact read surfaces need to attribute the work.
            terminal_art = stamp_provenance(terminal_art, {
                "adapter": job.get("adapter"),
                "model": job.get("model"),
                "result": terminal,
                "tokens": int(job.get("tokens") or 0),
                "est_cost_usd": round(real_cost, 6) if real_cost else 0.0,
                "cost_provenance": job.get("cost_provenance"),
                "worker_provenance": copy.deepcopy(job.get("worker_provenance") or {}),
            })
            signal_findings = findings
            if isinstance(worker_provenance, dict) and isinstance(findings, list):
                signal_findings = []
                for row in findings:
                    if not isinstance(row, dict):
                        continue
                    cleaned = dict(row)
                    for field in ("headline", "claim", "body", "detail"):
                        if field in cleaned and isinstance(cleaned[field], str):
                            cleaned[field] = sanitize_clean_tree_claims(
                                cleaned[field], provenance=worker_provenance,
                            )
                    signal_findings.append(cleaned)
            job["artifacts"] = (
                keep_routing
                + [terminal_art]
                + normalize_finding_artifacts(job_id, signal_findings)
            )
            # Stamp source validation fingerprints on terminal analysis /
            # review / explore jobs so later dispatch can reuse green findings.
            try:
                from harness.validation_reuse import (
                    analysis_role_class,
                    mark_validation_stamp_failed,
                    stamp_validation_on_job,
                )
                role_name = str(job.get("role") or "")
                if (
                    ok
                    and not cancelled
                    and analysis_role_class(role_name) == "analysis"
                ):
                    stamp_cwd = str(job.get("cwd") or self.config.repo or "")
                    # Preserve pre-stamped narrow_verify / reuse provenance when
                    # the finish caller does not override (background drain).
                    stamp_status = (
                        (reuse_status or "").strip()
                        or str(job.get("reuse_status") or "").strip()
                        or "fresh"
                    )
                    stamp_source = (
                        (source_job_id or "").strip()
                        or str(job.get("source_job_id") or "").strip()
                    )
                    stamp_fp = (
                        (validation_fingerprint or "").strip()
                        or str(job.get("validation_fingerprint") or "").strip()
                    )
                    stamp_paths = list(
                        invalidated_paths
                        if invalidated_paths is not None
                        else (job.get("invalidated_paths") or [])
                    )
                    stamp_reason = (
                        (reuse_reason or "").strip()
                        or str(job.get("reuse_reason") or "").strip()
                    )
                    stamp_env = (
                        (environment_fingerprint or "").strip()
                        or str(job.get("environment_fingerprint") or "").strip()
                    )
                    stamp_env_schema = environment_fingerprint_schema
                    if stamp_env_schema is None:
                        try:
                            raw_schema = job.get("environment_fingerprint_schema")
                            stamp_env_schema = (
                                int(raw_schema) if raw_schema is not None else None
                            )
                        except (TypeError, ValueError):
                            stamp_env_schema = None
                    stamp_criteria = (
                        list(acceptance_criteria)
                        if acceptance_criteria is not None
                        else None
                    )
                    if stamp_env:
                        job["environment_fingerprint"] = stamp_env
                    if stamp_env_schema is not None:
                        job["environment_fingerprint_schema"] = int(stamp_env_schema)
                    if stamp_criteria is not None:
                        job["acceptance_criteria"] = list(stamp_criteria)
                    try:
                        stamp_validation_on_job(
                            job,
                            cwd=stamp_cwd,
                            reuse_status=stamp_status,
                            source_job_id=stamp_source,
                            invalidated_paths=stamp_paths,
                            reuse_reason=stamp_reason,
                            validation_fingerprint=stamp_fp,
                            environment_fingerprint=stamp_env,
                            acceptance_criteria=stamp_criteria,
                        )
                    except Exception as stamp_exc:
                        # Preserve best-effort finish, but persist
                        # complete=false/error so the job cannot silently
                        # look like a reusable source.
                        try:
                            mark_validation_stamp_failed(job, stamp_exc)
                        except Exception:
                            pass
                elif (
                    reuse_status or source_job_id or reuse_reason
                    or environment_fingerprint or acceptance_criteria is not None
                ):
                    if reuse_status:
                        job["reuse_status"] = reuse_status
                    if source_job_id:
                        job["source_job_id"] = source_job_id
                    if validation_fingerprint:
                        job["validation_fingerprint"] = validation_fingerprint
                    if invalidated_paths:
                        job["invalidated_paths"] = list(invalidated_paths)
                    if reuse_reason:
                        job["reuse_reason"] = reuse_reason
                    if environment_fingerprint:
                        job["environment_fingerprint"] = (
                            environment_fingerprint or ""
                        ).strip()
                    if environment_fingerprint_schema is not None:
                        try:
                            job["environment_fingerprint_schema"] = int(
                                environment_fingerprint_schema
                            )
                        except (TypeError, ValueError):
                            pass
                    if acceptance_criteria is not None:
                        job["acceptance_criteria"] = list(acceptance_criteria)
            except Exception:
                pass
            # Nested UI must not spin forever after the parent job settles.
            settle_reason = (
                "cancelled" if cancelled
                else ("job failed" if not ok else "job finished")
            )
            job["actions"] = settle_running_actions(
                job.get("actions"), reason=settle_reason,
            )
            self._persist_local_jobs_locked()
            export_job = {
                "job_id": job_id,
                "session_id": str(job.get("session_id") or ""),
                "status": terminal,
                "engine": str(job.get("adapter") or engine_label or ""),
                "model": str(job.get("model") or ""),
                "tokens": int(job.get("tokens") or 0),
                "est_cost_usd": float(job.get("est_cost_usd") or 0.0),
                "cost_provenance": str(job.get("cost_provenance") or ""),
                "routing_saved_usd": float(job.get("routing_saved_usd") or 0.0),
                "routing_savings_basis": str(job.get("routing_savings_basis") or ""),
                "summary": headline,
            }
        try:
            from harness.observability_export import export_local_job_terminal

            export_local_job_terminal(**export_job)
        except Exception:
            pass

    def _fail_or_drop_local_job(self, job_id: str, summary: str = "") -> None:
        """Best-effort settle a registered job after finish failure.

        When register succeeded but finish raised, the row would otherwise
        remain ``running`` (live spinner / orphan). Try a failed finish; if
        that also cannot terminalize, remove the row from the local store.
        """
        jid = str(job_id or "").strip()
        if not jid:
            return
        try:
            with self._local_jobs_lock:
                job = self._local_jobs.get(jid)
                if not isinstance(job, dict):
                    return
                if str(job.get("status") or "") in _TERMINAL_LOCAL_JOB_STATUSES:
                    return
        except Exception:
            return
        try:
            self._finish_local_job(
                jid,
                ok=False,
                summary=(summary or "tracker finish failed")[:200],
                status="failed",
            )
        except Exception:
            pass
        try:
            with self._local_jobs_lock:
                job = self._local_jobs.get(jid)
                if isinstance(job, dict) and (
                    str(job.get("status") or "") not in _TERMINAL_LOCAL_JOB_STATUSES
                ):
                    self._local_jobs.pop(jid, None)
                    self._local_job_cancels.pop(jid, None)
                    self._persist_local_jobs_locked()
        except Exception:
            pass

    def _update_cancelled_local_job_provenance(
        self,
        job_id: str,
        worker_provenance: Optional[dict] = None,
        tokens: int = 0,
        est_cost_usd: float = 0.0,
    ) -> None:
        """Persist late worker facts without reopening a cancelled job.

        Cancellation creates the terminal artifact synchronously so the UI can
        stop spinning. A provider thread may still return measured provenance
        and spend afterward; this path enriches that existing terminal record
        while preserving its cancelled status and avoiding a second terminal
        transition.
        """
        import time

        if not isinstance(worker_provenance, dict):
            worker_provenance = {}
        measured_tokens = int(tokens or 0)
        measured_cost = float(est_cost_usd or 0.0)
        with self._local_jobs_lock:
            job = self._local_jobs.get(job_id)
            if not job or job.get("status") != "cancelled":
                return
            if worker_provenance:
                job["worker_provenance"] = copy.deepcopy(worker_provenance)
            if measured_tokens > 0:
                job["tokens"] = measured_tokens
            if measured_cost > 0:
                job["est_cost_usd"] = round(measured_cost, 6)
            job["updated_at"] = time.time()
            for artifact in job.get("artifacts") or []:
                if not isinstance(artifact, dict):
                    continue
                if artifact.get("id") != f"{job_id}-result":
                    continue
                if worker_provenance:
                    artifact["worker_provenance"] = copy.deepcopy(worker_provenance)
                if measured_tokens > 0:
                    artifact["tokens"] = measured_tokens
                if measured_cost > 0:
                    artifact["est_cost_usd"] = round(measured_cost, 6)
                break
            self._persist_local_jobs_locked()

    # Cap persisted history so the on-disk file cannot grow without bound.
    _LOCAL_JOBS_HISTORY_CAP = 200

    def _persist_local_jobs_locked(self) -> None:
        """Atomically mirror the current _local_jobs dict to disk. MUST be called
        while holding self._local_jobs_lock. Writes a .tmp then os.replace so a
        crash mid-write never leaves a half-written (corrupt) file. Best-effort:
        a persistence failure must never break a running worker."""
        import json
        try:
            items = list(self._local_jobs.values())
            # Keep only the most recent N by created_at to bound growth.
            items.sort(key=lambda j: j.get("created_at") or 0.0)
            if len(items) > self._LOCAL_JOBS_HISTORY_CAP:
                items = items[-self._LOCAL_JOBS_HISTORY_CAP:]
            tmp = self._local_jobs_path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"jobs": items}, f)
            os.replace(tmp, self._local_jobs_path)
        except Exception:
            # Persistence is a convenience; never let it take down the session.
            pass

    def _persist_local_jobs(self) -> None:
        """Lock-taking wrapper around _persist_local_jobs_locked for callers that
        do not already hold the lock."""
        with self._local_jobs_lock:
            self._persist_local_jobs_locked()

    def _load_local_jobs(self) -> None:
        """Reload provider-worker history written by a prior process. Tolerates a
        missing or corrupt file by starting empty. Any job still marked 'running'
        is stale -- its thread died with the old process -- so we flip it to
        'cancelled' with an 'Interrupted by backend restart' note instead of
        leaving a permanently-spinning ghost in the panel. Reloaded jobs are kept
        in history but get NO live cancel Event (nothing to cancel).

        Wave 4: command/batch rows with an existing terminal receipt keep that
        durable outcome (never rerun or overwrite solely because the process
        restarted). Unfinished command children heal from launch-checkpoint
        facts into an honest cancelled terminal.
        """
        import json
        try:
            with open(self._local_jobs_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception:
            # Corrupt/unreadable file: start empty rather than crash on restart.
            return
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            return
        with self._local_jobs_lock:
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                jid = job.get("id")
                if not jid:
                    continue
                is_command_job = (
                    job.get("job_kind") == "run_command"
                    or job.get("role") == "command"
                )
                is_command_batch = (
                    job.get("job_kind") == "run_command_batch"
                    or job.get("role") == "command_batch"
                )
                # Durable terminal receipt is authoritative — never reopen.
                prior_receipt = job.get("terminal_receipt")
                receipt_status = (
                    str(prior_receipt.get("status") or "").strip()
                    if isinstance(prior_receipt, dict)
                    else ""
                )
                has_durable_terminal = (
                    (is_command_job or is_command_batch)
                    and receipt_status in _TERMINAL_LOCAL_JOB_STATUSES
                )
                if has_durable_terminal:
                    job["status"] = receipt_status
                    if job.get("tasks"):
                        try:
                            job["tasks"][0]["status"] = receipt_status
                        except Exception:
                            pass
                    # Fall through to action settle; do not heal-overwrite.
                elif job.get("status") in ("running", "registered"):
                    # running *and* registered-but-not-started rows are stale
                    # after process death — heal to cancelled so the panel
                    # never spins. Launch-checkpoint distinguishes "never
                    # started" from "started then lost the process".
                    had_launch = isinstance(job.get("launch_checkpoint"), dict)
                    if is_command_job and not had_launch and job.get("status") == "registered":
                        summary = "Cancelled before launch (backend restart)"
                    else:
                        summary = "Interrupted by backend restart"
                    job["status"] = "cancelled"
                    job["updated_at"] = job.get("updated_at") or job.get("created_at")
                    if job.get("tasks"):
                        try:
                            job["tasks"][0]["status"] = "cancelled"
                        except Exception:
                            pass
                    # Keep ROUTING cards so policy/basis labels match live jobs
                    # after reload (do not wipe attested attribution).
                    keep_routing = [
                        a for a in (job.get("artifacts") or [])
                        if isinstance(a, dict)
                        and (a.get("type") or "").strip().upper() == "ROUTING"
                    ]
                    job["artifacts"] = keep_routing + [{
                        "type": "error",
                        "headline": summary,
                    }]
                    if is_command_job:
                        job["terminal_receipt"] = {
                            "status": "cancelled",
                            "summary": summary,
                            "exit_code": -1,
                            "finished_at": job.get("updated_at"),
                            "recovery": "terminal_after_restart",
                            "had_launch_checkpoint": had_launch,
                        }
                    if is_command_batch:
                        job["terminal_receipt"] = {
                            "status": "cancelled",
                            "summary": summary,
                            "finished_at": job.get("updated_at"),
                            "child_job_ids": list(job.get("child_job_ids") or []),
                            "mixed_terminal": bool(job.get("mixed_terminal")),
                            "recovery": "terminal_after_restart",
                        }
                elif (
                    job.get("status") in _TERMINAL_LOCAL_JOB_STATUSES
                    and (is_command_job or is_command_batch)
                    and not has_durable_terminal
                ):
                    # Terminal status without a receipt — synthesize one so
                    # reattach always sees exactly one durable outcome.
                    job["terminal_receipt"] = {
                        "status": str(job.get("status")),
                        "summary": f"Command {job.get('status')} (rehydrated)",
                        "exit_code": int(job["exit_code"])
                        if job.get("exit_code") is not None
                        else -1,
                        "finished_at": job.get("updated_at") or job.get("created_at"),
                        "recovery": "receipt_synthesized_on_restart",
                    }
                # Re-sanitize persisted actions (drop tampered keys) and settle
                # any nested rows left running when the prior process died.
                job["actions"] = settle_running_actions(
                    sanitize_actions_list(job.get("actions")),
                    reason="interrupted by restart",
                )
                self._local_jobs[jid] = job
            # Rewrite so the healed statuses are the new on-disk baseline.
            self._persist_local_jobs_locked()

    def cancel_local_job(self, job_id: str) -> bool:
        """Cooperatively cancel a running local (provider-worker) job.

        Sets the per-job cancel Event (best-effort: a Python thread cannot be
        force-killed). Provider-swarm workers and *unlaunched* command jobs are
        terminalized immediately. Launch-checkpointed command jobs record the
        cancel request only — the worker owns the single terminal receipt so
        partial stdout from ``run_cancellable`` is preserved (first-terminal-wins
        must not discard it with an empty early cancel).

        Returns True if the job existed and was not already terminal.
        """
        with self._local_jobs_lock:
            job = self._local_jobs.get(job_id)
            if job is None:
                return False
            already_terminal = job.get("status") in _TERMINAL_LOCAL_JOB_STATUSES
            is_command_batch = (
                job.get("job_kind") == "run_command_batch"
                or job.get("role") == "command_batch"
            )
            is_command_job = (
                job.get("job_kind") == "run_command"
                or job.get("role") == "command"
            )
            batch_id = str(job.get("batch_id") or "") if is_command_job else ""
            has_launch_checkpoint = (
                is_command_job
                and isinstance(job.get("launch_checkpoint"), dict)
            )
            ev = self._local_job_cancels.get(job_id)
            if ev is not None:
                ev.set()
            if already_terminal:
                return False
            if is_command_batch:
                # Aggregate cancel is handled below without wiping siblings.
                pass
            elif not is_command_job:
                job["status"] = "cancelled"
        if is_command_batch:
            from harness.command_batches import cancel_command_batch
            return cancel_command_batch(self, job_id)
        if is_command_job:
            if has_launch_checkpoint:
                # Cooperative cancel: leave status non-terminal until the
                # worker returns partial output and finishes once.
                if batch_id:
                    self._sync_command_batch_from_children(batch_id)
                return True
            # Registered without launch checkpoint — honest stop-before-start.
            self._finish_command_job(
                job_id,
                status="cancelled",
                summary="Cancelled by user",
                exit_code=-1,
                output="",
            )
            # Child cancel must not discard sibling truth — only refresh parent.
            if batch_id:
                self._sync_command_batch_from_children(batch_id)
            return True
        # _finish_local_job re-acquires the lock and persists.
        self._finish_local_job(job_id, ok=False, summary="Cancelled by user",
                               status="cancelled")
        return True

    def _local_job_cancelled(self, job_id: str) -> bool:
        """True if a cancel was requested for this job. Checked by the worker at
        its wall-clock boundary (best-effort cooperative cancel)."""
        ev = self._local_job_cancels.get(job_id)
        return bool(ev is not None and ev.is_set())

    def _upsert_local_job_action(self, job_id: str, ev: Any) -> None:
        """Progressively record one sanitized action event on a local job.

        Progressive UI reads ``/api/swarm/live`` — this path must NOT mutate
        ``_display_transcript`` (worker-thread race with send/export).

        Post-terminal callbacks (late on_event after cancel/finish) must not
        reintroduce status=running rows; settle them immediately.
        """
        row = sanitize_worker_event(ev)
        if row is None:
            return
        with self._local_jobs_lock:
            job = self._local_jobs.get(job_id)
            if not job:
                return
            parent_status = str(job.get("status") or "")
            if parent_status in _TERMINAL_LOCAL_JOB_STATUSES:
                row = self._terminalize_late_action_row(row, parent_status)
                actions = upsert_action_row(list(job.get("actions") or []), row)
                job["actions"] = self._settle_post_terminal_actions(
                    actions, parent_status,
                )
            else:
                job["actions"] = upsert_action_row(list(job.get("actions") or []), row)
            import time
            job["updated_at"] = time.time()
            self._persist_local_jobs_locked()

    def _ingest_local_job_events(self, job_id: str, events: Optional[Iterable[Any]]) -> list:
        """Ingest a completed WorkerResult.events list into job['actions'].

        Returns a deep-copied snapshot of the resulting actions list.
        Does not touch ``_display_transcript``; drain under ``_busy`` mirrors.
        Post-terminal ingest settles any late running rows instead of spinning.
        """
        incoming = ingest_worker_events(events)
        with self._local_jobs_lock:
            job = self._local_jobs.get(job_id)
            if not job:
                return snapshot_actions(incoming)
            parent_status = str(job.get("status") or "")
            terminal = parent_status in _TERMINAL_LOCAL_JOB_STATUSES
            actions = list(job.get("actions") or [])
            for row in incoming:
                if terminal:
                    row = self._terminalize_late_action_row(row, parent_status)
                actions = upsert_action_row(actions, row)
            if terminal:
                actions = self._settle_post_terminal_actions(actions, parent_status)
            job["actions"] = actions
            import time
            job["updated_at"] = time.time()
            self._persist_local_jobs_locked()
            return snapshot_actions(actions)

    @staticmethod
    def _terminalize_late_action_row(row: dict, job_status: str = "") -> dict:
        """Settle a late progressive row to match the parent job outcome.

        completed -> complete (not failed/red); failed/cancelled -> failed with a
        short safe error. Never leaves status=running after the parent is terminal.
        """
        if not isinstance(row, dict):
            return row
        if str(row.get("status") or "").lower() != "running":
            return row
        out = dict(row)
        parent = str(job_status or "").strip().lower()
        if parent == "completed":
            out["status"] = "complete"
            return out
        out["status"] = "failed"
        if not out.get("error"):
            out["error"] = (
                "cancelled" if parent == "cancelled" else "job already finished"
            )
        return out

    @staticmethod
    def _settle_post_terminal_actions(actions: list, job_status: str) -> list:
        """Safety-net settle for any running rows still present after terminalize."""
        parent = str(job_status or "").strip().lower()
        if parent == "completed":
            return settle_running_actions(
                actions, reason="job already finished", to_status="complete",
            )
        reason = "cancelled" if parent == "cancelled" else "job already finished"
        return settle_running_actions(actions, reason=reason, to_status="failed")

    def _mirror_local_job_actions_to_display(self, job_id: str) -> None:
        """Mirror sanitized actions onto display cards (safe drain / main path).

        Acquires ``_local_jobs_lock``. Callers that already hold the session
        single-writer ``_busy`` lock (e.g. ``drain_swarm_results``) may use this
        for reload durability without racing progressive worker threads.
        """
        with self._local_jobs_lock:
            job = self._local_jobs.get(job_id)
            if not job:
                return
            self._mirror_job_actions_to_display_locked(
                job_id, job.get("actions") or [],
            )

    def _mirror_job_actions_to_display_locked(self, job_id: str, actions: list) -> None:
        """Best-effort: attach nested actions onto matching display cards by job_id.

        Must be called while holding ``_local_jobs_lock``. Display transcript is
        session-owned; failures must never break worker bookkeeping. Progressive
        worker callbacks must not call this — only locked/main drain paths.
        """
        display = getattr(self, "_display_transcript", None)
        if not isinstance(display, list) or not job_id:
            return
        try:
            snap = snapshot_actions(actions)
            for entry in display:
                if not isinstance(entry, dict) or entry.get("type") != "card":
                    continue
                result = entry.get("result")
                if not isinstance(result, dict):
                    continue
                card_job = str(result.get("job_id") or "")
                if not card_job:
                    continue
                # run_parallel may join several ids with commas.
                job_ids = {p.strip() for p in card_job.split(",") if p.strip()}
                if job_id not in job_ids and card_job != job_id:
                    continue
                if len(job_ids) > 1:
                    # Parent parallel card: merge this worker's rows under a
                    # stable per-job namespace so siblings do not collide.
                    # Cap the combined multi-job list at MAX_JOB_ACTIONS (same
                    # as per-job persistence) so N×80 cannot balloon the card.
                    existing = list(entry.get("actions") or [])
                    prefixed = []
                    for row in snap:
                        if not isinstance(row, dict):
                            continue
                        cloned = dict(row)
                        aid = str(cloned.get("action_id") or "")
                        if aid and not aid.startswith(f"{job_id}:"):
                            cloned["action_id"] = f"{job_id}:{aid}"
                        cloned["worker_id"] = job_id
                        prefixed.append(cloned)
                    for row in prefixed:
                        existing = upsert_action_row(existing, row)
                    if len(existing) > MAX_JOB_ACTIONS:
                        existing = existing[-MAX_JOB_ACTIONS:]
                    entry["actions"] = existing
                else:
                    entry["actions"] = snap[:MAX_JOB_ACTIONS]
                    entry["worker_id"] = job_id
        except Exception:
            pass

    def live_local_jobs(self) -> list:
        """Snapshot of in-process provider-native worker jobs for /api/swarm/live.
        Returns deep copies so the server can merge without holding the session lock."""
        with self._local_jobs_lock:
            out = []
            for job in self._local_jobs.values():
                snap = copy.deepcopy(job)
                snap["actions"] = snapshot_actions(snap.get("actions"))
                out.append(snap)
            return out

