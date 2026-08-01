"""Opt-in durable background ``run_command`` jobs on the local-job seam.

Wave 2 of chat-loop resilience: an explicit ``PilotAction.background`` flag
registers a Marionette-owned local command job *before* process launch and
returns a pending receipt without holding the pilot turn open. Foreground
``run_command`` stays on the synchronous ``_do_run_command`` path.

Never infers background from duration, timeout, or command text.
"""
from __future__ import annotations

import hashlib
import threading
import uuid
from typing import Any, Dict, Optional

from harness.api.redaction import redact_secret_text
from harness.job_scoping import ACCOUNTING_SCOPE_MARIONETTE

# Contract: artifacts/chat_loop_resilience_contract.json durable_job_states.
COMMAND_JOB_STATES = frozenset({
    "registered",
    "running",
    "completed",
    "failed",
    "cancelled",
    "timeout",
    "truncated",
})
COMMAND_TERMINAL_STATES = frozenset({
    "completed",
    "failed",
    "cancelled",
    "timeout",
    "truncated",
})

# Adapter/role labels that must NOT read as provider-swarm workers.
COMMAND_JOB_ROLE = "command"
COMMAND_JOB_ADAPTER = "command"
COMMAND_JOB_KIND = "run_command"

# Inline output cap for pending/terminal receipts (matches foreground 50 KiB
# capture budget in the contract; spill when larger).
_INLINE_OUTPUT_CAP = 50 * 1024
_COMMAND_PREVIEW_CHARS = 160


def command_fingerprint(command: str) -> str:
    """Stable secret-free identity for a command string (sha256 hex)."""
    raw = (command or "").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def secret_free_command_preview(command: str, *, max_chars: int = _COMMAND_PREVIEW_CHARS) -> str:
    """Redacted, bounded command text safe for receipts / API projection."""
    cleaned = redact_secret_text(command or "").replace("\n", " ").strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3] + "..."


def is_background_run_command(act: Any) -> bool:
    """True only when the pilot explicitly opted into background mode."""
    if act is None:
        return False
    kind = str(getattr(act, "kind", "") or "").strip()
    if kind != "run_command":
        return False
    return bool(getattr(act, "background", False))


