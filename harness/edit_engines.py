from __future__ import annotations

"""In-process edit engines for run_implement / run_parallel.

Two engines, one normalized result (:class:`harness.worker.WorkerResult`), so the
downstream apply/review/checkpoint pipeline never has to care which one ran:

* ``agentic`` -- Puppetmaster's first-class, provider-agnostic adapter. Runs its
  own tool-use loop directly against a provider HTTP API on the user's own key
  (no external agent CLI), with the router picking a right-sized model among the
  providers the keys unlock. This is the standalone default whenever a provider
  key is present. We run it inside an isolated worktree and capture the diff, so
  edits never touch the live repo until the normal review/apply gate passes --
  identical isolation to the native engine.
* ``native`` -- Marionette's own pilot (:class:`ConversationalSession`) driven
  inside the worktree. Richer toolset (run_command, tests, codegraph, web) and
  the automatic fallback when no provider key is available.

Engine selection is provider-key-aware and overridable via ``HARNESS_EDIT_ENGINE``
or an explicit adapter on the action. The dispatcher falls back from agentic to
native only when agentic genuinely cannot run (no key / router could not pick a
model) -- never when agentic ran and simply produced no changes.
"""

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from typing import TYPE_CHECKING, Any, Iterator, Optional

from harness.diag import note as _diag

if TYPE_CHECKING:
    from harness.config import HarnessConfig
    from harness.swarm_model_pin import AgenticModelPin
    from harness.worker import WorkerResult


# Untracked build/agent artifacts a worker may create when it runs tests; kept
# out of the captured diff so a patch is only real source edits.
_ARTIFACT_PATHSPECS = [
    "*.pyc", "*.pyo", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "*.egg-info", ".coverage",
    "node_modules", ".DS_Store",
]

# Machine-readable reasons that mean "the agentic engine could not run at all"
# (as opposed to "ran fine, made no changes"). Only never-started reasons
# trigger native fallback — post-start stage codes must not.
AGENTIC_UNAVAILABLE = "agentic_unavailable"
AGENTIC_ROUTE_FAILED = "agentic_route_failed"
AGENTIC_ERROR = "agentic_error"
WORKTREE_CREATE_FAILED = "worktree_create_failed"
AGENTIC_ORCHESTRATOR_FAILED = "agentic_orchestrator_failed"
PATCH_CAPTURE_FAILED = "patch_capture_failed"
WORKER_CLEANUP_FAILED = "worker_cleanup_failed"
AGENTIC_PROVIDER_RATE_LIMITED = "agentic_provider_rate_limited"
AGENTIC_TIMEOUT = "agentic_timeout"
_FALLBACK_REASONS = (
    AGENTIC_UNAVAILABLE,
    AGENTIC_ROUTE_FAILED,
    WORKTREE_CREATE_FAILED,
)
_RATE_LIMIT_MARKERS = (
    "429",
    "retry-after",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "quota",
    "resource exhausted",
)
_TIMEOUT_MARKERS = (
    "timeout",
    "timed out",
    "time out",
    "deadline exceeded",
)


def _agentic_store_failure_snapshot(store: Any, job_id: str = "") -> dict:
    """Copy fail-closed facts off the scratch PM store before it is deleted.

    ``Orchestrator.run`` raises ``swarm exited with incomplete tasks`` after the
    worker/gate already recorded why. The temp sqlite dir is then rmtree'd, so
    this snapshot is the only durable reason/token/task status the harness keeps.
    """
    snap = {
        "job_id": "",
        "reason": "",
        "task_statuses": [],
        "task_ids": [],
        "tokens_in": 0,
        "tokens_out": 0,
        "usage_known": False,
    }
    if store is None:
        return snap
    resolved = str(job_id or "").strip()
    if not resolved:
        try:
            jobs = list(store.list_jobs() or [])
        except Exception:
            jobs = []
        if jobs:
            resolved = str(getattr(jobs[-1], "id", "") or "")
    snap["job_id"] = resolved
    if not resolved:
        return snap

    try:
        tasks = list(store.list_tasks(resolved) or [])
    except Exception:
        tasks = []
    statuses = []
    task_ids = []
    for task in tasks:
        tid = str(getattr(task, "id", "") or "").strip()
        if tid:
            task_ids.append(tid)
        role = str(getattr(task, "role", "") or "").strip()
        raw_status = getattr(task, "status", "")
        status = str(getattr(raw_status, "value", raw_status) or "").strip()
        if role and status:
            statuses.append(f"{role}={status}")
        elif status:
            statuses.append(status)
    snap["task_statuses"] = statuses
    snap["task_ids"] = task_ids

    reason = ""
    try:
        records = store.read_events(resolved) if hasattr(store, "read_events") else []
    except Exception:
        records = []
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        name = str(rec.get("event") or "").strip()
        payload = rec.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if name == "worker.gate_failed":
            text = str(payload.get("reason") or "").strip()
            if text:
                reason = text
        elif name == "worker.failed_task":
            text = str(payload.get("error") or payload.get("failure") or "").strip()
            if text:
                reason = text
        elif name == "worker.lease_lost":
            reason = "worker lease lost"
    snap["reason"] = reason

    try:
        arts = list(store.list_artifacts(resolved) or [])
    except Exception:
        arts = []
    tokens_in = 0
    tokens_out = 0
    usage_known = False
    art_failure = ""
    for art in arts:
        payload = getattr(art, "payload", None)
        if not isinstance(payload, dict):
            continue
        if "tokens_in" in payload or "tokens_out" in payload:
            usage_known = True
            tokens_out += int(payload.get("tokens_out") or 0)
            tokens_in += int(payload.get("tokens_in") or 0)
        if not art_failure and payload.get("failure"):
            art_failure = str(payload.get("failure") or "").strip()
    snap["tokens_in"] = tokens_in
    snap["tokens_out"] = tokens_out
    snap["usage_known"] = usage_known
    if not snap["reason"] and art_failure:
        snap["reason"] = art_failure
    return snap


def _format_agentic_engine_error(
    exc: BaseException,
    snapshot: Optional[dict] = None,
    files_changed: Optional[list] = None,
) -> str:
    """Pilot-facing summary for an agentic engine crash after store snapshot."""
    from harness.api.redaction import redact_secret_text

    parts = [f"Agentic engine error: {redact_secret_text(str(exc))}"]
    snap = snapshot or {}
    reason = redact_secret_text(str(snap.get("reason") or "").strip())
    if reason and reason not in parts[0]:
        parts.append(reason)
    statuses = [str(item) for item in (snap.get("task_statuses") or []) if str(item).strip()]
    if statuses:
        parts.append("tasks: " + ", ".join(statuses))
    files = [str(path) for path in (files_changed or []) if str(path).strip()]
    if files:
        parts.append("unapplied worktree files: " + ", ".join(files))
    return "\n".join(parts)


def _redact_text(text: str) -> str:
    from harness.api.redaction import redact_secret_text

    return redact_secret_text(text or "")


def _git_failed_message(wt_path: str, args: tuple, rc: int, stderr: str, stdout: str) -> str:
    detail = _redact_text((stderr or stdout or "").strip())
    cmd = " ".join(("git", "-C", str(wt_path)) + tuple(str(a) for a in args))
    return f"{cmd} failed (exit {rc}): {detail}"


