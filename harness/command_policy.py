"""Command execution policy: timeout resolution + danger classification.

PM-free and pure so it unit-tests fast and hermetically (AGENTS.md: the intent/
policy layer stays execution-free). Two responsibilities:

1. resolve_timeout(): how long a shell command may run. Hermes lets you turn
   timeouts off; we mirror that via HARNESS_COMMAND_TIMEOUT (seconds; 0 or
   "none"/"off" => unbounded). Default stays 120s so a fresh full-auto session
   cannot launch an unbounded remote command out of the box.

2. classify_command(): screen a shell command for irreversible or remote-reaching
   operations BEFORE execution. In full-auto (unattended) mode the harness pauses
   on a DANGER verdict and requires human approval -- the safety Hermes gets from
   its interactive destructive-op confirmation, which an autonomous loop otherwise
   lacks. In interactive co-working the human already sees every command, so the
   guard only bites in auto-mode.

The classifier is intentionally conservative: it flags by PATTERN, accepts that it
will sometimes flag a benign command (a false positive costs one approval click),
and never tries to "sanitize" or rewrite a command -- it only labels it.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Iterable

DEFAULT_TIMEOUT = 120
# When the operator sets HARNESS_COMMAND_TIMEOUT=0/off, commands are "unbounded"
# but still get this safety ceiling so a hung pytest cannot pin the turn forever.
# Set HARNESS_COMMAND_HARD_CEILING=0/off to opt out of the ceiling entirely.
DEFAULT_HARD_CEILING = 900
MAX_CAPTURED_OUTPUT = 2 * 1024 * 1024  # 2 MiB

# Explicit ownership registry for Marionette-spawned run_cancellable trees.
# Keyed by id(cancel_event) (or another owner token) — NEVER by cwd scan.
# Stop/interrupt reaps only these handles so user terminals and foreign
# Puppetmaster dashboard processes stay untouched.
_owned_command_lock = threading.Lock()
_owned_command_procs: dict[int, dict[int, Any]] = {}


def _owner_token(owner: Any) -> int | None:
    if owner is None:
        return None
    try:
        return id(owner)
    except Exception:
        return None


def register_owned_command_proc(owner: Any, proc: Any) -> None:
    """Record a Marionette-owned tool process for Stop hard-cancel."""
    token = _owner_token(owner)
    pid = getattr(proc, "pid", None)
    if token is None or pid is None:
        return
    try:
        pid_i = int(pid)
    except Exception:
        return
    if pid_i <= 1:
        return
    with _owned_command_lock:
        bucket = _owned_command_procs.setdefault(token, {})
        bucket[pid_i] = proc


def unregister_owned_command_proc(owner: Any, proc: Any) -> None:
    """Drop a finished/reaped owned tool process (PID-reuse safe)."""
    token = _owner_token(owner)
    pid = getattr(proc, "pid", None)
    if token is None or pid is None:
        return
    try:
        pid_i = int(pid)
    except Exception:
        return
    with _owned_command_lock:
        bucket = _owned_command_procs.get(token)
        if not bucket:
            return
        bucket.pop(pid_i, None)
        if not bucket:
            _owned_command_procs.pop(token, None)


def clear_owned_command_registry_for_tests() -> None:
    """Test helper: drop all owned command-process provenance."""
    with _owned_command_lock:
        _owned_command_procs.clear()


def owned_command_pids_for_tests(owner: Any = None) -> list[int]:
    """Test helper: list registered owned PIDs (optionally for one owner)."""
    with _owned_command_lock:
        if owner is None:
            out: list[int] = []
            for bucket in _owned_command_procs.values():
                out.extend(bucket.keys())
            return out
        token = _owner_token(owner)
        if token is None:
            return []
        return list(_owned_command_procs.get(token, {}).keys())


def kill_process_group(proc: Any) -> bool:
    """Kill a process group spawned by run_cancellable. Best-effort; never raises.

    Mirrors the Windows CREATE_NEW_PROCESS_GROUP + taskkill /T pattern and the
    POSIX start_new_session + killpg pattern already used inside run_cancellable.
    Returns True when a kill was attempted.
    """
    if proc is None:
        return False
    pid = getattr(proc, "pid", None)
    if pid is None:
        return False
    try:
        pid_i = int(pid)
    except Exception:
        return False
    if pid_i <= 1:
        return False
    me = os.getpid()
    parent = os.getppid()
    if pid_i in (me, parent):
        return False

    import signal

    if os.name != "posix":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid_i), "/T", "/F"],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        return True

    try:
        pgid = os.getpgid(pid_i)
    except Exception:
        pgid = None

    if pgid is not None and pgid > 1:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
    else:
        try:
            proc.terminate()
        except Exception:
            pass

    try:
        proc.wait(timeout=3)
    except Exception:
        pass

    if pgid is not None and pgid > 1:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass
    return True


def kill_owned_command_procs(owners: Iterable[Any]) -> dict[str, Any]:
    """Synchronously kill Marionette-owned command trees for the given owners.

    Ownership is registry-only (register_owned_command_proc). Foreign processes
    whose cwd happens to match the workspace are never targeted.
    """
    tokens: list[int] = []
    for owner in owners or ():
        token = _owner_token(owner)
        if token is not None and token not in tokens:
            tokens.append(token)
    if not tokens:
        return {"killed": 0, "signaled": [], "orphaned": []}

    targets: list[tuple[int, int, Any]] = []  # token, pid, proc
    with _owned_command_lock:
        for token in tokens:
            bucket = _owned_command_procs.get(token) or {}
            for pid, proc in list(bucket.items()):
                targets.append((token, int(pid), proc))

    signaled: list[int] = []
    orphaned: list[int] = []
    me = os.getpid()
    parent = os.getppid()
    for token, pid, proc in targets:
        if pid <= 1 or pid in (me, parent):
            with _owned_command_lock:
                bucket = _owned_command_procs.get(token)
                if bucket is not None:
                    bucket.pop(pid, None)
                    if not bucket:
                        _owned_command_procs.pop(token, None)
            continue
        try:
            alive_before = proc.poll() is None
        except Exception:
            alive_before = True
        if alive_before:
            kill_process_group(proc)
            signaled.append(pid)
        try:
            still_alive = proc.poll() is None
        except Exception:
            still_alive = False
        if still_alive:
            orphaned.append(pid)
        with _owned_command_lock:
            bucket = _owned_command_procs.get(token)
            if bucket is not None:
                bucket.pop(pid, None)
                if not bucket:
                    _owned_command_procs.pop(token, None)

    return {
        "killed": len(signaled),
        "signaled": signaled,
        "orphaned": orphaned,
    }


def resolve_timeout(env: dict | None = None) -> int | None:
    """Return the per-command timeout in seconds, or None for unbounded.

    HARNESS_COMMAND_TIMEOUT: integer seconds. 0, "none", "off", "" -> unbounded
    means the operator explicitly opted out. Unset -> DEFAULT_TIMEOUT.
    A malformed value falls back to the default (fail safe, not fail open).
    """
    env = env if env is not None else os.environ
    raw = (env.get("HARNESS_COMMAND_TIMEOUT", "") or "").strip().lower()
    if raw == "":
        return DEFAULT_TIMEOUT
    if raw in ("0", "none", "off", "unbounded", "infinite"):
        return None
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_TIMEOUT
    if val <= 0:
        return None
    return val


def resolve_hard_ceiling(env: dict | None = None) -> int | None:
    """Safety ceiling (seconds) applied when resolve_timeout returns None.

    HARNESS_COMMAND_HARD_CEILING: unset -> DEFAULT_HARD_CEILING (900).
    0 / none / off / unbounded / infinite -> None (truly unbounded).
    Malformed values fall back to DEFAULT_HARD_CEILING (fail safe).
    """
    env = env if env is not None else os.environ
    raw = (env.get("HARNESS_COMMAND_HARD_CEILING", "") or "").strip().lower()
    if raw == "":
        return DEFAULT_HARD_CEILING
    if raw in ("0", "none", "off", "unbounded", "infinite"):
        return None
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_HARD_CEILING
    if val <= 0:
        return None
    return val


def effective_command_timeout(env: dict | None = None) -> int | None:
    """Timeout actually used for run_command / batch jobs.

    Explicit HARNESS_COMMAND_TIMEOUT wins. When that is unbounded (None), the
    hard ceiling still applies unless the operator disabled it too.
    """
    env = env if env is not None else os.environ
    timeout = resolve_timeout(env)
    if timeout is not None:
        return timeout
    return resolve_hard_ceiling(env)


@dataclass
class CommandVerdict:
    danger: bool
    category: str   # "" when safe; else a short reason category
    reason: str     # human-readable explanation
    matched: str    # the pattern fragment that tripped it (for the UI)


# Windows catastrophic delete targets: drive root, root glob, or a top-level
# system directory (mirrors the POSIX rm -rf /etc-style boundary logic).
_WIN_SYSTEM_DIRS = (
    r"windows|program\s+files(?:\s+\(x86\))?|users|perflogs|programdata|"
    r"system32|syswow64|recovery|boot"
)
_WIN_CATASTROPHIC_TARGET = (
    r"[a-zA-Z]:(?:"
    r"\\\*"  # C:\*
    r"|\\?(?:\\(?:" + _WIN_SYSTEM_DIRS + r")(?:\\.*)?)?"  # C:\Windows, C:\, ...
    r")"
)

# Each rule: (category, human reason, compiled regex). Ordered most-severe first.
# Patterns are matched case-insensitively against the raw command string.
_RULES = [
    ("destructive-recursive-delete",
     "recursive force delete",
     r"\brm\s+(-[a-z]*\s+)*-[a-z]*r[a-z]*f|\brm\s+(-[a-z]*\s+)*-[a-z]*f[a-z]*r|\brm\s+-[rf]{2}\b"),
    ("destructive-recursive-delete",
     "recursive force delete (Windows cmd)",
     r"\brd(?:\s+/[a-z]+)+\s+" + _WIN_CATASTROPHIC_TARGET),
    ("destructive-recursive-delete",
     "recursive force delete (Windows cmd)",
     r"\brmdir(?:\s+/[a-z]+)+\s+" + _WIN_CATASTROPHIC_TARGET),
    ("destructive-recursive-delete",
     "recursive force delete (Windows cmd)",
     r"\bdel(?:\s+/[a-z]+)+\s+" + _WIN_CATASTROPHIC_TARGET),
    ("destructive-recursive-delete",
     "disk format (Windows)",
     r"\bformat\s+[a-zA-Z]:(?:\s|$)"),
    ("destructive-recursive-delete",
     "recursive force delete (PowerShell)",
     r"remove-item\b[^\n;|&`]*-(?:recurse|force)\b[^\n;|&`]*-(?:recurse|force)\b[^\n;|&`]*"
     + _WIN_CATASTROPHIC_TARGET),
    ("disk-write",
     "raw disk / filesystem write",
     r"\b(dd|mkfs|fdisk|parted|wipefs)\b|>\s*/dev/(sd|nvme|disk|rdisk)"),
    ("device-redirect",
     "redirect to a device or critical path",
     r">\s*/dev/(?!null|stdout|stderr)|>\s*/etc/|>\s*/boot/"),
    ("remote-shell",
     "remote machine access (ssh/scp/rsync to a host)",
     r"\bssh\s+[^\s]|\bscp\s+|\brsync\s+[^\n]*@[^\s]*:|\brsync\s+[^\n]*::|\bsftp\s+"),
    ("pipe-to-shell",
     "download piped directly into a shell",
     r"(curl|wget|fetch)\b[^|]*\|\s*(sudo\s+)?(ba|z|k|c|fi|da)?sh\b"),
    ("dynamic-code-exec",
     "execution of base64-decoded or dynamically evaluated content",
     r"base64\s+(-d|--decode)\s*\||\beval\s+\$\("),
    ("shell-exec-fetch",
     "shell executing a fetched command",
     r"\b(ba|z|k|c|fi|da)?sh\s+-c\s+.*(curl|wget|fetch)\b"),
    ("force-push",
     "history-rewriting git push",
     r"\bgit\s+push\b[^\n]*(--force(?!-with-lease)|\s-f\b)"),
    ("privilege-escalation",
     "privilege escalation",
     r"\bsudo\b|\bsu\s+-|\bdoas\b"),
    ("system-control",
     "service / power state change",
     r"\b(shutdown|reboot|halt|poweroff)\b|\bsystemctl\s+(stop|disable|mask)\b|\bkillall\b"),
    ("ownership-perms",
     "broad ownership or permission change",
     r"\bchmod\s+(-[a-z]*\s+)*-R\b|\bchown\s+(-[a-z]*\s+)*-R\b|\bchmod\s+777\b"),
    ("fork-bomb",
     "fork bomb",
     r":\(\)\s*\{\s*:\|:&\s*\}\s*;"),
    ("secret-exfil",
     "reading credential / key material",
     r"(cat|less|more|head|tail|cp|scp)\s+[^\n]*(\.ssh/|id_rsa|id_ed25519|\.env\b|\.aws/credentials|\.pem\b)"),
]

_COMPILED = [(cat, reason, re.compile(pat, re.IGNORECASE)) for cat, reason, pat in _RULES]


def classify_command(command: str) -> CommandVerdict:
    """Classify a shell command. Returns a CommandVerdict; danger=True means the
    command matches an irreversible/remote/escalating pattern and should be gated
    in full-auto mode. Never raises.

    This is a best-effort, full-auto SAFETY GATE, not a sandbox. It is intended to
    catch obvious high-signal "danger" patterns, not to be a comprehensive command
    auditor resistant to adversarial obfuscation. The intentional shell=True design
    of the harness command runner is a deliberate choice; this classifier is a
    defense-in-depth hardening measure, not a primary security boundary.
    """
    cmd = (command or "").strip()
    if not cmd:
        return CommandVerdict(False, "", "", "")
    for cat, reason, rx in _COMPILED:
        m = rx.search(cmd)
        if m:
            return CommandVerdict(True, cat, reason, m.group(0)[:80])
    return CommandVerdict(False, "", "", "")


def guard_destructive_command(command: str) -> CommandVerdict:
    """Classify, then keep an extra latch after a context switch.

    Safe (non-danger) commands always pass through. Classify categories
    (remote-shell, force-push, ...) win over ``context-switch-unconfirmed``.
    The latch only supplies that category when classify did not already
    name a danger class — it must not rewrite ssh/systemctl to a switch miss.
    """
    verdict = classify_command(command)
    if verdict.danger and verdict.category:
        return verdict
    if not verdict.danger:
        return verdict
    try:
        from .context_switch_guard import is_armed, snapshot
    except Exception:
        return verdict
    if not is_armed():
        return verdict
    snap = snapshot()
    pending = (snap.get("new") or snap.get("kind") or "new workspace").strip()
    return CommandVerdict(
        True,
        "context-switch-unconfirmed",
        f"destructive command blocked until the new workspace is confirmed ({pending})",
        (verdict.matched or command or "")[:80],
    )


# Known rewrites only — not a general sanitizer. First rewrite: bare force-push
# (`--force` / `-f`, but not `--force-with-lease`) → `--force-with-lease`.
_FORCE_PUSH_LONG = re.compile(r"--force(?!-with-lease)\b", re.IGNORECASE)
_FORCE_PUSH_SHORT = re.compile(r"(?<![\w-])-f(?![\w-])")
_GIT_PUSH = re.compile(r"\bgit\s+push\b", re.IGNORECASE)


def suggested_amendment(command: str) -> str | None:
    """Return a safer rewrite for known patterns, else ``None``.

    Only encodes curated substitutions (today: force-push → force-with-lease).
    Does not invent a general command sanitizer.
    """
    cmd = command or ""
    if not _GIT_PUSH.search(cmd):
        return None
    # Already the safe variant — never rewrite --force-with-lease.
    if _FORCE_PUSH_LONG.search(cmd) is None and _FORCE_PUSH_SHORT.search(cmd) is None:
        return None
    amended = _FORCE_PUSH_LONG.sub("--force-with-lease", cmd)
    amended = _FORCE_PUSH_SHORT.sub("--force-with-lease", amended)
    if amended == cmd:
        return None
    return amended


def smart_approve(
    command: str,
    *,
    allowlist_hit: bool = False,
    verdict: CommandVerdict | None = None,
) -> dict[str, str]:
    """Deterministic smart-approve verdict for a full-auto danger gate.

    Delegates to :func:`harness.smart_approve.smart_approve` so there is one
    precedence: allowlist hit → approve, suggested amendment → amend, else
    pending. Never auto-executes on its own.
    """
    from .smart_approve import smart_approve as _smart_approve

    return _smart_approve(
        command,
        allowlist_hit=allowlist_hit,
        verdict=verdict,
    )


def run_cancellable(
    command: str,
    *,
    cwd: str | None = None,
    timeout: int | None = None,
    cancel_event=None,
    poll_interval: float = 0.1,
):
    """Run a shell command that can be KILLED mid-flight by a cancel event.

    The stdlib subprocess.run(timeout=...) blocks the calling thread
    uninterruptibly: a user Stop sets a flag but the process keeps running until
    it exits or times out. With timeouts now optionally unbounded, that means
    Stop could not kill a long/infinite command. This runner instead launches the
    process in its OWN process group and polls cancel_event (and the deadline)
    while waiting, killing the whole group (so shell=True children die too, not
    just the parent shell) the moment either fires. Spawned handles are also
    registered under ``cancel_event`` so ``BusyControlMixin.interrupt`` can
    hard-cancel owned trees immediately (registry-only; never cwd scan).

    Cancellation is EDGE-triggered, not level-triggered. cancel_event is a
    process-global flag on a shared session: a sibling stream disconnect or a
    stale interrupt from a prior turn can leave it set. If we honored a flag that
    was ALREADY set the moment this command launched, a fresh command would be
    killed instantly and mislabeled "[interrupted by user]" -- exactly the
    "every shell command dies but reads work" failure. So we snapshot the flag at
    launch and only treat a clear->set transition DURING the run as a real Stop.
    A genuine Stop that predates this command has already halted the turn's action
    loop before we get here; ignoring a pre-set flag for this one command is safe
    (the loop's own cancel check still halts the turn afterward, with the command's
    output preserved instead of destroyed).

    A runaway command's output is capped at MAX_CAPTURED_OUTPUT bytes to avoid
    exhausting memory. When the cap is hit, the process group is killed and the
    output is marked as truncated.

    Returns (output: str, exit_code: int, status: str) where status is one of
    "ok" | "cancelled" | "timeout" | "truncated" | "error". Never raises.
    """
    import time as _time
    try:
        import fcntl
    except ImportError:  # Windows: no fcntl; blocking-read fallback below applies
        fcntl = None

    # Snapshot the cancel flag BEFORE launch. A flag already set here is stale
    # (sibling-stream poison / leftover interrupt), not a stop aimed at us.
    stale_cancel = cancel_event is not None and cancel_event.is_set()
    start = _time.monotonic()

    from harness import os_sandbox

    spawn_command = command
    sandbox_cleanup = None
    spawn_env = None
    try:
        plan = os_sandbox.prepare_sandbox_spawn(command, cwd=cwd)
        if plan is not None:
            spawn_command = plan.command
            sandbox_cleanup = plan.cleanup
            if plan.child_env:
                spawn_env = os.environ.copy()
                spawn_env.update(plan.child_env)
    except os_sandbox.SandboxRequiredUnavailable as exc:
        return (exc.message, -1, "error")

    try:
        # Put the child in its own process group so we can signal the entire
        # tree (shell + everything it spawned). start_new_session is POSIX-only;
        # on Windows the equivalent is the CREATE_NEW_PROCESS_GROUP flag, and
        # tree-kill goes through taskkill in kill_process_group. harness.win_console
        # then ORs CREATE_NO_WINDOW onto this flag (CREATE_NEW_PROCESS_GROUP is
        # not an explicit console choice), so run_command retains
        # CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW on Windows.
        group_kwargs = (
            {"start_new_session": True}
            if os.name == "posix"
            else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        )
        proc = subprocess.Popen(
            spawn_command,
            shell=True,
            cwd=cwd,
            env=spawn_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **group_kwargs,
        )
    except Exception as e:
        if sandbox_cleanup is not None:
            sandbox_cleanup()
        return (f"Failed to execute command: {e}", -1, "error")

    # Ownership key is the cancel Event (session._cancel or per-job Event).
    # Interrupt looks up the same tokens — never invents ownership by cwd.
    register_owned_command_proc(cancel_event, proc)

    try:
        return _run_cancellable_wait(
            proc,
            cancel_event=cancel_event,
            stale_cancel=stale_cancel,
            timeout=timeout,
            poll_interval=poll_interval,
            start=start,
            fcntl=fcntl,
        )
    finally:
        unregister_owned_command_proc(cancel_event, proc)
        if sandbox_cleanup is not None:
            sandbox_cleanup()


def _run_cancellable_wait(
    proc,
    *,
    cancel_event,
    stale_cancel: bool,
    timeout: int | None,
    poll_interval: float,
    start: float,
    fcntl,
):
    """Poll/capture loop for an already-spawned owned command process."""
    import time as _time

    # Set the pipe to non-blocking so we can read from it without stalling.
    nonblocking = False
    if proc.stdout and fcntl is not None:
        try:
            fd = proc.stdout.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            nonblocking = True
        except Exception:
            pass

    # Without a non-blocking pipe (Windows has no fcntl), an inline read()
    # blocks until the child exits -- which would starve the cancel/timeout
    # polling below and make Stop a no-op. Drain the pipe from a daemon
    # thread instead so the poll loop stays responsive.
    _threaded_chunks: list = []
    _drain_thread = None
    if proc.stdout and not nonblocking:
        import threading as _threading

        def _drain_pipe():
            try:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    _threaded_chunks.append(chunk)
            except Exception:
                pass

        _drain_thread = _threading.Thread(target=_drain_pipe, daemon=True)
        _drain_thread.start()

    def _kill_group():
        kill_process_group(proc)

    output_chunks = []
    total_read = 0
    status = "ok"

    while proc.poll() is None:
        if _drain_thread is not None:
            # Reader thread owns the pipe; harvest what it has collected so far.
            while _threaded_chunks:
                chunk = _threaded_chunks.pop(0)
                output_chunks.append(chunk)
                total_read += len(chunk)
        elif proc.stdout:
            try:
                chunk = proc.stdout.read(65536)
                if chunk:
                    output_chunks.append(chunk)
                    total_read += len(chunk)
            except (IOError, TypeError):
                # IOError/TypeError on read from a closed/non-blocking pipe is fine.
                pass

        if total_read > MAX_CAPTURED_OUTPUT:
            _kill_group()
            status = "truncated"
            break

        if cancel_event is not None and cancel_event.is_set() and not stale_cancel:
            _kill_group()
            status = "cancelled"
            break
        if timeout is not None and (_time.monotonic() - start) >= timeout:
            _kill_group()
            status = "timeout"
            break
        _time.sleep(poll_interval)

    # One final read to drain the pipe after the process has exited.
    if _drain_thread is not None:
        _drain_thread.join(timeout=2)
        while _threaded_chunks:
            chunk = _threaded_chunks.pop(0)
            output_chunks.append(chunk)
            total_read += len(chunk)
    elif proc.stdout:
        try:
            chunk = proc.stdout.read()
            if chunk:
                output_chunks.append(chunk)
                total_read += len(chunk)
        except (IOError, TypeError):
            pass

    # External Stop may have group-killed us while we slept in poll_interval.
    # Honor a clear->set cancel that outlived the child even if the loop
    # exited via poll() rather than the cancel branch.
    if (
        status == "ok"
        and cancel_event is not None
        and cancel_event.is_set()
        and not stale_cancel
    ):
        status = "cancelled"

    output = "".join(output_chunks)
    if status != "truncated" and total_read > MAX_CAPTURED_OUTPUT:
        status = "truncated"

    if status == "truncated":
        output = output[:MAX_CAPTURED_OUTPUT]
        output += f"\n\n[output truncated at {int(MAX_CAPTURED_OUTPUT / 1024 / 1024)} MiB cap]"
        exit_code = -1
    else:
        exit_code = proc.returncode if proc.returncode is not None else -1
        if status == "cancelled":
            output = (output or "") + "\n\n[interrupted by user]"
            exit_code = 130  # conventional SIGINT exit code
        elif status == "timeout":
            output = (output or "") + f"\n\n[TimeoutExpired after {timeout} seconds]"
            exit_code = -1

    return (output, exit_code, status)
