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


_WORKER_PROVENANCE_PATH_CAP = 12

# Soft-refuse cue for pilots/guards: empty managed implement already recovered once.
EMPTY_MANAGED_IMPLEMENT_EXHAUSTED = "empty_managed_implement_exhausted"

_EMPTY_WORKTREE_MARKERS = (
    "no changes in disposable managed worktree",
    "produced no changes in disposable managed worktree",
    "no changes produced",
    "worker produced no changes",
)


def _is_empty_diff_implement_failure(res, *, expects_diff: bool) -> bool:
    """True when an implement worker left the managed worktree unchanged."""
    if not expects_diff or res is None:
        return False
    try:
        if getattr(res, "worktree_diff_empty", None) is True:
            return True
    except Exception:
        pass
    try:
        if (getattr(res, "patch", None) or "").strip():
            return False
    except Exception:
        pass
    try:
        text = (
            f"{getattr(res, 'summary', '') or ''} "
            f"{getattr(res, 'error', '') or ''}"
        ).lower()
    except Exception:
        return False
    if not any(marker in text for marker in _EMPTY_WORKTREE_MARKERS):
        return False
    try:
        return not bool(getattr(res, "ok", True))
    except Exception:
        return True


def _empty_implement_recovery_objective(objective: str, dirty_paths: list) -> str:
    """Append a one-shot recovery instruction for seeded live dirty files."""
    shown = [str(p) for p in (dirty_paths or [])[:_WORKER_PROVENANCE_PATH_CAP]]
    path_hint = ", ".join(shown) if shown else "live dirty files"
    if len(dirty_paths or []) > len(shown):
        path_hint = f"{path_hint}, +{len(dirty_paths) - len(shown)} more"
    suffix = (
        "\n\n[recovery] Live checkout had pre-existing dirty/untracked files that "
        f"were seeded into this disposable managed worktree ({path_hint}). "
        "A non-empty patch against those seeded files is mandatory — do not "
        "finish with an empty worktree diff."
    )
    base = (objective or "").rstrip()
    return f"{base}{suffix}" if base else suffix.strip()


def _merge_worker_attempt_usage(primary, secondary) -> None:
    """Fold token/cost from ``secondary`` into ``primary`` (same job_id)."""
    if primary is None or secondary is None or primary is secondary:
        return
    try:
        for attr in ("tokens_in", "tokens_out", "tokens_cached"):
            a = int(getattr(primary, attr, 0) or 0)
            b = int(getattr(secondary, attr, 0) or 0)
            setattr(primary, attr, a + b)
    except Exception:
        pass
    try:
        a_cost = float(getattr(primary, "est_cost_usd", 0.0) or 0.0)
        b_cost = float(getattr(secondary, "est_cost_usd", 0.0) or 0.0)
        primary.est_cost_usd = a_cost + b_cost
    except Exception:
        pass


def _worker_attempt_has_usable_diff(res) -> bool:
    if res is None:
        return False
    try:
        if (getattr(res, "patch", None) or "").strip():
            return True
    except Exception:
        pass
    try:
        if getattr(res, "worktree_diff_empty", None) is False:
            return True
    except Exception:
        pass
    try:
        return bool(getattr(res, "ok", False)) and bool(
            getattr(res, "files_changed", None)
        )
    except Exception:
        return False


def _worker_stopped_by_guard_or_budget(res) -> bool:
    """True when the first attempt was halted by tool/AutoBudget ceilings."""
    if res is None:
        return False
    try:
        if bool(getattr(res, "stopped_by_guard_or_budget", False)):
            return True
    except Exception:
        pass
    try:
        text = (
            f"{getattr(res, 'summary', '') or ''} "
            f"{getattr(res, 'error', '') or ''}"
        ).lower()
    except Exception:
        return False
    markers = (
        "tool-call budget exhausted",
        "per-turn tool-call budget exhausted",
        "post-implement validation allowance exhausted",
        "token ceiling reached",
        "time ceiling reached",
        "edit-first nested implement",
        "halt reason: token ceiling",
        "halt reason: time ceiling",
        "halt reason: stall:",
    )
    return any(m in text for m in markers)


def _empty_implement_recovery_eligible(
    res,
    *,
    expects_diff: bool,
    live_dirty_before: list,
    cancelled: bool,
) -> bool:
    """One-shot recovery only for genuine seeded-worktree/provenance mismatch.

    Guard/budget exhaustion on the first attempt must not launch a second full
    worker with a fresh ceiling.
    """
    if cancelled or not expects_diff or not live_dirty_before:
        return False
    try:
        err = str(getattr(res, "error", "") or "").strip().lower()
    except Exception:
        err = ""
    # Engine crash / unavailable / route failure is not a seeded-worktree miss.
    # Stamping worktree_diff_empty on AGENTIC_ERROR must not launch a second run.
    if err.startswith("agentic_"):
        return False
    if not _is_empty_diff_implement_failure(res, expects_diff=True):
        return False
    if _worker_stopped_by_guard_or_budget(res):
        return False
    return True