def _raise_git_failed(wt_path: str, args: tuple, rc: int, stderr: str, stdout: str) -> None:
    raise RuntimeError(_git_failed_message(wt_path, args, rc, stderr, stdout))


def classify_agentic_exception(
    exc: BaseException,
    snapshot: Optional[dict] = None,
) -> str:
    """Map orchestrator/snapshot exceptions onto a stage error code."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    if status is None:
        status = getattr(exc, "http_status", None)
    try:
        status_i = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_i = None
    if status_i == 429:
        return AGENTIC_PROVIDER_RATE_LIMITED
    if isinstance(exc, TimeoutError):
        return AGENTIC_TIMEOUT
    parts = [_redact_text(str(exc))]
    if snapshot:
        parts.append(str(snapshot.get("reason") or ""))
    text = " ".join(parts).lower()
    if any(marker in text for marker in _RATE_LIMIT_MARKERS):
        return AGENTIC_PROVIDER_RATE_LIMITED
    if any(marker in text for marker in _TIMEOUT_MARKERS):
        return AGENTIC_TIMEOUT
    return AGENTIC_ORCHESTRATOR_FAILED


def _failure_http_hints(exc: BaseException) -> tuple:
    http_status = None
    retry_after = ""
    request_id = ""
    for obj in (exc, getattr(exc, "__cause__", None), getattr(exc, "__context__", None)):
        if obj is None:
            continue
        for attr in ("status_code", "status", "http_status"):
            raw = getattr(obj, attr, None)
            if raw is None:
                continue
            try:
                http_status = int(raw)
                break
            except (TypeError, ValueError):
                continue
        headers = getattr(obj, "headers", None)
        if isinstance(headers, dict):
            if not retry_after:
                retry_after = str(
                    headers.get("Retry-After") or headers.get("retry-after") or ""
                )
            if not request_id:
                request_id = str(
                    headers.get("x-request-id")
                    or headers.get("X-Request-Id")
                    or headers.get("request-id")
                    or ""
                )
        if not retry_after:
            retry_after = str(getattr(obj, "retry_after", "") or "")
        if not request_id:
            request_id = str(
                getattr(obj, "request_id", "")
                or getattr(obj, "provider_request_id", "")
                or ""
            )
    text = _redact_text(str(exc))
    if http_status is None and "429" in text:
        http_status = 429
    if not retry_after:
        lowered = text.lower()
        key = "retry-after"
        idx = lowered.find(key)
        if idx >= 0:
            rest = text[idx + len(key):].lstrip(":= \t")
            token = rest.split()[0] if rest.split() else ""
            retry_after = token.rstrip(".,;")
    return http_status, retry_after, request_id


def _failure_fields_from_exc(exc: BaseException) -> dict:
    text = str(exc)
    failure_command = ""
    failure_exit_code = None
    failure_stderr = text
    marker = " failed (exit "
    if text.startswith("git ") and marker in text:
        idx = text.find(marker)
        failure_command = text[:idx]
        rest = text[idx + len(marker):]
        code_s, sep, err = rest.partition("): ")
        if sep:
            try:
                failure_exit_code = int(code_s)
            except ValueError:
                failure_exit_code = None
            failure_stderr = err
    return {
        "failure_command": failure_command,
        "failure_exit_code": failure_exit_code,
        "failure_stderr": _redact_text(failure_stderr),
    }


def _usage_from_snapshot(snap: Optional[dict]) -> dict:
    data = snap or {}
    known = data.get("usage_known")
    if known is True:
        return {
            "tokens_in": int(data.get("tokens_in") or 0),
            "tokens_out": int(data.get("tokens_out") or 0),
            "usage_known": True,
            "cost_known": False,
        }
    return {
        "tokens_in": 0,
        "tokens_out": 0,
        "usage_known": False if known is False else None,
        "cost_known": False if known is False else None,
    }


def _artifacts_usage_known(result) -> bool:
    for art in getattr(result, "artifacts", []) or []:
        payload = getattr(art, "payload", {}) or {}
        if isinstance(payload, dict) and (
            "tokens_in" in payload or "tokens_out" in payload
        ):
            return True
    return False


def _should_native_fallback(result: "WorkerResult") -> bool:
    if result is None:
        return False
    if (getattr(result, "patch", None) or "").strip():
        return False
    if getattr(result, "files_changed", None):
        return False
    return getattr(result, "error", "") in _FALLBACK_REASONS


@contextlib.contextmanager
def managed_worktree(repo: str, base: str = "HEAD") -> Iterator[str]:
    """Create a confined git worktree for `repo`, yield its path, always clean up.

    Both engines edit inside the worktree so the live repo is untouched until the
    review/apply gate runs. The worktree and its throwaway branch are removed on
    exit even when the body raises.
    """
    from harness.worktrees import (
        _get_managed_dir,
        _is_confined,
        _safe_branch_name,
        add_worktree,
        delete_branch,
        remove_worktree,
    )

    branch_name = _safe_branch_name(f"pmedit-{uuid.uuid4().hex[:8]}")
    wt_path = ""
    try:
        wt_info = add_worktree(repo, branch=branch_name, base=base)
        wt_path = wt_info["path"]
        if not _is_confined(wt_path, _get_managed_dir(repo)):
            raise ValueError(
                "Confinement violation: worktree path lies outside the managed directory"
            )
        yield wt_path
    finally:
        if wt_path:
            with contextlib.suppress(Exception):
                remove_worktree(repo, wt_path, force=True)
        with contextlib.suppress(Exception):
            delete_branch(repo, branch_name)


@contextlib.contextmanager
def managed_worktree_for_goal(
    repo: str, goal: str, base: str = "HEAD",
) -> Iterator[str]:
    """Like :func:`managed_worktree`, then seed live goal paths into the worktree.

    HEAD checkouts omit untracked / dirty files the pilot just wrote. Seeding
    copies any goal-referenced live files into the worktree so agentic/native
    workers can see them (empty-diff / ``C:\\dev\\null`` class of failures).
    """
    from harness.worktree_seed import commit_seed_baseline, seed_worktree_from_goal

    with managed_worktree(repo, base=base) as wt_path:
        with contextlib.suppress(Exception):
            seed_result = seed_worktree_from_goal(repo, wt_path, goal)
            commit_seed_baseline(wt_path, seed_result.paths)
        yield wt_path


def finalize_worktree_patch(wt_path: str) -> tuple[str, list[str]]:
    """Stage everything in `wt_path`, drop build artifacts, return (patch, files).

    Returns the ``git diff --cached`` unified diff and the list of changed paths.
    Raises RuntimeError when a git step fails so the caller can report honestly.
    """
    from harness.worktrees import _is_repo

    if not wt_path or not os.path.exists(wt_path):
        raise RuntimeError(f"worktree path does not exist: {wt_path!r}")
    if not _is_repo(wt_path):
        raise RuntimeError(f"worktree path is not a git repository: {wt_path!r}")

    add_args = ("add", "-A")
    rc_add, out_add, err_add = _git(wt_path, *add_args)
    if rc_add != 0:
        _raise_git_failed(wt_path, add_args, rc_add, err_add, out_add)

    reset_specs: list[str] = []
    for spec in _ARTIFACT_PATHSPECS:
        reset_specs.append(f":(glob){spec}")
        reset_specs.append(f":(glob)**/{spec}")
    reset_args = ("reset", "-q", "--") + tuple(reset_specs)
    rc_reset, out_reset, err_reset = _git(wt_path, *reset_args)
    if rc_reset != 0:
        _raise_git_failed(wt_path, reset_args, rc_reset, err_reset, out_reset)

    diff_args = ("diff", "--cached", "--no-color")
    p_diff = subprocess.run(
        ["git", "-C", wt_path, *diff_args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if p_diff.returncode != 0:
        _raise_git_failed(
            wt_path, diff_args, p_diff.returncode, p_diff.stderr, p_diff.stdout,
        )
    patch = p_diff.stdout

    name_args = ("diff", "--cached", "--name-only")
    p_files = subprocess.run(
        ["git", "-C", wt_path, *name_args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
    )
    if p_files.returncode != 0:
        _raise_git_failed(
            wt_path, name_args, p_files.returncode, p_files.stderr, p_files.stdout,
        )
    files_changed = [ln.strip() for ln in p_files.stdout.splitlines() if ln.strip()]
    return patch, files_changed


def _git(cwd: str, *args: str) -> tuple[int, str, str]:
    from harness.worktrees import _git as _worktree_git

    return _worktree_git(cwd, *args)


def agentic_available() -> bool:
    """True when Puppetmaster's agentic edit workers can actually run.

    Mirrors Puppetmaster's key-aware adapter availability (OpenRouter, Codex
    OAuth, OpenCode Go, Nous, …). Use :func:`pilot_keys_ready` for the broader
    UI keyless-banner signal (includes pilot-only Cursor CLI login).
    """
    try:
        from puppetmaster import providers

        available = providers.available_providers()
        return bool(available)
    except Exception as exc:
        _diag("edit_engines.agentic_available", exc)
        # Fall back to a direct env check so a provider API shift never silently
        # disables the default engine.
        return any(
            os.environ.get(k, "").strip()
            for k in (
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "OPENCODE_GO_API_KEY",
                "OPENCODE_ZEN_API_KEY", "OPENCODE_API_KEY",
                "OPENAI_CODEX_TOKEN", "NOUS_API_KEY", "HERMES_API_KEY",
                "MINIMAX_API_KEY", "NVIDIA_API_KEY", "ZAI_API_KEY", "GLM_API_KEY",
                "XAI_API_KEY", "DEEPSEEK_API_KEY",
                "AWS_BEARER_TOKEN_BEDROCK",
            )
        ) or (
            bool(os.environ.get("AWS_ACCESS_KEY_ID", "").strip())
            and bool(os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip())
        )


def cursor_platform_available() -> bool:
    """True when platform cursor workers can run (CURSOR_API_KEY)."""
    try:
        from harness.provider_capabilities import cursor_platform_workers_ready

        return bool(cursor_platform_workers_ready())
    except Exception as exc:
        _diag("edit_engines.cursor_platform_available", exc)
        return bool(os.environ.get("CURSOR_API_KEY", "").strip())


def agentic_platform_enabled() -> bool:
    """Whether the Puppetmaster platform lock permits agentic execution."""

    try:
        from puppetmaster.platform_lock import is_adapter_enabled

        return bool(is_adapter_enabled("agentic"))
    except Exception:
        # Older Puppetmaster versions had no lock helper. Preserve the historical
        # key-aware behavior rather than disabling agentic on an import mismatch.
        return True


def _provider_has_usable_key(provider) -> bool:
    """Stored / env / OAuth key present and not disconnected (``has_key`` truth)."""
    from harness.registry_wizard import get_provider_key
    from harness.keys import get_api_key_status

    status = get_api_key_status(provider.name)
    return (get_provider_key(provider) is not None) or bool(status.get("has_key"))


def workers_ready() -> bool:
    """True when swarm/implement workers can run without a platform CLI install.

    Full stack Settings auths (OpenRouter, Codex OAuth, …) drive the agentic
    adapter. ``CURSOR_API_KEY`` is an optional platform-worker upgrade.
    cursor-cli agent login does **not** count — that path is Pilot only.
    """
    if cursor_platform_available():
        return True
    try:
        from harness.provider_capabilities import worker_capability
        from harness.registry_wizard import PROVIDERS
        from harness.keys import get_disconnected

        disconnected = get_disconnected()
        for p in PROVIDERS:
            if p.name in disconnected:
                continue
            if worker_capability(p.name) != "full_stack":
                continue
            if _provider_has_usable_key(p):
                return True
        return False
    except Exception as exc:
        _diag("edit_engines.workers_ready", exc)
        return agentic_available() or cursor_platform_available()


def pilot_keys_ready() -> bool:
    """True when Marionette has at least one usable keyed harness provider.

    Chat-lane signal (``/api/config`` ``pilot_ready``). Broader than
    :func:`workers_ready`: a cursor-cli agent login counts here, but does not
    make agentic workers runnable. Use :func:`workers_ready` for the banner
    that claims swarms can run.

    Authoritative semantics match ``GET /api/providers`` ``has_key``: stored keys,
    env keys, and credential-pool OAuth (ChatGPT Codex) all count, and explicit
    disconnects hide a provider even when its key remains on disk / in the shell.
    """
    try:
        from harness.registry_wizard import PROVIDERS
        from harness.keys import get_disconnected

        disconnected = get_disconnected()
        for p in PROVIDERS:
            if p.name in disconnected:
                continue
            if _provider_has_usable_key(p):
                return True
        return False
    except Exception as exc:
        _diag("edit_engines.pilot_keys_ready", exc)
        if agentic_available():
            return True
        return any(
            os.environ.get(k, "").strip()
            for k in (
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "GEMINI_API_KEY",
                "GOOGLE_API_KEY",
                "OPENROUTER_API_KEY",
                "OPENCODE_GO_API_KEY",
                "OPENCODE_ZEN_API_KEY",
                "OPENCODE_API_KEY",
                "OPENAI_CODEX_TOKEN",
                "AWS_BEARER_TOKEN_BEDROCK",
            )
        ) or (
            bool(os.environ.get("AWS_ACCESS_KEY_ID", "").strip())
            and bool(os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip())
        )


def select_edit_engine(config: "HarnessConfig", requested_adapter: str = "") -> str:
    """Pick the in-process edit engine: 'agentic', 'cursor', or 'native'.

    Precedence: explicit action adapter > HARNESS_EDIT_ENGINE env > provider-key
    availability. External CLI adapters (claude-code/codex) are handled by the
    caller before this point. An explicit ``agentic`` request (or env pin) fails
    closed to ``native`` when agentic keys are absent — never silently demotes
    to platform ``cursor``. Unpinned selection still prefers cursor when no
    agentic HTTP provider is keyed.
    """
    requested = (requested_adapter or "").strip().lower()
    if requested in ("native", "provider"):
        return "native"
    # Explicit agentic pin fails closed to native (same as HARNESS_EDIT_ENGINE=
    # agentic) — never silently demote to platform cursor when keys are absent.
    if requested == "agentic":
        return "agentic" if agentic_available() else "native"
    if requested in ("cursor", "cursor-sdk"):
        return "cursor" if cursor_platform_available() else "native"

    env_choice = (os.environ.get("HARNESS_EDIT_ENGINE", "") or "").strip().lower()
    if env_choice in ("native", "agentic", "cursor"):
        if env_choice == "native":
            return "native"
        if env_choice == "agentic":
            return "agentic" if agentic_available() else "native"
        return "cursor" if cursor_platform_available() else "native"

    if agentic_available():
        return "agentic"
    if cursor_platform_available():
        return "cursor"
    return "native"


def run_edit_worker(
    config: "HarnessConfig", goal: str, requested_adapter: str = "",
    job_id: str = "", session_id: str = "", cwd: str = "",
    expects_diff: bool = True,
    agentic_pin: Optional["AgenticModelPin"] = None,
    strict_adapter: bool = False,
    on_event=None,
) -> "WorkerResult":
    """Run the selected in-process edit engine and return a normalized result.

    Falls back from agentic/cursor to native only when those could not run.
    """
    from harness.worker import WorkerResult

    requested = (requested_adapter or "").strip().lower()
    strict_agentic = bool(
        strict_adapter or agentic_pin is not None or requested == "agentic"
    )
    if strict_agentic and requested not in ("", "agentic"):
        return _stamp_agentic(
            WorkerResult(
                ok=False,
                error=AGENTIC_ROUTE_FAILED,
                summary=(
                    f"Agentic model pin cannot run through adapter "
                    f"{requested_adapter!r}; use adapter='agentic'."
                ),
            ),
            execution_pin=agentic_pin,
        )
    if strict_agentic and (
        not agentic_platform_enabled() or not agentic_available()
    ):
        reason = (
            "Agentic adapter is disabled by the platform lock."
            if not agentic_platform_enabled()
            else "No usable provider key is available for the agentic adapter."
        )
        return _stamp_agentic(
            WorkerResult(
                ok=False,
                error=AGENTIC_UNAVAILABLE,
                summary=reason,
            ),
            execution_pin=agentic_pin,
        )

    effective_adapter = "agentic" if strict_agentic else requested_adapter
    engine = select_edit_engine(config, effective_adapter)
    target_cwd = cwd or config.repo
    if engine == "cursor":
        result = run_cursor_edit(
            config, goal, session_id=session_id, cwd=target_cwd,
            expects_diff=expects_diff, job_id=job_id,
        )
        if _should_native_fallback(result):
            _diag("edit_engines.run_edit_worker",
                  msg=f"cursor engine unavailable ({result.error}); falling back to native")
            return run_native_edit(
                config, goal, job_id=job_id, session_id=session_id, cwd=target_cwd,
                expects_diff=expects_diff, on_event=on_event,
            )
        return result
    if engine == "agentic":
        result = run_agentic_edit(
            config, goal, session_id=session_id, cwd=target_cwd,
            expects_diff=expects_diff, job_id=job_id,
            agentic_pin=agentic_pin,
        )
        if strict_agentic:
            return result
        if _should_native_fallback(result):
            if cursor_platform_available():
                _diag("edit_engines.run_edit_worker",
                      msg=f"agentic unavailable ({result.error}); trying cursor")
                cursor_result = run_cursor_edit(
                    config, goal, session_id=session_id, cwd=target_cwd,
                    expects_diff=expects_diff, job_id=job_id,
                )
                if not _should_native_fallback(cursor_result):
                    return cursor_result
            _diag("edit_engines.run_edit_worker",
                  msg=f"agentic engine unavailable ({result.error}); falling back to native")
            return run_native_edit(
                config, goal, job_id=job_id, session_id=session_id, cwd=target_cwd,
                expects_diff=expects_diff, on_event=on_event,
            )
        return result
    return run_native_edit(
        config, goal, job_id=job_id, session_id=session_id, cwd=target_cwd,
        expects_diff=expects_diff, on_event=on_event,
    )


def run_implement(
    config: "HarnessConfig", goal: str, requested_adapter: str = "",
    job_id: str = "", session_id: str = "", cwd: str = "",
    expects_diff: bool = True,
    agentic_pin: Optional["AgenticModelPin"] = None,
    strict_adapter: bool = False,
) -> "WorkerResult":
    """Dispatch a single implement worker (agentic or native)."""
    return run_edit_worker(
        config, goal, requested_adapter=requested_adapter,
        job_id=job_id, session_id=session_id, cwd=cwd,
        expects_diff=expects_diff,
        agentic_pin=agentic_pin,
        strict_adapter=strict_adapter,
    )


def run_parallel(
    config: "HarnessConfig", goals: list[str], requested_adapter: str = "",
    session_id: str = "", cwd: str = "",
    expects_diff: bool = True,
    agentic_pin: Optional["AgenticModelPin"] = None,
    strict_adapter: bool = False,
) -> list["WorkerResult"]:
    """Run several implement workers sequentially (caller fans out concurrency)."""
    results = []
    for goal in goals or []:
        if not (goal or "").strip():
            continue
        results.append(run_implement(
            config, goal, requested_adapter=requested_adapter,
            session_id=session_id, cwd=cwd,
            expects_diff=expects_diff,
            agentic_pin=agentic_pin,
            strict_adapter=strict_adapter,
        ))
    return results


def run_native_edit(
    config: "HarnessConfig", goal: str, job_id: str = "",
    session_id: str = "", cwd: str = "",
    expects_diff: bool = True,
    on_event=None,
) -> "WorkerResult":
    """Marionette's own pilot loop driven in a worktree (the rich engine)."""
    from harness.autobudget import AutoBudget
    from harness.worker import ProviderWorker
    from pmharness.bridge import worker_token_budget

    # Per-worker ceiling (Settings / HARNESS_WORKER_TOKEN_BUDGET), not the
    # full-auto tree ceiling — AutoBudget.from_env() used to starve analysis
    # workers at 50k mid-turn. Analysis gets more idle headroom (no swarm
    # findings to reset the stall counter).
    worker = ProviderWorker(
        cwd or config.repo, goal,
        driver=config.driver, reach=config.reach,
        budget=AutoBudget(
            max_tokens=worker_token_budget(),
            max_seconds=900,
            max_swarms=2,
            max_idle_steps=5 if not expects_diff else 2,
        ),
        require_codegraph=False,
        job_id=job_id,
        expects_diff=expects_diff,
        on_event=on_event,
    )
    # ProviderWorker.run() stamps tokens_out from the budget on every return path.
    result = worker.run()
    result.engine = "native"
    result.model = (getattr(config, "driver", None) or "") or ""
    return result


