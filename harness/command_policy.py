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
from dataclasses import dataclass

DEFAULT_TIMEOUT = 120


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


@dataclass
class CommandVerdict:
    danger: bool
    category: str   # "" when safe; else a short reason category
    reason: str     # human-readable explanation
    matched: str    # the pattern fragment that tripped it (for the UI)


# Each rule: (category, human reason, compiled regex). Ordered most-severe first.
# Patterns are matched case-insensitively against the raw command string.
_RULES = [
    ("destructive-recursive-delete",
     "recursive force delete",
     r"\brm\s+(-[a-z]*\s+)*-[a-z]*r[a-z]*f|\brm\s+(-[a-z]*\s+)*-[a-z]*f[a-z]*r|\brm\s+-[rf]{2}\b"),
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
    in full-auto mode. Never raises."""
    cmd = (command or "").strip()
    if not cmd:
        return CommandVerdict(False, "", "", "")
    for cat, reason, rx in _COMPILED:
        m = rx.search(cmd)
        if m:
            return CommandVerdict(True, cat, reason, m.group(0)[:80])
    return CommandVerdict(False, "", "", "")


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
    just the parent shell) the moment either fires.

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

    Returns (output: str, exit_code: int, status: str) where status is one of
    "ok" | "cancelled" | "timeout" | "error". Never raises.
    """
    import signal
    import time as _time

    # Snapshot the cancel flag BEFORE launch. A flag already set here is stale
    # (sibling-stream poison / leftover interrupt), not a stop aimed at us.
    stale_cancel = cancel_event is not None and cancel_event.is_set()
    start = _time.monotonic()
    try:
        # start_new_session=True puts the child in its own process group so we
        # can signal the entire tree (shell + everything it spawned).
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except Exception as e:
        return (f"Failed to execute command: {e}", -1, "error")

    def _kill_group():
        # Capture the group id BEFORE the parent exits -- once proc is reaped,
        # os.getpgid(proc.pid) raises and we lose the ability to sweep survivors.
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:
            pgid = None

        if pgid is not None:
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

        # Wait for the PARENT shell to exit on SIGTERM.
        try:
            proc.wait(timeout=3)
        except Exception:
            pass

        # ALWAYS SIGKILL the whole group afterward -- do NOT make this conditional
        # on the parent surviving. On Linux a backgrounded child ("cmd & cmd &
        # wait") can outlive the shell: the shell exits on SIGTERM (so proc.wait
        # succeeds) while a child ignored/escaped SIGTERM and lingers. The old code
        # skipped SIGKILL whenever the parent exited, orphaning that child. A final
        # unconditional group SIGKILL reaps any survivor; signalling an
        # already-dead group is a harmless no-op (ProcessLookupError, swallowed).
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                pass
        try:
            proc.kill()
        except Exception:
            pass

    status = "ok"
    while True:
        try:
            proc.wait(timeout=poll_interval)
            break  # process finished on its own
        except subprocess.TimeoutExpired:
            pass
        if cancel_event is not None and cancel_event.is_set() and not stale_cancel:
            _kill_group()
            status = "cancelled"
            break
        if timeout is not None and (_time.monotonic() - start) >= timeout:
            _kill_group()
            status = "timeout"
            break

    try:
        output = proc.stdout.read() if proc.stdout else ""
    except Exception:
        output = ""
    exit_code = proc.returncode if proc.returncode is not None else -1
    if status == "cancelled":
        output = (output or "") + "\n\n[interrupted by user]"
        exit_code = 130  # conventional SIGINT exit code
    elif status == "timeout":
        output = (output or "") + f"\n\n[TimeoutExpired after {timeout} seconds]"
        exit_code = -1
    return (output, exit_code, status)