def _annotate_empty_managed_implement_exhausted(
    res, *, dirty_paths: list, recovered: bool,
) -> None:
    """Mark a still-empty implement so the pilot must not re-dispatch the same pattern."""
    if res is None or not recovered:
        return
    shown = [str(p) for p in (dirty_paths or [])[:_WORKER_PROVENANCE_PATH_CAP]]
    path_hint = ", ".join(shown) if shown else "none"
    honesty = (
        f"[provenance] {EMPTY_MANAGED_IMPLEMENT_EXHAUSTED}: empty managed "
        "implement already recovered once while live-tree dirty overlap was "
        f"present ({path_hint}). Do NOT call run_implement again with the same "
        "dirty-tree pattern; inspect the seeded paths or change approach."
    )
    try:
        summary = (getattr(res, "summary", None) or "").strip()
        if EMPTY_MANAGED_IMPLEMENT_EXHAUSTED in summary:
            return
        res.summary = f"{honesty}\n{summary}".strip() if summary else honesty
    except Exception:
        pass
    try:
        err = (getattr(res, "error", None) or "").strip()
        if err and EMPTY_MANAGED_IMPLEMENT_EXHAUSTED not in err:
            res.error = f"{err}; {EMPTY_MANAGED_IMPLEMENT_EXHAUSTED}"
        elif not err:
            res.error = EMPTY_MANAGED_IMPLEMENT_EXHAUSTED
    except Exception:
        pass


def _worker_provenance_text(provenance: dict, *, expects_diff: bool = True) -> str:
    """Render measured worker/live-tree facts for pilot-facing text.

    Analysis (expects_diff=False) leaving the disposable worktree unchanged is
    expected for read-only jobs — never word that as a failure cue.
    """
    if not isinstance(provenance, dict) or not provenance:
        return ""
    before = list(provenance.get("live_dirty_paths_before") or [])
    after = list(provenance.get("live_dirty_paths_after") or [])
    mode = str(provenance.get("managed_worktree_mode") or "unknown")
    path = str(provenance.get("managed_worktree_path") or "")
    diff_empty = provenance.get("worktree_diff_empty")
    if diff_empty is True:
        if not expects_diff:
            worker_line = (
                "Analysis left disposable managed worktree unchanged "
                "(expected for read-only; findings are in artifacts/summary)"
            )
        else:
            worker_line = "Worker produced no changes in disposable managed worktree"
    elif diff_empty is False:
        worker_line = "Worker produced changes in disposable managed worktree"
    else:
        worker_line = "Worker worktree diff status unavailable"
    def path_list(paths: list) -> str:
        shown = [str(item) for item in paths[:_WORKER_PROVENANCE_PATH_CAP]]
        suffix = f", +{len(paths) - len(shown)} more" if len(paths) > len(shown) else ""
        return ", ".join(shown) + suffix if shown else "none"
    return (
        f"[provenance] {worker_line}; mode={mode}"
        f"{f', path={path}' if path else ''}. "
        f"User checkout had {len(before)} pre-existing dirty paths before"
        f" ({path_list(before)}) and {len(after)} after ({path_list(after)})."
    )


def _analysis_signal_rows_for_job(res, summary_text: str) -> list:
    """Typed finding/risk/decision rows for an analysis job result.

    Prefers ``WorkerResult.findings`` when present; otherwise parses FINDING/
    RISK/DECISION labels from the worker summary text. Passes through ``body``
    (full multi-line signal) when present so coerced findings keep paragraphs
    beyond the capped headline.
    """
    rows: list = []
    try:
        raw = getattr(res, "findings", None) or []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("type") or "").lower()
                if kind not in ("finding", "risk", "decision"):
                    continue
                headline = str(item.get("headline") or "").strip()
                if not headline:
                    continue
                body = str(item.get("body") or "").strip() or headline
                rows.append({
                    "type": kind,
                    "headline": headline,
                    "body": body,
                })
    except Exception:
        rows = []
    if rows:
        return rows
    try:
        from harness.worker import parse_analysis_signal_rows
        return list(parse_analysis_signal_rows(summary_text or ""))
    except Exception:
        return []


def _background_evidence_boundary(
    session: object,
    job_id: str,
    res_job: dict,
    stamped: dict,
) -> str:
    """Current-run evidence contract for a job that finished in the background.

    A background result lands turns after its dispatch, so without this the
    pilot reads it next to stale conclusions with nothing marking which run
    produced what. Same contract as the synchronous digest; best-effort because
    the drain must never raise on the chat hot path.
    """
    try:
        from .swarm_run_facts import (
            attribute_stored_execution_refs,
            build_swarm_run_facts,
            normalize_execution_refs,
            render_evidence_boundary,
        )

        artifacts = _background_artifacts(res_job, stamped)
        criteria = res_job.get("acceptance_criteria")
        if not criteria and isinstance(stamped, dict):
            criteria = stamped.get("acceptance_criteria")
        subject_cwd = ""
        if isinstance(stamped, dict):
            subject_cwd = str(stamped.get("cwd") or "")
        if not subject_cwd:
            subject_cwd = str(getattr(getattr(session, "config", None), "repo", "") or "")
        return render_evidence_boundary(build_swarm_run_facts(
            job_id=job_id,
            job_status=str(
                res_job.get("status")
                or (stamped.get("status") if isinstance(stamped, dict) else "")
                or ""
            ),
            subject_cwd=subject_cwd,
            state_root=str(getattr(session, "state_dir", "") or ""),
            artifacts=normalize_execution_refs(
                attribute_stored_execution_refs(artifacts, job_id),
                job_id,
            ),
            acceptance_criteria=list(criteria or []),
        ))
    except Exception as exc:
        job = str(job_id or "").strip() or "unknown"
        subject_cwd = ""
        if isinstance(stamped, dict):
            subject_cwd = str(stamped.get("cwd") or "")
        if not subject_cwd:
            subject_cwd = str(getattr(getattr(session, "config", None), "repo", "") or "")
        return (
            "\n"
            "CURRENT-JOB EVIDENCE BOUNDARY:\n"
            f"- Exact current job id: {job}\n"
            f"- Subject cwd (read-only audit target): {subject_cwd or 'unknown'}\n"
            "- Evidence boundary construction failed; treat every criterion as not verified.\n"
            f"- Boundary error: {exc.__class__.__name__}\n"
            "- Acceptance criteria: none settled (boundary unavailable)\n"
            "- Prior transcript audit conclusions are historical/untrusted.\n"
            "- Final claims may use only this job's returned artifacts or explicit "
            "probes run after it.\n"
        )