def run_cursor_edit(
    config: "HarnessConfig", goal: str, *, session_id: str = "", cwd: str = "",
    expects_diff: bool = True, job_id: str = "",
) -> "WorkerResult":
    """Platform Cursor SDK workers (CURSOR_API_KEY) in a managed worktree.

    Used when no agentic HTTP provider is keyed. Distinct from Settings
    Cursor CLI agent-login pilot auth.
    """
    from harness.worker import WorkerResult
    from harness.cli_job_merge import mark_marionette_host_scratch
    from harness.job_scoping import job_label_for_session, stamp_task_payload

    if not cursor_platform_available():
        return _stamp_agentic(WorkerResult(
            ok=False, error=AGENTIC_UNAVAILABLE,
            summary="CURSOR_API_KEY not set for platform cursor workers.",
        ))

    try:
        from puppetmaster.orchestrator import Orchestrator
        from puppetmaster.store_factory import create_store
        from puppetmaster.workers import WorkerSpec
    except Exception as exc:
        _diag("edit_engines.run_cursor_edit.import", exc)
        return _stamp_agentic(WorkerResult(
            ok=False, error=AGENTIC_UNAVAILABLE,
            summary=f"Puppetmaster unavailable: {exc}",
        ))

    try:
        repo_root = cwd or config.repo
        with managed_worktree_for_goal(repo_root, goal) as wt_path:
            from pmharness.bridge import (
                _analysis_instruction,
                _analyze_max_turns,
                worker_token_budget,
            )

            if not expects_diff:
                payload = stamp_task_payload({
                    "read_only": True,
                    "no_edit": True,
                    "dry_run": True,
                    "cwd": wt_path,
                    "prompt": goal,
                    "auto_route": True,
                    "allowed_adapters": ["cursor"],
                    "prefer_plan_billed": True,
                    "max_turns": _analyze_max_turns(),
                    "token_budget": worker_token_budget(),
                    "routing_policy": "balanced",
                }, session_id=session_id, cwd=wt_path)
                instruction = _analysis_instruction(
                    goal, wt_path, "explore", via_tool=True,
                )
                role = "explore"
            else:
                payload = stamp_task_payload({
                    "mode": "implement",
                    "cwd": wt_path,
                    "prompt": goal,
                    "auto_route": True,
                    "allowed_adapters": ["cursor"],
                    "prefer_plan_billed": True,
                    "token_budget": worker_token_budget(),
                    "routing_policy": "balanced",
                }, session_id=session_id, cwd=wt_path)
                instruction = goal
                role = "implement"

            spec = WorkerSpec(
                role=role,
                instruction=instruction,
                adapter="cursor",
                payload=payload,
            )
            tmp = tempfile.mkdtemp(prefix="pmh-cursor-edit-")
            mark_marionette_host_scratch(tmp)
            try:
                store = create_store("sqlite", tmp)
                result = Orchestrator(store).run(
                    goal,
                    specs=[spec],
                    worker_mode="inline",
                    label=job_label_for_session(session_id, dispatch_id=job_id),
                )
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

            patch, files_changed = finalize_worktree_patch(wt_path)
            if not expects_diff:
                patch = ""
                files_changed = []
            tokens_out, tokens_in, failure, final_text = _summarize_agentic_result(result)
            routed_model = _routed_model_id(result)
            if failure in ("no_model", "unknown_provider", "route_failed"):
                return _stamp_agentic(WorkerResult(
                    ok=False, error=AGENTIC_ROUTE_FAILED,
                    summary=final_text or "Cursor engine could not select a model.",
                    model=routed_model,
                    worktree=wt_path,
                    managed_worktree_path=wt_path,
                    managed_worktree_mode="managed",
                ), result)
            return _stamp_agentic(WorkerResult(
                ok=True,
                patch=patch,
                files_changed=files_changed,
                summary=final_text or ("ok" if expects_diff else "analysis complete"),
                model=routed_model,
                tokens_out=tokens_out,
                tokens_in=tokens_in,
                worktree=wt_path,
                managed_worktree_path=wt_path,
                managed_worktree_mode="managed",
                worktree_diff_empty=not bool(patch.strip()),
            ), result)
    except Exception as exc:
        _diag("edit_engines.run_cursor_edit", exc)
        return _stamp_agentic(WorkerResult(
            ok=False, error=AGENTIC_ERROR,
            summary=str(exc),
        ))