def build_pending_receipt(
    job: Dict[str, Any],
    *,
    include_output: bool = True,
) -> Dict[str, Any]:
    """Durable pending/terminal receipt projected from a command job row."""
    status = str(job.get("status") or "registered")
    receipt: Dict[str, Any] = {
        "job_id": str(job.get("id") or ""),
        "session_id": str(job.get("session_id") or ""),
        "action_id": str(job.get("action_id") or ""),
        "command_fingerprint": str(job.get("command_fingerprint") or ""),
        "command_preview": str(job.get("command_preview") or ""),
        "cwd": str(job.get("cwd") or ""),
        "started_at": job.get("started_at") or job.get("created_at"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "status": status,
        "kind": COMMAND_JOB_KIND,
        "job_kind": COMMAND_JOB_KIND,
        "role": COMMAND_JOB_ROLE,
        "adapter": COMMAND_JOB_ADAPTER,
        "source": str(job.get("source") or "harness"),
        "accounting_owned": bool(job.get("accounting_owned", True)),
        "accounting_scope": str(
            job.get("accounting_scope") or ACCOUNTING_SCOPE_MARIONETTE
        ),
        "terminal_receipt": job.get("terminal_receipt"),
    }
    if include_output:
        # Public spill metadata is spill_uri + preview flags only. The local
        # filesystem spill_path stays internal to the job row.
        if job.get("spill_uri") or job.get("spill_path"):
            receipt["output"] = str(job.get("output_preview") or "")
            receipt["spill_uri"] = job.get("spill_uri") or ""
            receipt["output_spilled"] = True
            receipt["output_chars"] = int(job.get("output_chars") or 0)
        else:
            receipt["output"] = str(job.get("output") or job.get("output_preview") or "")
            receipt["output_spilled"] = False
            if job.get("output_chars") is not None:
                receipt["output_chars"] = int(job.get("output_chars") or 0)
    if job.get("exit_code") is not None:
        receipt["exit_code"] = job.get("exit_code")
    return receipt


def lookup_command_job(session: Any, job_id: str) -> Optional[Dict[str, Any]]:
    """Restart-safe lookup of a command job from the local-jobs store."""
    getter = getattr(session, "get_local_job", None)
    if callable(getter):
        job = getter(job_id)
    else:
        jobs = getattr(session, "_local_jobs", None) or {}
        lock = getattr(session, "_local_jobs_lock", None)
        if lock is not None:
            with lock:
                job = dict(jobs.get(job_id) or {}) if job_id in jobs else None
        else:
            job = dict(jobs.get(job_id) or {}) if job_id in jobs else None
    if not isinstance(job, dict) or not job:
        return None
    if str(job.get("job_kind") or "") != COMMAND_JOB_KIND:
        # Allow role/adapter stamped command jobs without job_kind (reload).
        if str(job.get("role") or "") != COMMAND_JOB_ROLE:
            return None
    return job


def command_job_recovery_state(job: Optional[Dict[str, Any]]) -> str:
    """Classify a command job for restart / SSE-reattach recovery.

    Returns one of:
    - ``terminal`` — durable receipt present; never rerun
    - ``recoverable_running`` — launch checkpointed / running in a live process
    - ``registered_unlaunched`` — registered without launch checkpoint
    - ``needs_heal`` — unfinished without enough durable facts
    - ``unknown`` — not a command-job row
    """
    if not isinstance(job, dict) or not job:
        return "unknown"
    if str(job.get("job_kind") or "") != COMMAND_JOB_KIND and str(
        job.get("role") or ""
    ) != COMMAND_JOB_ROLE:
        return "unknown"
    status = str(job.get("status") or "").strip()
    receipt = job.get("terminal_receipt")
    if isinstance(receipt, dict) and str(receipt.get("status") or "") in COMMAND_TERMINAL_STATES:
        return "terminal"
    if status in COMMAND_TERMINAL_STATES:
        return "terminal"
    has_launch = isinstance(job.get("launch_checkpoint"), dict)
    if status == "running" and has_launch:
        return "recoverable_running"
    if status == "registered" and has_launch:
        # Checkpointed but process may not have flipped to running yet —
        # still recoverable in the live process; restart heals to terminal.
        return "recoverable_running"
    if status == "registered" and not has_launch:
        return "registered_unlaunched"
    return "needs_heal"


def launch_registered_command_job(
    session: Any,
    job_id: str,
    command: str,
    cwd: str,
) -> bool:
    """Start the daemon thread that executes a registered command job.

    Persists a launch checkpoint *before* the thread (and therefore before any
    child process) can start. Returns False when the job is missing, already
    terminal, or otherwise must not launch — duplicate/late launches never
    reopen settled children.

    Never submits to the provider-swarm pool — command jobs are Marionette-
    owned process work with their own cooperative cancel Event.
    """
    checkpoint = getattr(session, "_checkpoint_command_job_launch", None)
    if callable(checkpoint):
        if not checkpoint(job_id):
            return False
    else:
        # Minimal hosts without the mixin still refuse terminal relaunch.
        existing = lookup_command_job(session, job_id)
        if existing is None:
            return False
        if command_job_recovery_state(existing) == "terminal":
            return False
    short = str(job_id or "").rsplit("-", 1)[-1] or "cmd"
    threading.Thread(
        target=_run_registered_command_job,
        args=(session, job_id, command, cwd),
        daemon=True,
        name=f"pmh-cmd-{short}",
    ).start()
    return True


def start_background_run_command(
    session: Any,
    act: Any,
    action_id: str,
) -> Dict[str, Any]:
    """Register a command job before launch; return a durable pending receipt.

    Registration is persisted before the background thread is started so a
    crash/restart after register still leaves a restart-safe row.
    """
    if not is_background_run_command(act):
        raise ValueError("start_background_run_command requires act.background=True")
    command = str(getattr(act, "command", "") or "").strip()
    if not command:
        raise ValueError("run_command requires a non-empty command")
    repo = str(getattr(getattr(session, "config", None), "repo", "") or "").strip()
    if not repo:
        raise ValueError("No workspace directory (config.repo) is open.")

    register = getattr(session, "_register_command_job", None)
    if not callable(register):
        raise RuntimeError("session does not support _register_command_job")

    short = uuid.uuid4().hex[:8]
    job_id = f"local-cmd-{short}"
    fingerprint = command_fingerprint(command)
    preview = secret_free_command_preview(command)
    job = register(
        job_id,
        command=command,
        action_id=action_id,
        command_fingerprint=fingerprint,
        command_preview=preview,
        cwd=repo,
    )
    receipt = build_pending_receipt(job, include_output=False)
    receipt["status"] = "pending"
    receipt["message"] = (
        f"Background command registered as job {job_id}; "
        "query the job for the terminal receipt."
    )

    # Launch only after durable registration + launch checkpoint. Daemon
    # thread — not the swarm pool — so command jobs never count as
    # provider-swarm capacity.
    try:
        launched = launch_registered_command_job(session, job_id, command, repo)
        if not launched:
            # Already terminal or missing — surface the durable row, do not
            # invent a second receipt.
            live = lookup_command_job(session, job_id) or job
            return build_pending_receipt(live, include_output=False)
    except Exception as exc:
        finish = getattr(session, "_finish_command_job", None)
        if callable(finish):
            finish(
                job_id,
                status="failed",
                summary=f"Failed to start background command: {exc}",
                exit_code=-1,
                output="",
            )
        receipt["status"] = "failed"
        receipt["terminal_receipt"] = {
            "status": "failed",
            "summary": f"Failed to start background command: {exc}",
            "exit_code": -1,
        }
        receipt["message"] = receipt["terminal_receipt"]["summary"]
    return receipt


def _run_registered_command_job(
    session: Any,
    job_id: str,
    command: str,
    cwd: str,
) -> None:
    """Thread body: mark running, execute, persist terminal receipt."""
    # Late/duplicate workers must not reopen a child that already settled.
    existing = lookup_command_job(session, job_id)
    if existing is not None and command_job_recovery_state(existing) == "terminal":
        return

    mark_running = getattr(session, "_mark_command_job_running", None)
    if callable(mark_running):
        mark_running(job_id)
        # mark_running is a no-op when already terminal; re-check before exec.
        existing = lookup_command_job(session, job_id)
        if existing is not None and command_job_recovery_state(existing) == "terminal":
            return

    # Full-auto danger gate: reuse the same classify path as foreground.
    # Background never bypasses the auto command guard.
    if getattr(session, "_auto_mode", False) and getattr(session, "_auto_command_guard", None):
        try:
            from harness.command_policy import classify_command

            verdict = classify_command(command)
            cmd_hash = hashlib.sha256(command.encode("utf-8")).hexdigest()
            approved = False
            consume = getattr(session, "consume_command_approval", None)
            if verdict.danger:
                if callable(consume):
                    approved = bool(consume(cmd_hash))
                else:
                    approved_set = getattr(session, "_approved_commands", set())
                    if cmd_hash in approved_set:
                        approved_set.discard(cmd_hash)
                        approved = True
            else:
                approved = True
            if verdict.danger and not approved:
                finish = getattr(session, "_finish_command_job", None)
                if callable(finish):
                    finish(
                        job_id,
                        status="failed",
                        summary=(
                            f"BLOCKED in full-auto: command matches "
                            f"'{verdict.category}' ({verdict.reason})"
                        ),
                        exit_code=-1,
                        output="",
                    )
                return
        except Exception:
            pass

    cancel_event = None
    cancels = getattr(session, "_local_job_cancels", None)
    if isinstance(cancels, dict):
        cancel_event = cancels.get(job_id)

    # A cancel request that arrives before process launch must not run the
    # command. run_cancellable treats a pre-set Event as stale (edge-triggered
    # against sibling-stream poison), so gate here instead.
    if getattr(session, "_local_job_cancelled", lambda _jid: False)(job_id):
        finish = getattr(session, "_finish_command_job", None)
        if callable(finish):
            finish(
                job_id,
                status="cancelled",
                summary="Cancelled before start",
                exit_code=-1,
                output="",
            )
        return

    from harness.command_policy import resolve_timeout, run_cancellable

    try:
        output, exit_code, run_status = run_cancellable(
            command,
            cwd=cwd,
            timeout=resolve_timeout(),
            cancel_event=cancel_event,
        )
    except Exception as exc:
        finish = getattr(session, "_finish_command_job", None)
        if callable(finish):
            finish(
                job_id,
                status="failed",
                summary=f"Command execution error: {exc}",
                exit_code=-1,
                output="",
            )
        return

    if run_status in ("success", None, ""):
        run_status = "ok"
    # Map run_cancellable status onto durable job states.
    if run_status == "ok":
        terminal = "completed"
    elif run_status in COMMAND_TERMINAL_STATES:
        terminal = run_status
    elif run_status == "error":
        terminal = "failed"
    else:
        terminal = "failed"

    # Cooperative cancel may have already terminalized the row.
    if getattr(session, "_local_job_cancelled", lambda _jid: False)(job_id):
        terminal = "cancelled"

    bounded_output, spill_meta = _bound_or_spill_output(
        session,
        job_id,
        output if isinstance(output, str) else str(output or ""),
    )
    finish = getattr(session, "_finish_command_job", None)
    if callable(finish):
        finish(
            job_id,
            status=terminal,
            summary=_terminal_summary(terminal, exit_code, bounded_output),
            exit_code=int(exit_code) if exit_code is not None else -1,
            output=bounded_output,
            spill_uri=spill_meta.get("spill_uri") or "",
            spill_path=spill_meta.get("spill_path") or "",
            output_chars=int(spill_meta.get("output_chars") or len(bounded_output)),
            output_preview=spill_meta.get("output_preview") or "",
            run_status=run_status,
        )


def _terminal_summary(status: str, exit_code: int, output: str) -> str:
    first = ""
    for line in (output or "").splitlines():
        stripped = line.strip()
        if stripped:
            first = redact_secret_text(stripped)
            break
    if first and len(first) > 80:
        first = first[:77] + "..."
    if status == "completed":
        base = f"exit {exit_code}"
    else:
        base = f"{status} (exit {exit_code})"
    return f"{base} · {first}" if first else f"Command {base}"


def _bound_or_spill_output(
    session: Any,
    job_id: str,
    output: str,
) -> tuple:
    """Return (inline_or_preview, spill_metadata) without leaking secrets in paths."""
    text = output if isinstance(output, str) else str(output or "")
    meta: Dict[str, Any] = {
        "output_chars": len(text),
        "spill_uri": "",
        "spill_path": "",
        "output_preview": "",
    }
    if len(text) <= _INLINE_OUTPUT_CAP:
        return text, meta

    # Spill oversized output through the existing results/spill seam.
    state_dir = (
        getattr(session, "_state_dir_or_tempdir", None)
        or getattr(session, "state_dir", None)
        or ""
    )
    session_id = str(getattr(session, "harness_session_id", "") or "")
    preview = text[:4096]
    if len(text) > 8192:
        preview = text[:2048] + "\n...\n" + text[-2048:]
    meta["output_preview"] = preview
    if not state_dir:
        # No durable state dir: keep a hard-capped inline excerpt only.
        capped = text[:_INLINE_OUTPUT_CAP] + "\n\n... (output truncated to 50KB) ..."
        return capped, meta
    try:
        from harness.context_budget import spill_to_disk
        from harness.spill_registry import register_spill, spill_uri

        result_id = f"{job_id}-stdout"
        path = spill_to_disk(text, result_id, state_dir, dedupe=False)
        uri = spill_uri(session_id, result_id) if session_id else None
        if uri and path:
            register_spill(
                state_dir=state_dir,
                session_id=session_id,
                tool_call_id=result_id,
                path=path,
                chars=len(text),
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        meta["spill_uri"] = uri or ""
        meta["spill_path"] = path or ""
        return preview, meta
    except Exception:
        capped = text[:_INLINE_OUTPUT_CAP] + "\n\n... (output truncated to 50KB) ..."
        return capped, meta


def project_command_job_fields(job: Dict[str, Any]) -> Dict[str, Any]:
    """Extra fields for API/swarm-live projection of a command job."""
    if not isinstance(job, dict):
        return {}
    if str(job.get("job_kind") or "") != COMMAND_JOB_KIND and str(
        job.get("role") or ""
    ) != COMMAND_JOB_ROLE:
        return {}
    out: Dict[str, Any] = {
        "job_kind": COMMAND_JOB_KIND,
        "action_id": str(job.get("action_id") or ""),
        "command_fingerprint": str(job.get("command_fingerprint") or ""),
        "command_preview": str(job.get("command_preview") or ""),
        "started_at": job.get("started_at") or job.get("created_at"),
        "recovery_state": command_job_recovery_state(job),
    }
    ckpt = job.get("launch_checkpoint")
    if isinstance(ckpt, dict):
        # Secret-free launch fact for reattach / restart recovery UIs.
        out["launch_checkpoint"] = {
            "at": ckpt.get("at"),
            "phase": str(ckpt.get("phase") or ""),
            "running_at": ckpt.get("running_at"),
            "action_id": str(ckpt.get("action_id") or ""),
            "command_fingerprint": str(ckpt.get("command_fingerprint") or ""),
            "batch_id": str(ckpt.get("batch_id") or ""),
        }
    if job.get("terminal_receipt") is not None:
        out["terminal_receipt"] = job.get("terminal_receipt")
    if job.get("spill_uri"):
        out["spill_uri"] = job.get("spill_uri")
    if job.get("output_preview"):
        out["output_preview"] = job.get("output_preview")
    if job.get("exit_code") is not None:
        out["exit_code"] = job.get("exit_code")
    # Never project a raw command string — fingerprint + redacted preview only.
    return out