def _background_artifacts(res_job: dict, stamped: dict) -> list:
    """The rows a finished background job actually produced.

    The queued result usually carries only ``ar_list`` — raw worker rows with no
    ``execution_ref`` — while the settled local job has fully attributed rows,
    because ``_finish_local_job`` runs before the result is enqueued and stamps
    a terminal artifact plus normalized findings. Preferring the attributed
    sources is what keeps a completed run from reporting a misleading ``0/0``;
    the raw list is a last resort so counts stay honest rather than empty.
    """
    stamped_rows = stamped if isinstance(stamped, dict) else {}
    for candidate in (
        stamped_rows.get("artifacts"),
        stamped_rows.get("findings"),
        res_job.get("artifacts"),
        res_job.get("ar_list"),
    ):
        if isinstance(candidate, list) and candidate:
            return candidate
    return []


class ConversationJobsMixin:
    """Mixin holding swarm job await/apply/drain helpers.

    The concrete class (ConversationalSession) supplies the state these
    methods read/write via `self`. This mixin defines no __init__ and no
    instance state of its own.
    """

    def _automatic_patch_apply_refused(self, job_id: str) -> Optional[str]:
        """Refuse automatic live-checkout patch apply after Stop quarantine.

        Checks the per-job cancel Event and the session cooperative quarantine
        (existing cancel / interrupt / stop-idle flags). Intentional
        ``apply_review`` bypasses this helper and only sees the per-job gate
        inside ``_apply_worker_patch``.
        """
        msg = (
            "cancelled: refusing patch apply after Stop "
            "(cooperative quarantine)"
        )
        try:
            if self._local_job_cancelled(job_id):
                return msg
        except Exception:
            pass
        try:
            quarantined = getattr(
                self, "_cooperative_disk_mutations_quarantined", None,
            )
            if callable(quarantined) and quarantined():
                return msg
        except Exception:
            pass
        return None

    def _await_and_apply_job(self, job_id: str, state_dir: Optional[str] = None, objective: str = "") -> dict:
        import json
        import subprocess
        from .repo_resolve import resolve_effective_repo

        # Per-operation only — do not persist the resolved child onto config.repo.
        effective_repo = (
            resolve_effective_repo(self.config.repo or "")
            if (self.config.repo or "").strip()
            else (self.config.repo or "")
        )

        # 1. Await job
        if state_dir:
            await_cmd = _puppetmaster_cmd("--state-dir", state_dir, "await", job_id, "--cwd", effective_repo)
        else:
            await_cmd = _puppetmaster_cmd("await", job_id, "--cwd", effective_repo)
        subprocess.run(await_cmd, cwd=effective_repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)

        # 2. Fetch artifacts
        if state_dir:
            art_cmd = _puppetmaster_cmd("--state-dir", state_dir, "artifacts", job_id, "--cwd", effective_repo)
        else:
            art_cmd = _puppetmaster_cmd("artifacts", job_id, "--cwd", effective_repo)
        art_p = subprocess.run(art_cmd, cwd=effective_repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=60)
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
        for a in artifacts:
            if not isinstance(a, dict):
                continue
            t = a.get("type", "finding")
            headline = ""
            payload = a.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            if t == "patch":
                files = payload.get("files") or []
                headline = f"Patch: modified {', '.join(files)}" if files else "Patch generated"
            elif t == "finding":
                claim = payload.get("claim") or ""
                rep = payload.get("report") or ""
                headline = claim or rep[:80] or "Finding"
            else:
                headline = f"{t.capitalize()} artifact"
            row = {"type": t, "headline": headline}
            for key in ("id", "task_id", "sha256"):
                val = a.get(key)
                if val in (None, ""):
                    val = payload.get(key)
                if val not in (None, ""):
                    row[key] = val
            ar_list.append(row)

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
            refused = self._automatic_patch_apply_refused(job_id)
            if refused:
                applied, applied_files, apply_msg = False, [], refused
                cp_id = None
            else:
                with self._apply_lock:
                    applied, applied_files, apply_msg = self._apply_worker_patch(
                        artifacts, job_id,
                    )
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
        agentic_pin=None, strict_adapter: bool = False,
    ) -> None:
        from .conversation import append_failed_declarative_checks_summary

        live_repo = target_repo or self.config.repo or ""
        live_dirty_before: list[str] = []
        live_dirty_after: list[str] = []
        try:
            from harness.worker import WorkerResult
            from harness.worktree_seed import _list_git_status_porcelain_paths

            try:
                live_dirty_before = _list_git_status_porcelain_paths(live_repo)
            except Exception:
                live_dirty_before = []

            # Bounded run so a wedged worker frees its _swarm_pool slot on the
            # hard deadline instead of occupying it forever (audit finding #4).
            # target_repo (optional): abs path to a DIFFERENT git repo than the
            # open workspace; swaps self.config for a shallow-copied per-dispatch
            # HarnessConfig so the engines transparently target that repo.
            #
            # One shared AutoBudget covers the primary attempt AND any one-shot
            # dirty-checkout recovery so we never merge two fresh full ceilings.
            from harness.autobudget import AutoBudget
            from pmharness.bridge import worker_token_budget

            try:
                deadline_s = float(self._worker_deadline_seconds() or 0.0)
            except Exception:
                deadline_s = 900.0
            # Prefer the governing fully-auto budget when present so recovery
            # cannot escape the tree ceiling; otherwise mint one shared lifecycle.
            lifecycle_budget = getattr(self, "_auto_budget", None)
            if lifecycle_budget is None:
                lifecycle_budget = AutoBudget(
                    max_tokens=worker_token_budget(),
                    max_seconds=int(deadline_s) if deadline_s > 0 else 900,
                    max_swarms=2,
                    max_idle_steps=2 if expects_diff else 5,
                ).start()

            def _on_worker_event(ev):
                try:
                    self._upsert_local_job_action(job_id, ev)
                except Exception:
                    pass

            res = self._run_edit_worker_bounded(
                objective, requested_adapter, job_id=job_id,
                target_repo=target_repo, expects_diff=expects_diff,
                on_event=_on_worker_event,
                agentic_pin=agentic_pin,
                strict_adapter=strict_adapter,
                lifecycle_budget=lifecycle_budget,
                deadline_seconds=deadline_s if deadline_s > 0 else None,
            )
            try:
                live_dirty_after = _list_git_status_porcelain_paths(live_repo)
            except Exception:
                live_dirty_after = []
            if res is None:
                deadline = int(self._worker_deadline_seconds())
                res = WorkerResult(
                    ok=False,
                    error=f"worker exceeded {deadline}s wall-clock deadline",
                    summary=f"Worker exceeded its {deadline}s deadline and was abandoned to free the pool slot.",
                    stopped_by_guard_or_budget=True,
                )

            # One automatic recovery when implement left the managed worktree
            # empty while the live checkout was already dirty (seeded files
            # present but unused). Analysis empty-diff is fine — skip.
            # Do NOT recover after guard/budget exhaustion (would double spend).
            recovered_empty_implement = False
            try:
                should_recover = _empty_implement_recovery_eligible(
                    res,
                    expects_diff=expects_diff,
                    live_dirty_before=live_dirty_before,
                    cancelled=self._local_job_cancelled(job_id),
                )
            except Exception:
                should_recover = False
            if not should_recover:
                # Guard/budget exhaustion correctly skips automatic recovery, but
                # must still annotate exhausted so check_implement_exhausted
                # soft-refuses a reformulated re-dispatch (same as lifecycle_halted).
                try:
                    if (
                        expects_diff
                        and live_dirty_before
                        and not self._local_job_cancelled(job_id)
                        and _is_empty_diff_implement_failure(res, expects_diff=True)
                        and _worker_stopped_by_guard_or_budget(res)
                    ):
                        recovered_empty_implement = True
                        _annotate_empty_managed_implement_exhausted(
                            res,
                            dirty_paths=live_dirty_before,
                            recovered=True,
                        )
                except Exception:
                    pass
            if should_recover:
                first_res = res
                recovery_objective = _empty_implement_recovery_objective(
                    objective, live_dirty_before,
                )
                # Shared lifecycle budget: do not start recovery when the
                # primary attempt already consumed the total ceiling.
                lifecycle_halted = False
                try:
                    lifecycle_halted = bool(lifecycle_budget.check())
                except Exception:
                    lifecycle_halted = False
                try:
                    remaining = max(
                        1.0,
                        float(lifecycle_budget.max_seconds)
                        - float(lifecycle_budget.elapsed),
                    )
                except Exception:
                    remaining = deadline_s if deadline_s > 0 else None
                if lifecycle_halted:
                    # Do not burn a second attempt against a spent ceiling, but
                    # still mark the empty dirty implement exhausted so the
                    # pilot soft-refuses an identical re-dispatch.
                    should_recover = False
                    recovered_empty_implement = True
                    _annotate_empty_managed_implement_exhausted(
                        res,
                        dirty_paths=live_dirty_before,
                        recovered=True,
                    )
                else:
                    try:
                        recovery_res = self._run_edit_worker_bounded(
                            recovery_objective, requested_adapter, job_id=job_id,
                            target_repo=target_repo, expects_diff=expects_diff,
                            on_event=_on_worker_event,
                            agentic_pin=agentic_pin,
                            strict_adapter=strict_adapter,
                            lifecycle_budget=lifecycle_budget,
                            deadline_seconds=remaining,
                        )
                    except Exception:
                        recovery_res = None
                    try:
                        live_dirty_after = _list_git_status_porcelain_paths(live_repo)
                    except Exception:
                        pass
                    if recovery_res is None:
                        # Deadline/cancel on recovery: keep the first attempt, but
                        # still mark exhausted so the pilot does not re-dispatch.
                        res = first_res
                        recovered_empty_implement = True
                        _annotate_empty_managed_implement_exhausted(
                            res,
                            dirty_paths=live_dirty_before,
                            recovered=True,
                        )
                    elif _worker_attempt_has_usable_diff(recovery_res):
                        _merge_worker_attempt_usage(recovery_res, first_res)
                        res = recovery_res
                    else:
                        # Still empty — prefer recovery surface (honest about retry)
                        # but fold first-attempt spend onto the same job_id.
                        _merge_worker_attempt_usage(recovery_res, first_res)
                        res = recovery_res
                        recovered_empty_implement = True
                        _annotate_empty_managed_implement_exhausted(
                            res,
                            dirty_paths=live_dirty_before,
                            recovered=True,
                        )

            def _worker_attribute(name: str, default=""):
                try:
                    return getattr(res, name, default)
                except Exception:
                    return default

            provenance = {
                "live_dirty_paths_before": list(live_dirty_before),
                "live_dirty_paths_after": list(live_dirty_after),
                "managed_worktree_path": str(
                    _worker_attribute("managed_worktree_path")
                    or _worker_attribute("worktree")
                    or ""
                ),
                "managed_worktree_mode": str(
                    _worker_attribute("managed_worktree_mode")
                    or ("managed" if _worker_attribute("worktree") else "unknown")
                ),
                "worktree_diff_empty": _worker_attribute("worktree_diff_empty", None),
                "empty_implement_recovery": bool(should_recover),
                "empty_managed_implement_exhausted": bool(
                    recovered_empty_implement
                    and _is_empty_diff_implement_failure(res, expects_diff=expects_diff)
                ),
            }
            res.live_dirty_paths_before = list(live_dirty_before)
            res.live_dirty_paths_after = list(live_dirty_after)
            res.managed_worktree_path = provenance["managed_worktree_path"]
            res.managed_worktree_mode = provenance["managed_worktree_mode"]
            res.worktree_diff_empty = provenance["worktree_diff_empty"]
            if self._local_job_cancelled(job_id):
                # The cancel path already created the terminal record. Enrich it
                # with facts from the late worker result, but never reopen it,
                # apply its patch, meter its spend twice, or enqueue a result.
                self._update_cancelled_local_job_provenance(
                    job_id,
                    worker_provenance=provenance,
                    tokens=(
                        int(_worker_attribute("tokens_in", 0) or 0)
                        + int(_worker_attribute("tokens_out", 0) or 0)
                    ),
                    est_cost_usd=float(_worker_attribute("est_cost_usd", 0.0) or 0.0),
                )
                return
            raw_worker_summary = res.summary or ""
            try:
                from harness.provenance_sanitize import sanitize_clean_tree_claims
                res.summary = sanitize_clean_tree_claims(
                    raw_worker_summary, provenance=provenance,
                )
            except Exception:
                pass
            provenance_text = _worker_provenance_text(
                provenance, expects_diff=expects_diff,
            )
            if provenance_text:
                res.summary = f"{provenance_text}\n{res.summary}".strip()

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
                            real_cost_usd=float(getattr(res, "est_cost_usd", 0.0) or 0.0),
                            tokens_cached=_nc_t_cached)
                # Analysis failures are findings/contract failures, not missing patches.
                _fail_fallback = (
                    "Analysis failed"
                    if not expects_diff
                    else "Worker failed to produce patch"
                )
                failed_files = [
                    str(path) for path in (getattr(res, "files_changed", None) or [])
                    if str(path).strip()
                ]
                res_dict = {
                    "job_id": job_id,
                    "applied": False,
                    "files": failed_files,
                    "tokens_in": _nc_t_in,
                    "tokens_out": _nc_t_out,
                    "tokens_cached": _nc_t_cached,
                    "summary": append_failed_declarative_checks_summary(
                        res.summary or res.error or _fail_fallback,
                        getattr(res, "declarative_checks", None),
                    ),
                    "error": res.error,
                    "artifacts": [],
                    "has_patch_art": False,
                    "apply_msg": res.error or _fail_fallback,
                    "num_artifacts": 0,
                    "artifact_types": [],
                    "ar_list": []
                }
            elif not expects_diff or not (res.patch or "").strip():
                # Analysis/review (expects_diff=False): findings-only — never
                # apply a patch even if finalize left seed noise in res.patch.
                # Empty-patch implement also lands here for the no-op path.
                if not expects_diff:
                    res.patch = ""
                    res.files_changed = []
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
                        real_cost_usd=float(getattr(res, "est_cost_usd", 0.0) or 0.0),
                        tokens_cached=tokens_cached)
                summary = res.summary or "Successfully completed analysis task"
                substantive = True
                if not expects_diff:
                    try:
                        from harness.pilot_guards import analysis_summary_is_substantive
                        # Machine-generated provenance is diagnostic context, not
                        # evidence that an analysis found something substantive.
                        substantive = analysis_summary_is_substantive(raw_worker_summary)
                    except Exception:
                        substantive = bool(raw_worker_summary.strip())
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
                    # Persist typed FINDING/RISK/DECISION rows onto the job so
                    # artifact:// readers see structured analysis, not only prose.
                    signal_rows = _analysis_signal_rows_for_job(
                        res, raw_worker_summary or summary,
                    )
                    artifacts = [
                        {
                            "type": row["type"],
                            "payload": {
                                "claim": row.get("headline") or "",
                                "report": (
                                    row.get("body")
                                    or row.get("headline")
                                    or ""
                                ),
                            },
                        }
                        for row in signal_rows
                    ]
                    ar_list = [
                        {
                            "type": row["type"],
                            "headline": row.get("headline") or "",
                            "body": (
                                row.get("body")
                                or row.get("headline")
                                or ""
                            ),
                        }
                        for row in signal_rows
                    ]
                    artifact_types = sorted({
                        str(row.get("type") or "")
                        for row in signal_rows
                        if row.get("type")
                    })
                    # applied means patch landed — analysis accepts findings with
                    # no diff. analysis_ok keeps drain/resume from treating that
                    # as a failed apply (empty files + applied=False).
                    res_dict = {
                        "job_id": job_id,
                        "applied": False if not expects_diff else True,
                        "files": [],
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "tokens_cached": tokens_cached,
                        "summary": summary,
                        "error": None,
                        "artifacts": artifacts,
                        "has_patch_art": False,
                        "apply_msg": "",
                        "num_artifacts": len(artifacts),
                        "artifact_types": artifact_types,
                        "ar_list": ar_list,
                    }
                    if not expects_diff:
                        res_dict["analysis_ok"] = True
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
                        real_cost_usd=float(getattr(res, "est_cost_usd", 0.0) or 0.0),
                        tokens_cached=tokens_cached)

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
                    refused = self._automatic_patch_apply_refused(job_id)
                    if refused:
                        applied, applied_files, apply_msg = False, [], refused
                        cp_id = None
                    else:
                        with self._apply_lock:
                            applied, applied_files, apply_msg = (
                                self._apply_worker_patch(artifacts, job_id)
                            )
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

            res_dict["worker_provenance"] = provenance
            for routing_key, routing_value in (
                ("adapter", getattr(res, "engine", "")),
                ("model", getattr(res, "model", "")),
                ("requested_model", getattr(res, "requested_model", "")),
                ("provider", getattr(res, "provider", "")),
                ("routing_policy", getattr(res, "routing_policy", "")),
            ):
                normalized_value = str(routing_value or "").strip()
                if normalized_value:
                    res_dict[routing_key] = normalized_value

            # Always fold completed WorkerResult.events into job['actions']
            # (progressive callback may have already recorded most of them).
            try:
                self._ingest_local_job_events(job_id, getattr(res, "events", None))
            except Exception as exc:
                try:
                    from harness.diag import note as _diag_note
                    _diag_note(
                        "conversation_jobs.ingest_local_job_events",
                        exc,
                        msg=f"job_id={job_id}",
                    )
                except Exception:
                    pass

            wr_engine = (getattr(res, "engine", None) or "").strip()
            wr_model = (getattr(res, "model", None) or "").strip()
            # Carry real structured rows (never the patch/plumbing ones) onto the
            # sidecar so artifact:// readers see the analysis, and pass the diff so
            # a patch artifact is only claimed when one actually exists.
            _signal_rows = [
                row for row in (res_dict.get("ar_list") or [])
                if isinstance(row, dict)
                and str(row.get("type") or "") in ("finding", "risk", "decision")
            ]
            self._swarm_results.put({
                "job_id": job_id,
                "objective": objective,
                "result": res_dict,
                "state_dir": None
            })
            # Queue the chat continuation before publishing terminal tracker
            # status. Otherwise swarm/live can prune the final pending id and
            # disable the only drain poll while this result is not yet visible.
            self._finish_local_job(
                job_id,
                ok=not res_dict.get("error"),
                summary=res_dict.get("summary", ""),
                files=res_dict.get("files") or [],
                tokens=res_dict.get("tokens_out", 0) + res_dict.get("tokens_in", 0),
                est_cost_usd=float(getattr(res, "est_cost_usd", 0.0) or 0.0),
                engine=wr_engine,
                model=wr_model,
                findings=_signal_rows,
                diff=(getattr(res, "patch", "") or ""),
                worker_provenance=provenance,
            )
            # Finish stamps analysis reuse onto the local job. Mutate the same
            # queued res_dict so a drain that has not popped yet still sees it.
            try:
                stamped = (getattr(self, "_local_jobs", {}) or {}).get(job_id) or {}
                for _rk in (
                    "reuse_status",
                    "source_job_id",
                    "validation_fingerprint",
                    "environment_fingerprint",
                    "invalidated_paths",
                    "reuse_reason",
                    "acceptance_criteria",
                    "financial_receipt",
                ):
                    if stamped.get(_rk) not in (None, "", [], {}):
                        res_dict[_rk] = stamped[_rk]
            except Exception:
                pass

        except Exception as e:
            try:
                from harness.worktree_seed import _list_git_status_porcelain_paths
                live_dirty_after = _list_git_status_porcelain_paths(live_repo)
            except Exception:
                live_dirty_after = []
            failure_provenance = {
                "live_dirty_paths_before": list(live_dirty_before),
                "live_dirty_paths_after": list(live_dirty_after),
                "managed_worktree_path": "",
                "managed_worktree_mode": "unknown",
                "worktree_diff_empty": None,
            }
            if self._local_job_cancelled(job_id):
                # A failure while collecting late facts must not turn a
                # cancelled job into a failed/completed result or enqueue one.
                self._update_cancelled_local_job_provenance(
                    job_id,
                    worker_provenance=failure_provenance,
                )
                return
            failure_text = _worker_provenance_text(failure_provenance)
            failure_summary = f"{failure_text}\nFailed background worker: {e}".strip()
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
                    "ar_list": [],
                    "worker_provenance": failure_provenance,
                },
                "state_dir": None
            })
            # The result queue owns the durable/visible terminal continuation;
            # publish failed tracker state only after that continuation exists.
            self._finish_local_job(
                job_id, ok=False, summary=failure_summary,
                worker_provenance=failure_provenance,
            )
        finally:
            # Free the objective for legitimate future dispatch regardless of
            # how this worker settled (applied, failed, or crashed).
            self._release_objective(objective)

    def _try_recover_busy_for_swarm_drain(self) -> bool:
        """Force-release a stale/abandoned ``_busy`` so queued swarm results can drain.

        Mirrors ``send()`` stale-lock recovery: a healthy in-flight turn must keep
        the lock (end-of-stream drain will surface results). Abandoned holders that
        already report idle / Stop / interrupt must not strand ``swarm_result`` +
        ``pilot_resume`` after ``has_pending_swarms`` clears. Returns True when the
        lock was released (caller should acquire again).
        """
        import time as _t

        try:
            if self._swarm_results.empty():
                return False
        except Exception:
            return False

        held_for = _t.monotonic() - self._busy_since if self._busy_since else 0.0
        stop_idle = bool(getattr(self, "_stop_holds_idle", False))
        interrupt = bool(getattr(self, "_interrupt_requested", False))
        state = getattr(self, "_state", "") or ""

        # Stop reports idle while the abandoned generator may still hold the lock.
        stale = bool(stop_idle and self._busy.locked())
        # Explicit interrupt: same shorter grace as send() force-recover.
        if not stale and interrupt and self._busy_since and held_for > 0.5:
            stale = True
        # Leaked lock with idle state (send() uses the same 1.5s heuristic).
        if not stale and self._busy_since and held_for > 1.5 and state == "idle":
            stale = True
        # Pause-point / masked busy: no inflight futures, session reports not-busy,
        # but the lock is still held — results would strand forever once FE prunes
        # pending ids after has_pending_swarms clears.
        if not stale and self._busy_since and held_for > 1.5:
            pending = True
            try:
                pending = bool(self.has_pending_swarms())
            except Exception:
                pending = True
            try:
                reports_busy = bool(self.is_turn_busy())
            except Exception:
                reports_busy = True
            if not pending and not reports_busy:
                stale = True

        if not stale:
            return False

        with self._busy_meta:
            self._busy_gen += 1
            self._busy_since = 0.0
            try:
                self._busy.release()
            except RuntimeError:
                pass
        try:
            from harness.diag import note as _diag_note
            _diag_note(
                "conversation_jobs.drain_busy_recover",
                msg=f"released stale _busy after {held_for:.1f}s for queued swarm drain",
            )
        except Exception:
            pass
        return True

    def drain_swarm_results(
        self,
        emit_resume: bool = True,
        already_holding_busy: bool = False,
    ) -> Iterator[ConvEvent]:
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
        # Additionally, when results are already queued and the holder is stale /
        # Stop-abandoned / reports not-busy, recover the lock immediately so idle
        # FE keep-alive can receive swarm_result + pilot_resume without waiting
        # for the 600s reaper (busy-held starve after has_pending_swarms clears).
        from .conversation import ConvEvent

        self._reap_stuck_turn()
        acquired_here = False
        if not already_holding_busy:
            if not self._busy.acquire(blocking=False):
                if not self._try_recover_busy_for_swarm_drain():
                    return
                if not self._busy.acquire(blocking=False):
                    return
            acquired_here = True
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

                    # Stamped local job carries the subject cwd / criteria the
                    # dispatch recorded; needed for both the evidence boundary
                    # and the reuse fields folded onto the display card below.
                    try:
                        stamped = (getattr(self, "_local_jobs", {}) or {}).get(job_id) or {}
                    except Exception:
                        stamped = {}

                    # Append a labeled follow-up assistant message to self._history (SINGLE-WRITER held via _busy lock!)
                    applied = res_job["applied"]
                    applied_files = res_job["files"]
                    summary = res_job["summary"]
                    provenance_text = _worker_provenance_text(
                        res_job.get("worker_provenance") or {}
                    )
                    if provenance_text and provenance_text not in summary:
                        summary = f"{provenance_text}\n{summary}".strip()
                    held_for_review = bool(res_job.get("held_for_review"))
                    # Green analysis: applied=False by design (no patch). Do not
                    # conflate findings-accepted with failed apply for resume-cap.
                    analysis_ok = bool(res_job.get("analysis_ok")) and not res_job.get("error")
                    failed = bool(
                        res_job.get("error")
                        or (not applied and not held_for_review and not analysis_ok)
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
                        msg_content = f"[swarm result for: {objective}]"
                        if applied and applied_files:
                            msg_content += f"; applied {len(applied_files)} files"
                        elif held_for_review:
                            msg_content += "; held for review"
                        elif res_job.get("has_patch_art") and not applied:
                            msg_content += f"; patch failed to apply: {res_job.get('apply_msg')}"
                        display_error = res_job.get("error") or None

                    try:
                        from harness.send_loop_dispatch import (
                            _render_swarm_delivery_manifest,
                            _swarm_artifact_delivery,
                        )
                        snapshot = list(_background_artifacts(res_job, stamped))
                        delivered, delivery = _swarm_artifact_delivery(
                            self, job_id, snapshot, require_store=False,
                        )
                        if isinstance(res_job, dict):
                            res_job["artifacts"] = delivered
                            res_job["artifact_delivery"] = delivery
                        try:
                            with self._local_jobs_lock:
                                local = (getattr(self, "_local_jobs", {}) or {}).get(job_id)
                                if isinstance(local, dict):
                                    local["artifact_delivery"] = delivery
                                    if delivered and not local.get("artifacts"):
                                        local["artifacts"] = list(delivered)
                        except Exception:
                            pass
                        manifest = _render_swarm_delivery_manifest(
                            job_id, delivered, delivery,
                        )
                        if manifest:
                            msg_content = f"{msg_content}\n{manifest}"
                    except Exception:
                        delivered = []
                        delivery = None

                    boundary = _background_evidence_boundary(
                        self, job_id, res_job, stamped,
                    )
                    if boundary:
                        msg_content = f"{msg_content}\n{boundary}"
                    self._history.append({"role": "assistant", "content": msg_content})

                    # Persist the outcome to the display transcript so the green/red
                    # "swarm done / swarm failed" badge survives a session reload or
                    # app restart -- the live ConvEvent below only reaches a renderer
                    # that is open right now.
                    display_result = {
                        "type": "swarm_result",
                        "job_id": job_id,
                        "applied": bool(applied),
                        "files": list(applied_files or []),
                        "summary": summary or "",
                        "error": display_error,
                        "objective": objective,
                        # Durable badge honesty across reload/reattach — FE must
                        # not paint held_for_review / analysis_ok as applied or failed.
                        "held_for_review": bool(held_for_review),
                        "analysis_ok": bool(analysis_ok),
                    }
                    if delivery is not None:
                        display_result["artifacts"] = delivered
                        display_result["artifact_delivery"] = delivery
                    worker_provenance = res_job.get("worker_provenance") or {}
                    if worker_provenance:
                        display_result["worker_provenance"] = worker_provenance
                    try:
                        from harness.pilot_guards import (
                            note_implement_exhausted_from_provenance,
                            note_implement_success_from_job_result,
                        )

                        guard_state = getattr(self, "_turn_guard_state", None)
                        if guard_state is not None:
                            note_implement_exhausted_from_provenance(
                                guard_state, worker_provenance,
                            )
                            note_implement_success_from_job_result(
                                guard_state, res_job, stamped,
                            )
                    except Exception:
                        pass
                    # Prefer fields already on the result; fall back to stamped
                    # local job so transcript hydrate keeps reuse provenance.
                    for _rk in (
                        "reuse_status",
                        "source_job_id",
                        "validation_fingerprint",
                        "environment_fingerprint",
                        "invalidated_paths",
                        "reuse_reason",
                        "acceptance_criteria",
                    ):
                        value = res_job.get(_rk)
                        if value in (None, "", [], {}):
                            value = stamped.get(_rk) if isinstance(stamped, dict) else None
                        if value not in (None, "", [], {}):
                            display_result[_rk] = value
                            if isinstance(res_job, dict) and res_job.get(_rk) in (None, "", [], {}):
                                res_job[_rk] = value
                    self._display_transcript.append(display_result)
                    if delivery is not None:
                        try:
                            self._note_parallel_child_receipt(job_id)
                        except Exception:
                            pass
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
                    if (
                        not degraded
                        and not failed
                        and not analysis_ok
                        and not (applied_files or [])
                    ):
                        # Empty-diff "success" with no substantive summary is
                        # treated as degraded for resume-cap purposes. Green
                        # analysis (analysis_ok) already passed the substantive
                        # gate at produce time — do not re-degrade it here.
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

                    if emit_resume:
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
                            if emit_resume:
                                yield ConvEvent("pilot_resume", {
                                    "job_id": job_id,
                                    "objective": objective,
                                })
                        except Exception:
                            pass
        finally:
            if acquired_here:
                self._busy.release()