def run_agentic_edit(
    config: "HarnessConfig", goal: str, *, session_id: str = "", cwd: str = "",
    expects_diff: bool = True, job_id: str = "",
    agentic_pin: Optional["AgenticModelPin"] = None,
) -> "WorkerResult":
    """Puppetmaster agentic adapter in a managed worktree.

    ``expects_diff=True`` (default): implement mode + patch capture.
    ``expects_diff=False``: read-only analyze mode (submit_findings contract),
    matching bridge agentic swarms — never ``mode=implement``.

    Never raises for a run failure -- it returns a WorkerResult whose ``error`` is
    one of the ``AGENTIC_*`` reasons so the dispatcher can fall back to native.
    """
    from harness.worker import (
        WorkerResult,
        _analysis_output_is_structured,
        coerce_unlabeled_analysis_prose,
        parse_analysis_signal_rows,
    )
    from harness.cli_job_merge import mark_marionette_host_scratch
    from harness.job_scoping import job_label_for_session, stamp_task_payload

    requested_mode = "implement" if expects_diff else "analysis"

    def finish(
        wr: "WorkerResult",
        pm_result=None,
        *,
        worktree_existed: bool = False,
        snap: Optional[dict] = None,
        job_id_hint: str = "",
    ) -> "WorkerResult":
        wr.requested_mode = requested_mode
        if requested_mode == "implement" and (wr.managed_worktree_mode or "") in (
            "", "unknown",
        ):
            wr.managed_worktree_mode = "managed" if worktree_existed else "none"
        if job_id_hint and not wr.pm_job_id:
            wr.pm_job_id = job_id_hint
        if snap:
            if not wr.pm_job_id:
                wr.pm_job_id = str(snap.get("job_id") or "")
            if not wr.task_ids and snap.get("task_ids"):
                wr.task_ids = list(snap.get("task_ids") or [])
        return _stamp_agentic(wr, pm_result, execution_pin=agentic_pin)

    if not agentic_available():
        return finish(WorkerResult(
            ok=False, error=AGENTIC_UNAVAILABLE,
            summary="No provider key visible for the agentic engine.",
            patch_capture_status="skipped",
        ), worktree_existed=False)

    try:
        from puppetmaster.orchestrator import Orchestrator
        from puppetmaster.store_factory import create_store
        from puppetmaster.workers import WorkerSpec
    except Exception as exc:
        _diag("edit_engines.run_agentic_edit.import", exc)
        return finish(WorkerResult(
            ok=False, error=AGENTIC_UNAVAILABLE,
            summary=f"Puppetmaster unavailable: {_redact_text(str(exc))}",
            patch_capture_status="skipped",
        ), worktree_existed=False)

    provider = (
        agentic_pin.provider
        if agentic_pin is not None
        else (os.environ.get("HARNESS_IMPLEMENT_PROVIDER", "") or "").strip().lower()
    )
    model = (
        agentic_pin.model
        if agentic_pin is not None
        else (os.environ.get("HARNESS_IMPLEMENT_MODEL", "") or "").strip()
    )

    worktree_entered = False
    try:
        repo_root = cwd or config.repo
        with managed_worktree_for_goal(repo_root, goal) as wt_path:
            worktree_entered = True
            from pmharness.bridge import (
                _analysis_capability_payload,
                _analysis_instruction,
                _analyze_max_turns,
                _has_real_structured_findings,
                _router_supports_max_capability,
                rescue_analysis_compact,
                worker_token_budget,
            )

            if not expects_diff:
                # Mirror bridge agentic analysis swarms: read-only analyze
                # mode (no mode=implement) so the adapter does not burn the
                # full-edit loop until the 900s wall deadline.
                base_payload = {
                    "read_only": True,
                    "no_edit": True,
                    "dry_run": True,
                    "cwd": wt_path,
                    "prompt": goal,
                    "auto_route": True,
                    "allowed_adapters": ["agentic"],
                    "prefer_plan_billed": False,
                    "max_turns": _analyze_max_turns(),
                    "token_budget": worker_token_budget(),
                    "routing_policy": "balanced",
                    **_analysis_capability_payload(),
                }
                if agentic_pin is not None:
                    base_payload.update(agentic_pin.payload_fields())
                payload = stamp_task_payload(
                    base_payload, session_id=session_id, cwd=wt_path,
                )
                instruction = _analysis_instruction(
                    goal, wt_path, "explore", via_tool=True,
                )
                spec = WorkerSpec(
                    role="explore",
                    instruction=instruction,
                    adapter="agentic",
                    payload=payload,
                )
            else:
                payload = stamp_task_payload({
                    "mode": "implement",
                    "cwd": wt_path,
                    "prompt": goal,
                    "auto_route": not (provider and model),
                    "allowed_adapters": ["agentic"],
                    "token_budget": worker_token_budget(),
                }, session_id=session_id, cwd=wt_path)
                if not (provider and model):
                    # Cost guardrail (mirrors the analysis-swarm cap in bridge.py):
                    # role="implement" has a high base score that first-picks the
                    # frontier model (opus, ~$15/$75 per Mtok). A balanced policy with
                    # a capability CEILING (max_capability, not min_capability --
                    # min would force every edit to the exact same score and pin one
                    # model) lands on a strong-but-far-cheaper coder that is more
                    # than capable of edits. Opt into frontier depth with
                    # HARNESS_IMPLEMENT_DEEP=1.
                    payload["routing_policy"] = "balanced"
                    if os.environ.get("HARNESS_IMPLEMENT_DEEP", "").strip() not in ("1", "true", "yes"):
                        try:
                            _cap = int(
                                os.environ.get("HARNESS_IMPLEMENT_MAX_CAPABILITY", "86"))
                        except (TypeError, ValueError):
                            _cap = 86
                        _cap_key = ("max_capability"
                                    if _router_supports_max_capability()
                                    else "min_capability")
                        payload[_cap_key] = _cap
                if provider:
                    payload["provider"] = provider
                if model:
                    payload["model"] = model
                if agentic_pin is not None:
                    payload.update(agentic_pin.payload_fields())

                spec = WorkerSpec(
                    role="implement",
                    instruction=goal,
                    adapter="agentic",
                    payload=payload,
                )
            # The PM sqlite store is scratch state for this single inline run.
            # Map any structured tool/action events BEFORE deleting the store;
            # never parse prose/stdout. Without the rmtree every agentic
            # implement worker leaked a pmh-edit-* dir (audit finding #3).
            tmp = tempfile.mkdtemp(prefix="pmh-edit-")
            mark_marionette_host_scratch(tmp)
            mapped_events: list = []
            result = None
            orchestrator_exc = None
            failure_snap: dict = {}
            store_cleanup_exc = None
            pm_job_id = ""
            try:
                store = create_store("sqlite", tmp)
                try:
                    result = Orchestrator(store).run(
                        goal,
                        specs=[spec],
                        worker_mode="inline",
                        label=job_label_for_session(session_id, dispatch_id=job_id),
                    )
                    pm_job_id = str(getattr(getattr(result, "job", None), "id", "") or "")
                    mapped_events = agentic_events_from_store(store, pm_job_id)
                    try:
                        failure_snap = _agentic_store_failure_snapshot(store, pm_job_id)
                    except Exception as snap_exc:
                        _diag("edit_engines.run_agentic_edit.snapshot", snap_exc)
                except Exception as run_exc:
                    orchestrator_exc = run_exc
                    try:
                        failure_snap = _agentic_store_failure_snapshot(store)
                        mapped_events = agentic_events_from_store(
                            store, str(failure_snap.get("job_id") or ""),
                        )
                    except Exception as snap_exc:
                        _diag("edit_engines.run_agentic_edit.snapshot", snap_exc)
            finally:
                try:
                    shutil.rmtree(tmp, ignore_errors=True)
                except Exception as rmtree_exc:
                    store_cleanup_exc = rmtree_exc

            patch = ""
            files_changed: list = []
            worktree_diff_empty = None
            patch_capture_status = "skipped"
            finalize_exc = None
            try:
                patch, files_changed = finalize_worktree_patch(wt_path)
                worktree_diff_empty = not bool(patch.strip())
                patch_capture_status = "ok"
            except Exception as final_exc:
                finalize_exc = final_exc
                patch_capture_status = "failed"
                worktree_diff_empty = None
                _diag("edit_engines.run_agentic_edit.finalize", final_exc)

            primary_error = ""
            if orchestrator_exc is not None:
                primary_error = classify_agentic_exception(
                    orchestrator_exc, failure_snap,
                )
            elif finalize_exc is not None:
                primary_error = PATCH_CAPTURE_FAILED
            elif store_cleanup_exc is not None and result is None:
                primary_error = WORKER_CLEANUP_FAILED

            if orchestrator_exc is not None:
                usage = _usage_from_snapshot(failure_snap)
                http_status, retry_after, request_id = _failure_http_hints(
                    orchestrator_exc,
                )
                git_fail = (
                    _failure_fields_from_exc(finalize_exc)
                    if finalize_exc is not None else {}
                )
                return finish(WorkerResult(
                    ok=False,
                    error=primary_error,
                    summary=_format_agentic_engine_error(
                        orchestrator_exc, failure_snap, files_changed,
                    ),
                    patch=patch,
                    files_changed=list(files_changed),
                    tokens_out=int(usage["tokens_out"]),
                    tokens_in=int(usage["tokens_in"]),
                    usage_known=usage["usage_known"],
                    cost_known=usage["cost_known"],
                    worktree=wt_path,
                    managed_worktree_path=wt_path,
                    managed_worktree_mode="managed",
                    worktree_diff_empty=worktree_diff_empty,
                    events=list(mapped_events),
                    patch_capture_status=patch_capture_status,
                    http_status=http_status,
                    retry_after=retry_after,
                    provider_request_id=request_id,
                    finish_reason=str((failure_snap or {}).get("reason") or ""),
                    failure_command=str(git_fail.get("failure_command") or ""),
                    failure_exit_code=git_fail.get("failure_exit_code"),
                    failure_stderr=str(git_fail.get("failure_stderr") or ""),
                ), worktree_existed=True, snap=failure_snap)

            if finalize_exc is not None:
                git_fail = _failure_fields_from_exc(finalize_exc)
                return finish(WorkerResult(
                    ok=False,
                    error=PATCH_CAPTURE_FAILED,
                    summary=_redact_text(
                        f"Patch capture failed: {finalize_exc}"
                    ),
                    worktree=wt_path,
                    managed_worktree_path=wt_path,
                    managed_worktree_mode="managed",
                    worktree_diff_empty=None,
                    events=list(mapped_events),
                    patch_capture_status="failed",
                    usage_known=_artifacts_usage_known(result) if result is not None else False,
                    cost_known=False,
                    failure_command=str(git_fail.get("failure_command") or ""),
                    failure_exit_code=git_fail.get("failure_exit_code"),
                    failure_stderr=str(git_fail.get("failure_stderr") or ""),
                ), result, worktree_existed=True, snap=failure_snap,
                    job_id_hint=pm_job_id)

            if store_cleanup_exc is not None and result is None:
                return finish(WorkerResult(
                    ok=False,
                    error=WORKER_CLEANUP_FAILED,
                    summary=_redact_text(
                        f"Worker cleanup failed: {store_cleanup_exc}"
                    ),
                    worktree=wt_path,
                    managed_worktree_path=wt_path,
                    managed_worktree_mode="managed",
                    worktree_diff_empty=worktree_diff_empty,
                    events=list(mapped_events),
                    patch_capture_status=patch_capture_status,
                    usage_known=False,
                    cost_known=False,
                ), worktree_existed=True, snap=failure_snap)

            tokens_out, tokens_in, failure, final_text = _summarize_agentic_result(result)
            usage_known = _artifacts_usage_known(result)
            cost_known = False if usage_known else None
            routed_model = _routed_model_id(result)
            if agentic_pin is not None:
                from harness.swarm_model_pin import agentic_pin_matches_routed_model

                if not agentic_pin_matches_routed_model(
                    agentic_pin,
                    routed_model,
                ):
                    return finish(WorkerResult(
                        ok=False,
                        error=AGENTIC_ROUTE_FAILED,
                        summary=(
                            f"Agentic model mismatch: requested "
                            f"{agentic_pin.router_model_id!r}, routed "
                            f"{routed_model!r}."
                        ),
                        model=routed_model,
                        worktree=wt_path,
                        managed_worktree_path=wt_path,
                        managed_worktree_mode="managed",
                        worktree_diff_empty=worktree_diff_empty,
                        events=list(mapped_events),
                        patch_capture_status=patch_capture_status,
                        usage_known=usage_known,
                        cost_known=cost_known,
                    ), result, worktree_existed=True, snap=failure_snap,
                        job_id_hint=pm_job_id)

            # Analysis/review: never report seed leftovers as applied edits.
            if not expects_diff:
                patch = ""
                files_changed = []

            if not patch.strip():
                # Distinguish "engine could not run" (route/provider failure) from
                # "ran fine but changed nothing" so fallback only fires for the former.
                if failure in ("no_model", "unknown_provider", "route_failed"):
                    return finish(WorkerResult(
                        ok=False, error=AGENTIC_ROUTE_FAILED,
                        summary=final_text or "Agentic engine could not select a model/provider.",
                        model=routed_model,
                        worktree=wt_path,
                        managed_worktree_path=wt_path,
                        managed_worktree_mode="managed",
                        worktree_diff_empty=worktree_diff_empty,
                        events=list(mapped_events),
                        patch_capture_status=patch_capture_status,
                        usage_known=usage_known,
                        cost_known=cost_known,
                    ), result, worktree_existed=True, snap=failure_snap,
                        job_id_hint=pm_job_id)
                if not expects_diff:
                    # Gate on structured findings — never green unlabeled prose.
                    # Same rescue order as swarm/bridge: promote verification-
                    # parked empty_or_unstructured prose first, then fall back
                    # to coerce on unlabeled final_text.
                    # has_structured = typed compact artifacts; structured_ok =
                    # FINDING/RISK/DECISION labels in final_text (tightened gate).
                    compact: list = []
                    try:
                        from pmharness.bridge import _compact_artifact
                        compact = [
                            _compact_artifact(a)
                            for a in (getattr(result, "artifacts", None) or [])
                        ]
                        compact = rescue_analysis_compact(compact)
                        has_structured = _has_real_structured_findings(compact)
                    except Exception:
                        has_structured = False
                    analysis_text = final_text or ""
                    structured_ok, degrade_reason = _analysis_output_is_structured(
                        analysis_text,
                        halt_reason=failure or "",
                    )
                    if not has_structured and not structured_ok:
                        coerced = coerce_unlabeled_analysis_prose(analysis_text)
                        if coerced != analysis_text:
                            structured_ok, degrade_reason = (
                                _analysis_output_is_structured(
                                    coerced,
                                    halt_reason=failure or "",
                                )
                            )
                            if structured_ok:
                                analysis_text = coerced
                    if has_structured or structured_ok:
                        summary = _agentic_analysis_summary(
                            compact, analysis_text,
                        )
                        signal_rows = parse_analysis_signal_rows(analysis_text)
                        if not signal_rows and has_structured:
                            signal_rows = _signal_rows_from_compact(compact)
                        return finish(WorkerResult(
                            ok=True, tokens_out=tokens_out, tokens_in=tokens_in,
                            summary=summary,
                            model=routed_model,
                            worktree=wt_path,
                            managed_worktree_path=wt_path,
                            managed_worktree_mode="managed",
                            worktree_diff_empty=worktree_diff_empty,
                            events=list(mapped_events),
                            findings=signal_rows,
                            patch_capture_status=patch_capture_status,
                            usage_known=usage_known,
                            cost_known=cost_known,
                        ), result, worktree_existed=True, snap=failure_snap,
                            job_id_hint=pm_job_id)
                    label = degrade_reason or "no structured findings"
                    summary_parts = [label]
                    if final_text:
                        summary_parts.append(
                            f"Last assistant message: {final_text}"
                        )
                    return finish(WorkerResult(
                        ok=False, error=label,
                        tokens_out=tokens_out, tokens_in=tokens_in,
                        summary="\n".join(summary_parts),
                        model=routed_model,
                        worktree=wt_path,
                        managed_worktree_path=wt_path,
                        managed_worktree_mode="managed",
                        worktree_diff_empty=worktree_diff_empty,
                        events=list(mapped_events),
                        patch_capture_status=patch_capture_status,
                        usage_known=usage_known,
                        cost_known=cost_known,
                    ), result, worktree_existed=True, snap=failure_snap,
                        job_id_hint=pm_job_id)
                return finish(WorkerResult(
                    ok=False, tokens_out=tokens_out, tokens_in=tokens_in,
                    summary=final_text or "no changes produced",
                    model=routed_model,
                    worktree=wt_path,
                    managed_worktree_path=wt_path,
                    managed_worktree_mode="managed",
                    worktree_diff_empty=worktree_diff_empty,
                    events=list(mapped_events),
                    patch_capture_status=patch_capture_status,
                    usage_known=usage_known,
                    cost_known=cost_known,
                ), result, worktree_existed=True, snap=failure_snap,
                    job_id_hint=pm_job_id)

            return finish(WorkerResult(
                ok=True, patch=patch, files_changed=files_changed,
                tokens_out=tokens_out, tokens_in=tokens_in,
                summary=final_text or (f"Files changed: {', '.join(files_changed)}" if files_changed else "Patch generated"),
                model=routed_model,
                worktree=wt_path,
                managed_worktree_path=wt_path,
                managed_worktree_mode="managed",
                worktree_diff_empty=worktree_diff_empty,
                events=list(mapped_events),
                patch_capture_status=patch_capture_status,
                usage_known=usage_known,
                cost_known=cost_known,
            ), result, worktree_existed=True, snap=failure_snap,
                job_id_hint=pm_job_id)
    except Exception as exc:
        _diag("edit_engines.run_agentic_edit", exc)
        if not worktree_entered:
            git_fail = _failure_fields_from_exc(exc)
            return finish(WorkerResult(
                ok=False,
                error=WORKTREE_CREATE_FAILED,
                summary=f"Worktree create failed: {_redact_text(str(exc))}",
                managed_worktree_mode="none",
                worktree_diff_empty=None,
                patch_capture_status="skipped",
                failure_command=git_fail.get("failure_command") or "git worktree add",
                failure_exit_code=git_fail.get("failure_exit_code"),
                failure_stderr=_redact_text(str(exc)),
            ), worktree_existed=False)
        return finish(WorkerResult(
            ok=False,
            error=AGENTIC_ERROR,
            summary=f"Agentic engine error: {_redact_text(str(exc))}",
            managed_worktree_mode="managed",
            worktree_diff_empty=None,
            patch_capture_status="skipped",
        ), worktree_existed=True)


