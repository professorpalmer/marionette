from __future__ import annotations

"""Conversation-jobs mixin: await/apply, provider-worker background, swarm drain.

Extracted mechanically from harness/conversation.py to continue decomposing the
ConversationalSession god-object, matching LocalJobsMixin / BusyControlMixin
contract: these methods operate through `self` (``_swarm_results``, ``_busy``,
``_apply_lock``, ``_history``, ``_local_jobs`` helpers, …) provided by the
concrete class -- the mixin defines no state and no __init__.

Owns the hot job-bridge helpers:
- ``_await_and_apply_job`` — await Puppetmaster job + fold artifacts/patch
- ``_run_provider_worker_background`` — in-process provider edit worker
- ``drain_swarm_results`` — non-blocking poll drain + pilot_resume keep-alive

Local-job register/finish/persist stays on LocalJobsMixin; busy lifecycle on
BusyControlMixin; send-loop submit on SendLoopMixin. Zero wire/JSON/status
change — only the method definitions move.

Method Resolution Order keeps behavior identical: callers still resolve these
via ConversationalSession inheritance.
"""

from typing import Iterator, Optional

from ._exec import _puppetmaster_cmd


class ConversationJobsMixin:
    """Mixin holding swarm job await/apply/drain helpers.

    The concrete class (ConversationalSession) supplies the state these
    methods read/write via `self`. This mixin defines no __init__ and no
    instance state of its own.
    """

    def _await_and_apply_job(self, job_id: str, state_dir: Optional[str] = None, objective: str = "") -> dict:
        import json
        import subprocess
        # 1. Await job
        if state_dir:
            await_cmd = _puppetmaster_cmd("--state-dir", state_dir, "await", job_id, "--cwd", self.config.repo)
        else:
            await_cmd = _puppetmaster_cmd("await", job_id, "--cwd", self.config.repo)
        subprocess.run(await_cmd, cwd=self.config.repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)

        # 2. Fetch artifacts
        if state_dir:
            art_cmd = _puppetmaster_cmd("--state-dir", state_dir, "artifacts", job_id, "--cwd", self.config.repo)
        else:
            art_cmd = _puppetmaster_cmd("artifacts", job_id, "--cwd", self.config.repo)
        art_p = subprocess.run(art_cmd, cwd=self.config.repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=60)
        art_out = art_p.stdout or ""
        try:
            artifacts = json.loads(art_out)
        except Exception:
            artifacts = []

        # 3. Add worker tokens
        tokens_in, tokens_out, tokens_cached = self._add_worker_tokens_from_artifacts(artifacts)

        # 4. Process artifacts
        num_artifacts = len(artifacts)
        artifact_types = sorted({str(a.get("type", "finding")) for a in artifacts})

        patch_summary = ""
        patch_art = next((a for a in artifacts if isinstance(a, dict) and a.get("type") == "patch"), None)
        if patch_art:
            payload = patch_art.get("payload") or {}
            files_changed = payload.get("files", [])
            if files_changed:
                patch_summary = f"Files changed: {', '.join(files_changed)}"
            else:
                diff_text = payload.get("unified_diff") or ""
                if diff_text:
                    patch_summary = f"Diff total chars: {len(diff_text)}"

        findings_summary = []
        for a in artifacts:
            if isinstance(a, dict) and a.get("type") == "finding":
                rep = (a.get("payload") or {}).get("report") or ""
                if rep:
                    findings_summary.append(rep[:120])

        summary_parts = []
        if patch_summary:
            summary_parts.append(patch_summary)
        if findings_summary:
            summary_parts.append("; ".join(findings_summary[:3]))

        summary = "\n".join(summary_parts) if summary_parts else "Successfully completed implement task"

        ar_list = []
        for a in artifacts[:8]:
            if not isinstance(a, dict):
                continue
            t = a.get("type", "finding")
            headline = ""
            if t == "patch":
                files = (a.get("payload") or {}).get("files") or []
                headline = f"Patch: modified {', '.join(files)}" if files else "Patch generated"
            elif t == "finding":
                claim = (a.get("payload") or {}).get("claim") or ""
                rep = (a.get("payload") or {}).get("report") or ""
                headline = claim or rep[:80] or "Finding"
            else:
                headline = f"{t.capitalize()} artifact"
            ar_list.append({"type": t, "headline": headline})

        # 5. Apply patch
        # CORRECTNESS (comment these in code): Guard the git apply operation with self._apply_lock
        # so two concurrent backgrounded swarms cannot attempt to run git apply / git merge simultaneously,
        # which would cause repository index/state corruption.
        has_patch_art = any(isinstance(a, dict) and a.get("type") == "patch" for a in artifacts)
        held_for_review = False
        pending_review_info = None

        if has_patch_art and getattr(self, "_review_edits_before_apply", False):
            held_for_review = True

            # Find patch artifact and parse it
            patch_art = next((a for a in artifacts if isinstance(a, dict) and a.get("type") == "patch"), None)
            payload = patch_art.get("payload") or {}
            diff_text = payload.get("unified_diff") or ""

            from .diffreview import parse_unified_diff
            parsed_files = parse_unified_diff(diff_text)

            import uuid
            import time
            review_id = f"rev-{uuid.uuid4().hex[:8]}"

            pending_review = {
                "id": review_id,
                "job_id": job_id,
                "objective": objective or "Implement edits",
                "files": parsed_files,
                "created_at": time.time()
            }

            with self._pending_reviews_lock:
                self._pending_reviews[review_id] = pending_review

            pending_review_info = {
                "id": review_id,
                "summary": f"Held {len(parsed_files)} files for review"
            }

            applied = False
            applied_files = []
            apply_msg = "held for review"
            cp_id = None

            apply_summary = f"Patch held for review (ID: {review_id})"
        else:
            with self._apply_lock:
                applied, applied_files, apply_msg = self._apply_worker_patch(artifacts, job_id)
                cp_id = getattr(self, "_last_checkpoint_id", None)

            apply_summary = ""
            if has_patch_art:
                if applied:
                    apply_summary = f"Applied patch to {len(applied_files)} files: {', '.join(applied_files)}"
                else:
                    apply_summary = f"PATCH DID NOT APPLY: {apply_msg}"

        if apply_summary:
            summary = f"{summary}\n{apply_summary}" if summary else apply_summary

        error = f"PATCH DID NOT APPLY: {apply_msg}" if (has_patch_art and not applied and not held_for_review) else None

        # Check if any preflight or verification task failed before a patch could be generated
        if not error:
            blocked_or_failed_verifications = [
                a for a in artifacts if isinstance(a, dict) and a.get("type") == "verification" and a.get("result") in ("blocked", "failed")
            ]
            if blocked_or_failed_verifications:
                v = blocked_or_failed_verifications[0]
                v_payload = v.get("payload") or {}
                fail_type = v_payload.get("failure") or "unknown_failure"
                fail_msg = v_payload.get("message") or ""
                if not fail_msg:
                    raw_err = v_payload.get("stderr") or v_payload.get("stdout") or ""
                    err_lines = []
                    for line in raw_err.splitlines():
                        if any(term in line.lower() for term in ["error", "exception", "unauthorized", "fail", "401", "403", "denied", "invalid"]):
                            err_lines.append(line.strip())
                    if err_lines:
                        fail_msg = " | ".join(err_lines[:3])
                    else:
                        fail_msg = raw_err[:200]

                error = f"{fail_type}: {fail_msg}" if fail_msg else fail_type

        return {
            "job_id": job_id,
            "applied": applied,
            "files": applied_files,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_cached": tokens_cached,
            "summary": summary,
            "error": error,
            "artifacts": artifacts,
            "has_patch_art": has_patch_art,
            "apply_msg": apply_msg,
            "num_artifacts": num_artifacts,
            "artifact_types": artifact_types,
            "ar_list": ar_list,
            "checkpoint_id": cp_id,
            "held_for_review": held_for_review,
            "pending_review": pending_review_info
        }

    def _run_provider_worker_background(
        self, job_id: str, objective: str, requested_adapter: str = "",
        target_repo: str = "", expects_diff: bool = True,
    ) -> None:
        from .conversation import append_failed_declarative_checks_summary

        try:
            from harness.worker import WorkerResult

            # Bounded run so a wedged worker frees its _swarm_pool slot on the
            # hard deadline instead of occupying it forever (audit finding #4).
            # target_repo (optional): abs path to a DIFFERENT git repo than the
            # open workspace; swaps self.config for a shallow-copied per-dispatch
            # HarnessConfig so the engines transparently target that repo.
            def _on_worker_event(ev):
                try:
                    self._upsert_local_job_action(job_id, ev)
                except Exception:
                    pass

            res = self._run_edit_worker_bounded(
                objective, requested_adapter, job_id=job_id,
                target_repo=target_repo, expects_diff=expects_diff,
                on_event=_on_worker_event,
            )
            if self._local_job_cancelled(job_id):
                # A cancel landed while the worker was running. The job was already
                # flipped to 'cancelled' by cancel_local_job(); drop the result so
                # we do not re-open/overwrite the terminal state, and stop here.
                return
            if res is None:
                deadline = int(self._worker_deadline_seconds())
                res = WorkerResult(
                    ok=False,
                    error=f"worker exceeded {deadline}s wall-clock deadline",
                    summary=f"Worker exceeded its {deadline}s deadline and was abandoned to free the pool slot.",
                )

            if not res.ok:
                # A worker that produced NO patch ("no changes produced" /
                # degrade path) still SPENT tokens exploring -- read the real
                # counts off the result instead of hard-coding 0, so the job
                # surfaces its true cost in the tracker (previously these jobs
                # showed no price at all while normal completions did).
                _nc_t_in = int(getattr(res, "tokens_in", 0) or 0)
                _nc_t_out = int(getattr(res, "tokens_out", 0) or 0)
                _nc_t_cached = int(getattr(res, "tokens_cached", 0) or 0)
                if _nc_t_in or _nc_t_out or _nc_t_cached:
                    with self._apply_lock:
                        self._tokens_used += _nc_t_out + _nc_t_in
                        self._tokens_in += _nc_t_in
                        self._tokens_out += _nc_t_out
                        # Cached prompt tokens are a SUBSET of tokens_in already
                        # counted above; do NOT re-add to _tokens_used, only
                        # feed the cache-savings meter.
                        self._tokens_cached += _nc_t_cached
                        # Worker dollars at the worker's own model rate.
                        self._attribute_worker_cost(
                            _nc_t_in, _nc_t_out,
                            real_cost_usd=float(getattr(res, "est_cost_usd", 0.0) or 0.0))
                res_dict = {
                    "job_id": job_id,
                    "applied": False,
                    "files": [],
                    "tokens_in": _nc_t_in,
                    "tokens_out": _nc_t_out,
                    "tokens_cached": _nc_t_cached,
                    "summary": append_failed_declarative_checks_summary(
                        res.summary or res.error or "Worker failed to produce patch",
                        getattr(res, "declarative_checks", None),
                    ),
                    "error": res.error,
                    "artifacts": [],
                    "has_patch_art": False,
                    "apply_msg": res.error or "Worker failed to produce patch",
                    "num_artifacts": 0,
                    "artifact_types": [],
                    "ar_list": []
                }
            elif not (res.patch or "").strip():
                # Analysis/review with no patch: only green when the summary
                # carries substantive findings. Verification/plumbing-only
                # outputs must surface degraded/failed, never a clean done.
                tokens_in = int(getattr(res, "tokens_in", 0) or 0)
                tokens_out = int(getattr(res, "tokens_out", 0) or 0)
                tokens_cached = int(getattr(res, "tokens_cached", 0) or 0)
                with self._apply_lock:
                    self._tokens_used += tokens_out + tokens_in
                    self._tokens_in += tokens_in
                    self._tokens_out += tokens_out
                    self._tokens_cached += tokens_cached
                    self._attribute_worker_cost(
                        tokens_in, tokens_out,
                        real_cost_usd=float(getattr(res, "est_cost_usd", 0.0) or 0.0))
                summary = res.summary or "Successfully completed analysis task"
                substantive = True
                if not expects_diff:
                    try:
                        from harness.pilot_guards import analysis_summary_is_substantive
                        substantive = analysis_summary_is_substantive(summary)
                    except Exception:
                        substantive = bool((summary or "").strip())
                if not expects_diff and not substantive:
                    degrade_err = (
                        "analysis produced no substantive findings "
                        "(verification/plumbing only)"
                    )
                    res_dict = {
                        "job_id": job_id,
                        "applied": False,
                        "files": [],
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "tokens_cached": tokens_cached,
                        "summary": summary,
                        "error": degrade_err,
                        "artifacts": [],
                        "has_patch_art": False,
                        "apply_msg": degrade_err,
                        "num_artifacts": 0,
                        "artifact_types": [],
                        "ar_list": [],
                        "degraded": True,
                    }
                else:
                    res_dict = {
                        "job_id": job_id,
                        "applied": True,
                        "files": [],
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "tokens_cached": tokens_cached,
                        "summary": summary,
                        "error": None,
                        "artifacts": [],
                        "has_patch_art": False,
                        "apply_msg": "",
                        "num_artifacts": 0,
                        "artifact_types": [],
                        "ar_list": [],
                    }
            else:
                artifacts = []
                artifacts.append({
                    "type": "patch",
                    "payload": {
                        "unified_diff": res.patch,
                        "files": res.files_changed or []
                    }
                })

                tokens_in = res.tokens_in
                tokens_out = res.tokens_out
                tokens_cached = int(getattr(res, "tokens_cached", 0) or 0)
                with self._apply_lock:
                    # Attribute the worker's FULL spend (prompt + completion) to
                    # the parent session's cost meter. Track _tokens_out too, not
                    # just _tokens_in: the cost accounting prices output at the
                    # (higher) completion rate, so dropping _tokens_out here made
                    # implement-worker output get billed at the cheaper input
                    # rate -- undercounting every implement worker's real cost.
                    self._tokens_used += tokens_out + tokens_in
                    self._tokens_in += tokens_in
                    self._tokens_out += tokens_out
                    # Cached prompt tokens are already inside tokens_in above;
                    # feed the parent's cache-savings meter without inflating
                    # _tokens_used (avoids double-counting).
                    self._tokens_cached += tokens_cached
                    # Worker dollars at the worker's own model rate (prefer the
                    # result's real cost when present, else derive from rate).
                    self._attribute_worker_cost(
                        tokens_in, tokens_out,
                        real_cost_usd=float(getattr(res, "est_cost_usd", 0.0) or 0.0))

                patch_summary = ""
                if res.files_changed:
                    patch_summary = f"Files changed: {', '.join(res.files_changed)}"
                elif res.patch:
                    patch_summary = f"Diff total chars: {len(res.patch)}"

                summary = patch_summary if patch_summary else "Successfully completed implement task"
                if res.summary:
                    summary = f"{summary}\n{res.summary}"

                ar_list = [{
                    "type": "patch",
                    "headline": f"Patch: modified {', '.join(res.files_changed)}" if res.files_changed else "Patch generated"
                }]

                has_patch_art = True
                held_for_review = False
                pending_review_info = None

                if getattr(self, "_review_edits_before_apply", False):
                    held_for_review = True
                    from .diffreview import parse_unified_diff
                    parsed_files = parse_unified_diff(res.patch)

                    import uuid
                    import time
                    review_id = f"rev-{uuid.uuid4().hex[:8]}"

                    pending_review = {
                        "id": review_id,
                        "job_id": job_id,
                        "objective": objective or "Implement edits",
                        "files": parsed_files,
                        "created_at": time.time()
                    }

                    with self._pending_reviews_lock:
                        self._pending_reviews[review_id] = pending_review

                    pending_review_info = {
                        "id": review_id,
                        "summary": f"Held {len(parsed_files)} files for review"
                    }

                    applied = False
                    applied_files = []
                    apply_msg = "held for review"
                    cp_id = None
                    apply_summary = f"Patch held for review (ID: {review_id})"
                else:
                    with self._apply_lock:
                        applied, applied_files, apply_msg = self._apply_worker_patch(artifacts, job_id)
                        cp_id = getattr(self, "_last_checkpoint_id", None)

                    apply_summary = ""
                    if applied:
                        apply_summary = f"Applied patch to {len(applied_files)} files: {', '.join(applied_files)}"
                    else:
                        apply_summary = f"PATCH DID NOT APPLY: {apply_msg}"

                if apply_summary:
                    summary = f"{summary}\n{apply_summary}" if summary else apply_summary

                summary = append_failed_declarative_checks_summary(
                    summary,
                    getattr(res, "declarative_checks", None),
                )

                error = f"PATCH DID NOT APPLY: {apply_msg}" if (not applied and not held_for_review) else None

                res_dict = {
                    "job_id": job_id,
                    "applied": applied,
                    "files": applied_files,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "tokens_cached": tokens_cached,
                    "summary": summary,
                    "error": error,
                    "artifacts": artifacts,
                    "has_patch_art": has_patch_art,
                    "apply_msg": apply_msg,
                    "num_artifacts": len(artifacts),
                    "artifact_types": ["patch"],
                    "ar_list": ar_list,
                    "checkpoint_id": cp_id,
                    "held_for_review": held_for_review,
                    "pending_review": pending_review_info
                }

            # Always fold completed WorkerResult.events into job['actions']
            # (progressive callback may have already recorded most of them).
            try:
                self._ingest_local_job_events(job_id, getattr(res, "events", None))
            except Exception:
                pass

            wr_engine = (getattr(res, "engine", None) or "").strip()
            wr_model = (getattr(res, "model", None) or "").strip()
            self._finish_local_job(
                job_id,
                ok=not res_dict.get("error"),
                summary=res_dict.get("summary", ""),
                files=res_dict.get("files") or [],
                tokens=res_dict.get("tokens_out", 0) + res_dict.get("tokens_in", 0),
                est_cost_usd=float(getattr(res, "est_cost_usd", 0.0) or 0.0),
                engine=wr_engine,
                model=wr_model,
            )
            self._swarm_results.put({
                "job_id": job_id,
                "objective": objective,
                "result": res_dict,
                "state_dir": None
            })

        except Exception as e:
            self._finish_local_job(job_id, ok=False, summary=f"Failed background worker: {e}")
            self._swarm_results.put({
                "job_id": job_id,
                "objective": objective,
                "result": {
                    "job_id": job_id,
                    "applied": False,
                    "files": [],
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "summary": f"Failed background worker: {e}",
                    "error": str(e),
                    "artifacts": [],
                    "has_patch_art": False,
                    "apply_msg": str(e),
                    "num_artifacts": 0,
                    "artifact_types": [],
                    "ar_list": []
                },
                "state_dir": None
            })
        finally:
            # Free the objective for legitimate future dispatch regardless of
            # how this worker settled (applied, failed, or crashed).
            self._release_objective(objective)

    def drain_swarm_results(self) -> Iterator[ConvEvent]:
        # Drain finished background-swarm results, appending follow-up messages to
        # history under the single-writer _busy lock. CRITICAL: acquire NON-blocking.
        # This is called from an HTTP handler (the 2.5s frontend poll). If a chat
        # turn is in flight (or a wedged turn never released _busy), a blocking
        # acquire would hang the server thread indefinitely -- the "swarm running
        # forever / app hung" symptom. If we can't get the lock right now, just
        # return nothing; the next poll (2.5s later) drains it once the turn frees
        # the lock. Results stay queued, so nothing is lost.
        #
        # But a turn that WEDGED (a hung provider call the step-boundary budget
        # check can't interrupt) would hold _busy forever and starve this drain --
        # completed worker patches would never surface. The reaper force-recovers
        # such a turn past the hard deadline so the app self-heals (audit #6).
        from .conversation import ConvEvent

        self._reap_stuck_turn()
        if not self._busy.acquire(blocking=False):
            return
        try:
            import queue
            # (job_id, objective, failed, error, degraded)
            finished_jobs: list[tuple[str, str, bool, str, bool]] = []
            while True:
                try:
                    item = self._swarm_results.get_nowait()
                except queue.Empty:
                    break

                try:
                    job_id = item["job_id"]
                    objective = item["objective"]
                    res_job = item["result"]

                    if isinstance(res_job, dict) and res_job.get("kind") in ("distilled", "wiki_prepared"):
                        yield ConvEvent(res_job["kind"], res_job["data"])
                        self._swarm_results.task_done()
                        continue

                    # Append a labeled follow-up assistant message to self._history (SINGLE-WRITER held via _busy lock!)
                    applied = res_job["applied"]
                    applied_files = res_job["files"]
                    summary = res_job["summary"]
                    held_for_review = bool(res_job.get("held_for_review"))
                    failed = bool(
                        res_job.get("error")
                        or (not applied and not held_for_review)
                    )

                    if failed:
                        # Loud failure keep-alive: never dress a dead worker as a
                        # quiet "swarm result" -- the pilot must not pretend a
                        # patch landed.
                        err_bit = (res_job.get("error") or summary or "worker failed").strip()
                        msg_content = f"[swarm FAILED for: {objective}] {err_bit}"
                        if res_job.get("has_patch_art") and not applied:
                            apply_msg = res_job.get("apply_msg") or ""
                            if apply_msg and apply_msg not in msg_content:
                                msg_content += f"; patch failed to apply: {apply_msg}"
                        display_error = res_job.get("error") or err_bit or None
                    else:
                        err_bit = ""
                        msg_content = f"[swarm result for: {objective}] {summary}"
                        if applied and applied_files:
                            msg_content += f"; applied {len(applied_files)} files"
                        elif held_for_review:
                            msg_content += f"; held for review"
                        elif res_job.get("has_patch_art") and not applied:
                            msg_content += f"; patch failed to apply: {res_job.get('apply_msg')}"
                        display_error = res_job.get("error") or None

                    self._history.append({"role": "assistant", "content": msg_content})

                    # Persist the outcome to the display transcript so the green/red
                    # "swarm done / swarm failed" badge survives a session reload or
                    # app restart -- the live ConvEvent below only reaches a renderer
                    # that is open right now.
                    self._display_transcript.append({
                        "type": "swarm_result",
                        "job_id": job_id,
                        "applied": bool(applied),
                        "files": list(applied_files or []),
                        "summary": summary or "",
                        "error": display_error,
                        "objective": objective,
                    })
                    # Nested actions are progressive via /api/swarm/live; mirror
                    # onto display cards only here under _busy for reload durability.
                    try:
                        self._mirror_local_job_actions_to_display(job_id)
                    except Exception:
                        pass

                    # Yield ConvEvent kind="swarm_result" (per-job; badges depend on it)
                    yield ConvEvent("swarm_result", {
                        "job_id": job_id,
                        "objective": objective,
                        "result": res_job,
                        "message": msg_content
                    })

                    pending_review = res_job.get("pending_review")
                    if pending_review:
                        yield ConvEvent("pending_review", {
                            "id": pending_review["id"],
                            "summary": pending_review["summary"]
                        })

                    checkpoint_id = res_job.get("checkpoint_id")
                    if checkpoint_id:
                        yield ConvEvent("checkpoint", {
                            "id": checkpoint_id,
                            "trigger": "swarm_patch",
                            "label": f"Before swarm patch {job_id[:8]}"
                        })

                    # Track failed/degraded outcomes for keep-alive resume capping.
                    degraded = bool(res_job.get("degraded"))
                    if not degraded and not failed and not (applied_files or []):
                        # Empty-diff "success" with no substantive summary is
                        # treated as degraded for resume-cap purposes.
                        try:
                            from harness.pilot_guards import analysis_summary_is_substantive
                            if not analysis_summary_is_substantive(summary or ""):
                                degraded = True
                        except Exception:
                            pass
                    finished_jobs.append((
                        job_id,
                        objective,
                        failed,
                        (res_job.get("error") or err_bit or "") if failed else "",
                        degraded,
                    ))
                except Exception:
                    # Best-effort: never raise on the chat hot path; degrade to
                    # continuing the drain so remaining results still surface.
                    pass
                finally:
                    try:
                        self._swarm_results.task_done()
                    except Exception:
                        pass

            # Coalesce: one merged user continuation + one pilot_resume per drain
            # pass (not per job). Keeps the keep-alive contract while avoiding
            # N resume turns when N workers finish in the same poll window.
            # After explicit Stop, still emit swarm_result badges above but do
            # NOT append resume text or fire pilot_resume -- that re-arms thinking.
            suppress_resume = (
                getattr(self, "_interrupted_swarms", False)
                or getattr(self, "_stop_holds_idle", False)
                or self._cancel.is_set()
            )
            # Bound post-swarm keep-alive redispatch for the same normalized
            # failed/degraded objective so provider outages cannot create
            # endless resume chains. Successful substantive work resets the key;
            # fresh user turns clear the whole map in send().
            if finished_jobs and not suppress_resume:
                try:
                    from harness.pilot_guards import (
                        failed_objective_resume_cap,
                        normalize_objective_key,
                    )
                    counts = getattr(self, "_failed_objective_resume_counts", None)
                    if counts is None:
                        counts = {}
                        self._failed_objective_resume_counts = counts
                    cap = failed_objective_resume_cap()
                    capped_jobs: list[tuple] = []
                    resume_jobs: list[tuple] = []
                    for item in finished_jobs:
                        job_id, objective, failed, err, degraded = item
                        key = normalize_objective_key(objective)
                        if failed or degraded:
                            n = int(counts.get(key, 0) or 0) + 1
                            counts[key] = n
                            if n > cap:
                                capped_jobs.append(item)
                                continue
                        else:
                            counts.pop(key, None)
                        resume_jobs.append(item)
                    if not resume_jobs and capped_jobs:
                        # Still surface a user-visible notice, but do not fire
                        # pilot_resume (stops the endless keep-alive chain).
                        ids = ", ".join(jid for jid, *_rest in capped_jobs)
                        notice = (
                            f"Keep-alive resume capped for failed/degraded "
                            f"objective(s) after {cap} attempt(s) "
                            f"(jobs: {ids}). Report the failure and wait for "
                            f"the user — do not re-dispatch the same objective."
                        )
                        if self._history and self._history[-1].get("role") == "user":
                            self._history[-1]["content"] = (
                                self._history[-1]["content"].rstrip()
                                + "\n\n" + notice
                            )
                        else:
                            self._history.append({"role": "user", "content": notice})
                        yield ConvEvent("notice", {
                            "message": notice,
                            "kind": "resume_cap",
                        })
                        finished_jobs = []
                    else:
                        finished_jobs = resume_jobs
                except Exception:
                    pass
            if finished_jobs and not suppress_resume:
                try:
                    from harness.implement_guards import is_preflight_worker_error

                    def _fail_resume(job_id: str, err: str) -> str:
                        if is_preflight_worker_error(err):
                            return (
                                f"[background job {job_id} FAILED before work started] "
                                f"Setup/preflight error — no patch was attempted: {err}. "
                                "Tell the user clearly. Prefer Open Project / pass "
                                "repo=<git path> / run_command for filesystem tasks, "
                                "or retry once the workspace is a git checkout. Do not "
                                "claim a patch failed to land."
                            )
                        return (
                            f"[background job {job_id} FAILED] The swarm result above "
                            "did NOT land a patch. Report this failure to the user "
                            "clearly; do not pretend the patch was applied. Decide "
                            "whether to retry with a narrowed follow-up, gather more "
                            "context, or stop -- without waiting for the user to ask."
                        )

                    any_failed = any(failed for _jid, _obj, failed, _err, _deg in finished_jobs)
                    thin_analysis_nudge = (
                        " If this was a read-only analysis swarm and findings are "
                        "empty, vague, verification-only, or insufficient for the "
                        "user's ask, re-dispatch a narrowed run_swarm (or "
                        "run_parallel analysis roles) with a sharper objective — "
                        "do NOT open a broad inline exploration campaign "
                        "(list_dir/search_files/grep/read sweeps) as a substitute."
                    )
                    if len(finished_jobs) == 1:
                        job_id, _obj, failed, err, _deg = finished_jobs[0]
                        if failed:
                            resume_text = _fail_resume(job_id, err)
                        else:
                            resume_text = (
                                f"[background job {job_id} finished] The result above is now "
                                "available. Report the outcome to the user concisely and take "
                                "the appropriate next step (validate, run tests, apply/fix, or "
                                "run a narrowed follow-up) without waiting for the user to ask."
                                + thin_analysis_nudge
                            )
                    else:
                        ids = ", ".join(jid for jid, _obj, _f, _e, _d in finished_jobs)
                        if any_failed:
                            fail_bits = []
                            for jid, _obj, failed, err, _deg in finished_jobs:
                                if not failed:
                                    continue
                                if is_preflight_worker_error(err):
                                    fail_bits.append(f"{jid} (preflight: {err})")
                                else:
                                    fail_bits.append(jid)
                            resume_text = (
                                f"[background jobs {ids} finished; FAILED: "
                                f"{', '.join(fail_bits)}] "
                                "One or more swarm results above FAILED. Report "
                                "failures clearly; do not pretend patches were "
                                "applied when setup/preflight blocked the worker. "
                                "Take the appropriate next step without waiting "
                                "for the user to ask."
                            )
                        else:
                            resume_text = (
                                f"[background jobs {ids} finished] The results above are now "
                                "available. Report the outcomes to the user concisely and take "
                                "the appropriate next step (validate, run tests, apply/fix, or "
                                "run a narrowed follow-up) without waiting for the user to ask."
                                + thin_analysis_nudge
                            )
                    # Re-activate the pilot with a user-role continuation. But never
                    # create two adjacent user messages: some chat APIs (Anthropic)
                    # require strict user/assistant alternation, and the concurrency
                    # stress test guards it. If the last message is already a user turn
                    # (e.g. the user typed while a job was in flight), MERGE the resume
                    # text into it instead of appending a second user message.
                    if self._history and self._history[-1].get("role") == "user":
                        self._history[-1]["content"] = (
                            self._history[-1]["content"].rstrip() + "\n\n" + resume_text
                        )
                    else:
                        self._history.append({"role": "user", "content": resume_text})

                    yield ConvEvent("pilot_resume", {
                        "job_id": finished_jobs[0][0],
                        "job_ids": [jid for jid, _obj, _f, _e, _d in finished_jobs],
                        "objective": finished_jobs[0][1],
                    })
                except Exception:
                    # Degrade: emit one resume per job (previous behavior) so the
                    # keep-alive contract is preserved even if merge fails.
                    for job_id, objective, failed, err, *_rest in finished_jobs:
                        try:
                            if failed:
                                try:
                                    from harness.implement_guards import is_preflight_worker_error
                                    if is_preflight_worker_error(err):
                                        resume_text = (
                                            f"[background job {job_id} FAILED before work started] "
                                            f"Setup/preflight error — no patch was attempted: {err}. "
                                            "Tell the user clearly; do not claim a patch failed to land."
                                        )
                                    else:
                                        resume_text = (
                                            f"[background job {job_id} FAILED] The swarm result above "
                                            "did NOT land a patch. Report this failure to the user "
                                            "clearly; do not pretend the patch was applied. Decide "
                                            "whether to retry with a narrowed follow-up, gather more "
                                            "context, or stop -- without waiting for the user to ask."
                                        )
                                except Exception:
                                    resume_text = (
                                        f"[background job {job_id} FAILED] The swarm result above "
                                        "did NOT land a patch. Report this failure to the user "
                                        "clearly; do not pretend the patch was applied. Decide "
                                        "whether to retry with a narrowed follow-up, gather more "
                                        "context, or stop -- without waiting for the user to ask."
                                    )
                            else:
                                resume_text = (
                                    f"[background job {job_id} finished] The result above is now "
                                    "available. Report the outcome to the user concisely and take "
                                    "the appropriate next step (validate, run tests, apply/fix, or "
                                    "run a narrowed follow-up) without waiting for the user to ask."
                                )
                            if self._history and self._history[-1].get("role") == "user":
                                self._history[-1]["content"] = (
                                    self._history[-1]["content"].rstrip() + "\n\n" + resume_text
                                )
                            else:
                                self._history.append({"role": "user", "content": resume_text})
                            yield ConvEvent("pilot_resume", {
                                "job_id": job_id,
                                "objective": objective,
                            })
                        except Exception:
                            pass
        finally:
            self._busy.release()
