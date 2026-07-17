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

import os
import threading
from typing import Optional


class LocalJobsMixin:
    """Mixin holding in-process local-job register/finish/persist/cancel helpers.

    The concrete class (ConversationalSession) supplies the state these
    methods read/write via `self`. This mixin defines no __init__ and no
    instance state of its own.
    """

    def _register_local_job(self, job_id: str, goal: str, role: str = "implement",
                            cwd: str = "", engine: str = "", model: str = "") -> None:
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
        instead of a bare ``agentic`` badge.
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
        model_id = (model or "").strip()
        if not model_id and engine_label == "native":
            model_id = (self.config.driver or "").strip()
        routing_arts: list = []
        est_cost = 0.0
        if engine_label == "agentic" and not model_id:
            try:
                from harness.local_job_routing import preview_agentic_route
                preview = preview_agentic_route(goal, role=role or "implement")
            except Exception:
                preview = {}
            model_id = (preview.get("model_id") or "").strip()
            est_cost = float(preview.get("est_cost_usd") or 0.0)
            art = preview.get("artifact")
            if isinstance(art, dict):
                routing_arts.append(art)
        display_model = f"{engine_label}/{model_id}" if model_id else engine_label
        task_role = f"{role} ({engine_label})" if role else f"implement ({engine_label})"
        with self._local_jobs_lock:
            self._local_job_cancels[job_id] = threading.Event()
            now = time.time()
            self._local_jobs[job_id] = {
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
                "tasks": [{
                    "id": f"{job_id}-w0",
                    "role": task_role,
                    "instruction": goal,
                    "status": "running",
                    "adapter": engine_label,
                }],
            }
            self._persist_local_jobs_locked()

    def _finish_local_job(self, job_id: str, ok: bool, summary: str = "",
                          files: Optional[list] = None, tokens: int = 0,
                          est_cost_usd: float = 0.0,
                          status: str = "",
                          engine: str = "", model: str = "") -> None:
        """Flip a live local job to its terminal state so the panel stops showing
        a spinner and surfaces the outcome (files touched + a one-line summary).

        When ``engine`` / ``model`` are known (from WorkerResult), overwrite the
        provisional register-time labels so an agentic run never keeps a native
        or pilot-slug stamp after it finishes.
        """
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
            model_id = (model or "").strip()
            if engine_label in ("agentic", "native"):
                job["adapter"] = engine_label
                if job.get("tasks"):
                    job["tasks"][0]["adapter"] = engine_label
                    base_role = (job.get("role") or "implement").strip() or "implement"
                    job["tasks"][0]["role"] = f"{base_role} ({engine_label})"
            if engine_label or model_id:
                eng = engine_label or (job.get("adapter") or "").strip() or "native"
                mid = model_id
                if mid:
                    job["model"] = f"{eng}/{mid}"
                elif eng:
                    job["model"] = eng
            if tokens:
                job["tokens"] = tokens
            real_cost = float(est_cost_usd or 0.0)
            if not real_cost and tokens:
                # Provider-worker jobs only carry a combined token total (no
                # in/out split). Price at the output rate so output-heavy runs
                # are not systematically under-priced. Prefer the worker's own
                # model when stamped; else fall back to the pilot driver.
                try:
                    from pmharness.registry import resolve_price
                    from harness.server import _job_cost
                    price_spec = model_id or (job.get("model") or "")
                    # Strip engine/ prefix if present (e.g. agentic/z-ai/...).
                    if "/" in price_spec and price_spec.split("/", 1)[0] in (
                        "agentic", "native",
                    ):
                        price_spec = price_spec.split("/", 1)[1]
                    price_spec = price_spec or self.config.driver
                    price_in, price_out = resolve_price(price_spec)
                    real_cost = _job_cost(0, 0, tokens, price_in, price_out)
                except Exception:
                    real_cost = 0.0
            if real_cost:
                job["est_cost_usd"] = round(real_cost, 6)
            if job.get("tasks"):
                job["tasks"][0]["status"] = terminal
            if cancelled and not summary:
                headline = "Cancelled by user"
            else:
                headline = (summary or "").strip().splitlines()[0] if summary else (
                    "Patch applied" if ok else "Worker failed")
            if files:
                headline = f"{headline} ({len(files)} file{'s' if len(files) != 1 else ''})"
            # Keep any pre-stamped ROUTING card (model/cost preview) and update
            # its estimate to the real spend so expand still shows the model.
            keep_routing = []
            for art in (job.get("artifacts") or []):
                if not isinstance(art, dict):
                    continue
                if (art.get("type") or "").strip().upper() != "ROUTING":
                    continue
                updated = dict(art)
                if model_id:
                    updated["model"] = model_id
                    updated["headline"] = f"Routed to {model_id}"
                if real_cost:
                    updated["est_cost_usd"] = round(real_cost, 6)
                keep_routing.append(updated)
            job["artifacts"] = keep_routing + [{
                "type": "patch" if (ok and not cancelled) else "error",
                "headline": headline[:240],
            }]
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
        in history but get NO live cancel Event (nothing to cancel)."""
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
                if job.get("status") == "running":
                    job["status"] = "cancelled"
                    job["updated_at"] = job.get("updated_at") or job.get("created_at")
                    if job.get("tasks"):
                        try:
                            job["tasks"][0]["status"] = "cancelled"
                        except Exception:
                            pass
                    job["artifacts"] = [{
                        "type": "error",
                        "headline": "Interrupted by backend restart",
                    }]
                self._local_jobs[jid] = job
            # Rewrite so the healed statuses are the new on-disk baseline.
            self._persist_local_jobs_locked()

    def cancel_local_job(self, job_id: str) -> bool:
        """Cooperatively cancel a running local (provider-worker) job. Sets the
        per-job cancel Event (best-effort: a Python thread cannot be force-killed,
        so the underlying provider call may still run to completion) and flips the
        job to a terminal 'cancelled' state immediately so the UI stops spinning.
        Returns True if the job existed and was running, False otherwise."""
        with self._local_jobs_lock:
            job = self._local_jobs.get(job_id)
            if job is None:
                return False
            already_terminal = job.get("status") in ("completed", "failed", "cancelled")
            ev = self._local_job_cancels.get(job_id)
            if ev is not None:
                ev.set()
            if already_terminal:
                return False
            job["status"] = "cancelled"
        # _finish_local_job re-acquires the lock and persists.
        self._finish_local_job(job_id, ok=False, summary="Cancelled by user",
                               status="cancelled")
        return True

    def _local_job_cancelled(self, job_id: str) -> bool:
        """True if a cancel was requested for this job. Checked by the worker at
        its wall-clock boundary (best-effort cooperative cancel)."""
        ev = self._local_job_cancels.get(job_id)
        return bool(ev is not None and ev.is_set())

    def live_local_jobs(self) -> list:
        """Snapshot of in-process provider-native worker jobs for /api/swarm/live.
        Returns copies so the server can merge without holding the session lock."""
        with self._local_jobs_lock:
            return [dict(job) for job in self._local_jobs.values()]