# Store event names that already mean a tool/action boundary (not lifecycle).
_AGENTIC_TOOL_EVENT_NAMES = frozenset({
    "tool.started",
    "tool.finished",
    "tool.failed",
    "tool_call_progress",
    "action_start",
    "action_result",
})


def agentic_events_from_store(store: Any, job_id: str) -> list:
    """Map structured PM store tool/action events into ConvEvent rows.

    Only payloads that already carry a stable id plus kind/tool (or an explicit
    tool event name) are mapped. Lifecycle events and raw artifact/stdout
    payloads are ignored — never fabricate tool rows from prose.
    """
    from harness.conversation import ConvEvent

    if not store or not job_id:
        return []
    try:
        records = store.read_events(job_id) if hasattr(store, "read_events") else []
    except Exception:
        return []
    out: list = []
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        name = str(rec.get("event") or "").strip()
        payload = rec.get("payload")
        if isinstance(payload, str):
            try:
                import json
                payload = json.loads(payload)
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        action_id = (
            payload.get("id")
            or payload.get("action_id")
            or payload.get("tool_call_id")
            or ""
        )
        action_kind = (
            payload.get("kind")
            or payload.get("tool")
            or payload.get("tool_name")
            or ""
        )
        explicit_tool = name in _AGENTIC_TOOL_EVENT_NAMES
        if not explicit_tool and not (action_id and action_kind):
            continue
        if not action_id:
            # Explicit tool event without id — skip rather than invent one.
            continue
        goal = payload.get("goal") or payload.get("path") or payload.get("target") or ""
        # Never pull command/stdout/env into the mapped event.
        is_start = name in ("tool.started", "action_start", "tool_call_progress")
        if not is_start and name not in (
            "tool.finished", "tool.failed", "action_result",
        ):
            # Tool-shaped payload on an unknown event name: treat as start when
            # status is still running; otherwise a result.
            status_hint = str(payload.get("status") or "").lower()
            is_start = status_hint in ("", "running", "started", "in_progress")
        if is_start:
            out.append(ConvEvent("action_start", {
                "id": str(action_id),
                "kind": str(action_kind or "tool_call"),
                "goal": str(goal or ""),
            }))
            continue
        err = payload.get("error")
        if name == "tool.failed" and not err:
            err = "failed"
        status = payload.get("status")
        if name == "tool.failed":
            status = "failed"
        elif name == "tool.finished" and not status and not err:
            status = "complete"
        out.append(ConvEvent("action_result", {
            "id": str(action_id),
            "kind": str(action_kind or "tool_call"),
            "goal": str(goal or ""),
            "status": str(status or ("failed" if err else "complete")),
            "duration_ms": payload.get("duration_ms"),
            "error": err,
        }))
    return out


def _signal_rows_from_compact(compact: list) -> list:
    """Map compact typed artifacts into WorkerResult.findings-shaped rows."""
    rows: list = []
    for art in compact or []:
        if not isinstance(art, dict):
            continue
        kind = str(art.get("type") or "").lower()
        if kind not in ("finding", "risk", "decision"):
            continue
        if art.get("empty_headline"):
            continue
        headline = str(art.get("headline") or art.get("body") or "").strip()
        if not headline:
            continue
        if len(headline) > 240:
            headline = headline[:239] + "…"
        rows.append({"type": kind, "headline": headline})
    return rows


def _agentic_analysis_summary(compact: list, final_text: str) -> str:
    """Build a substantive analysis summary from stdout + FINDING/RISK/DECISION.

    Thin placeholders like ``Analysis complete.`` fail the parent job's
    ``analysis_summary_is_substantive`` gate even when artifacts were real.
    Prefer concrete artifact headlines so a green worker stays green upstream.
    """
    parts: list[str] = []
    body = (final_text or "").strip()
    if body:
        parts.append(body)
    for a in compact or []:
        if not isinstance(a, dict):
            continue
        if str(a.get("type") or "").lower() not in (
            "finding", "risk", "decision",
        ):
            continue
        if a.get("empty_headline"):
            continue
        text = str(a.get("body") or a.get("headline") or "").strip()
        if not text:
            continue
        if body and text in body:
            continue
        parts.append(text)
    summary = "\n\n".join(parts).strip()
    return summary or body or "Analysis complete."


def _summarize_agentic_result(result) -> tuple[int, int, str, str]:
    """Pull (tokens_out, tokens_in, failure_reason, final_text) from PM artifacts.

    Both token directions are summed so cost/telemetry counts the prompt tokens
    an implement worker burned, not just its completion tokens (audit finding #5)."""
    tokens_out = 0
    tokens_in = 0
    failure = ""
    final_text = ""
    for art in getattr(result, "artifacts", []) or []:
        payload = getattr(art, "payload", {}) or {}
        tokens_out += int(payload.get("tokens_out") or 0)
        tokens_in += int(payload.get("tokens_in") or 0)
        if not failure and payload.get("failure"):
            failure = str(payload.get("failure"))
        stdout = payload.get("stdout")
        if stdout and not final_text:
            final_text = str(stdout)[:2000]
    return tokens_out, tokens_in, failure, final_text


def _routed_model_id(result) -> str:
    """Model id from a ROUTING artifact on an agentic orchestrator result.

    Prefers the last non-empty model_id so a router-fallback stamp wins over an
    earlier plan-billed $0 pick. Returns '' when nothing routed."""
    model_id = ""
    for art in getattr(result, "artifacts", []) or []:
        atype = getattr(art, "type", None)
        type_str = str(getattr(atype, "value", None) or atype or "").strip().lower()
        payload = getattr(art, "payload", {}) or {}
        # Typed ROUTING rows are authoritative; untyped fakes that already carry
        # model_id are accepted so hermetic tests need not import ArtifactType.
        if type_str and type_str != "routing":
            continue
        if type_str != "routing" and not (payload.get("model_id") or payload.get("model")):
            continue
        mid = payload.get("model_id") or payload.get("model") or ""
        if mid:
            model_id = str(mid)
    return model_id


def _stamp_agentic(
    result: "WorkerResult",
    pm_result=None,
    *,
    execution_pin: Optional["AgenticModelPin"] = None,
) -> "WorkerResult":
    """Label a WorkerResult as the agentic engine + routed model (best-effort)."""
    result.engine = "agentic"
    result.adapter = "agentic"
    if not (result.model or "").strip() and pm_result is not None:
        routed = _routed_model_id(pm_result)
        if routed:
            result.model = routed
    if execution_pin is not None:
        result.requested_model = execution_pin.requested
        result.provider = execution_pin.provider
        result.routing_policy = execution_pin.policy
    return result
