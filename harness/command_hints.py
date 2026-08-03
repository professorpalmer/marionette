"""Actionable recovery hints for ``run_command`` results.

A raw nonzero exit plus stderr regularly costs the pilot one or more wasted
diagnostic turns (retrying ``python`` on a python3-only box, re-running a
merge that conflicted, re-sending a gh field the installed CLI rejects).
This module maps well-known failure shapes to a single short "do this next"
sentence.

Design rules, in the order they matter:

* Never annotate a clean exit, and never annotate an *informational* nonzero
  exit -- ``grep`` finding nothing and ``git diff --quiet`` reporting drift
  are answers, not failures.
* At most one hint per result; first match wins.
* Only the head of the output is scanned: error headers land early and deep
  output is noise.
* Pure functions -- no I/O, no config reads, no process launches.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

# Error headers appear early; scanning further only adds false positives.
_OUTPUT_SCAN_CHARS = 4000

# Programs whose exit code 1 means "no result / difference found" rather than
# "the command failed". Keyed on the program that determines the pipeline's
# exit status.
_EXIT_ONE_IS_INFORMATIONAL = frozenset({
    "ag",
    "ack",
    "cmp",
    "diff",
    "egrep",
    "fgrep",
    "grep",
    "rg",
    "test",
    "[",
})

# Shell wrappers that prefix the program actually being run.
_TRANSPARENT_PREFIXES = frozenset({"sudo", "command", "env", "nohup", "exec", "time"})

_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|[;|\n]")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def exit_status_program(command: str) -> str:
    """Return the program whose exit code the shell reports for ``command``.

    In a pipeline or ``&&`` chain the *last* segment sets ``$?``, so that is
    the segment worth classifying. Leading environment assignments and
    transparent wrappers (``sudo``, ``env``, ...) are skipped.
    """
    segments = [seg.strip() for seg in _SEGMENT_SPLIT_RE.split(command or "") if seg.strip()]
    if not segments:
        return ""
    tokens = segments[-1].split()
    for token in tokens:
        if _ENV_ASSIGNMENT_RE.match(token) or token in _TRANSPARENT_PREFIXES:
            continue
        return token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip("\"'")
    return ""


def is_informational_exit(command: str, exit_code: int) -> bool:
    """True when a nonzero exit is a documented answer, not a failure.

    Keeps ``search`` and ``diff`` style commands out of the hint path so the
    pilot is never told to "fix" a command that worked exactly as intended.
    """
    if exit_code != 1:
        return False
    program = exit_status_program(command)
    if program in _EXIT_ONE_IS_INFORMATIONAL:
        return True
    # `git diff --quiet` / `git diff --exit-code` deliberately exit 1 on drift.
    if program == "git" and re.search(r"(?<!\S)--(quiet|exit-code)\b", command or ""):
        return True
    return False


def _hint_command_not_found(command: str, output: str) -> Optional[str]:
    match = re.search(
        r"(?:bash: line \d+: |bash: |sh: \d*:? ?|zsh: )?([\w.+-]+): (?:command )?not found",
        output,
    )
    if not match:
        return None
    missing = match.group(1)
    if missing == "python":
        return (
            "No bare `python` on PATH — use `python3`, or the project venv's "
            "interpreter (e.g. .venv/bin/python)."
        )
    if missing == "pip":
        return (
            "No bare `pip` on PATH — use `pip3`, `python3 -m pip`, or the "
            "project venv's pip."
        )
    return (
        f"`{missing}` is not installed or not on PATH. Check with "
        f"`which {missing}` and use an absolute path or install it; "
        "re-running the same command will fail identically."
    )


def _hint_module_not_found(command: str, output: str) -> Optional[str]:
    match = re.search(r"(?:ModuleNotFoundError|ImportError): No module named '?([\w.]+)", output)
    if not match:
        return None
    return (
        f"Python cannot import '{match.group(1)}'. This is usually the wrong "
        "interpreter rather than a missing package: activate the project venv "
        "or invoke its python directly before installing anything."
    )


def _hint_merge_conflict(command: str, output: str) -> Optional[str]:
    if not re.search(r"^CONFLICT |Automatic merge failed|needs merge", output, re.M):
        return None
    return (
        "Git merge conflict — do not re-run this command. Resolve the listed "
        "files, `git add` them, then continue (or `--abort`)."
    )


def _hint_already_exists(command: str, output: str) -> Optional[str]:
    match = re.search(r"(?:fatal|error):.*?'([^']+)' already exists", output)
    if not match:
        return None
    return (
        f"'{match.group(1)}' already exists — an unchanged retry keeps failing. "
        "Reuse it, pick another name, or remove it first."
    )


def _hint_permission_denied(command: str, output: str) -> Optional[str]:
    if "Permission denied" not in output and "EACCES" not in output:
        return None
    return (
        "Permission denied. Check ownership/mode of the target path and prefer "
        "a user-writable location; only escalate if the task truly requires it."
    )


def _hint_no_such_file(command: str, output: str) -> Optional[str]:
    match = re.search(r"(?:cannot|No such file or directory)[^\n]*?'([^']+)'", output)
    if not match:
        match = re.search(r"([^\s:]+): No such file or directory", output)
    if not match:
        return None
    return (
        f"'{match.group(1)}' does not exist relative to the reported cwd. "
        "Confirm the path with list_dir before retrying — the working "
        "directory is the workspace root, not the last directory you cd'd to."
    )


# Ordered so the most specific shapes win.
_OUTPUT_HINTS: tuple[Callable[[str, str], Optional[str]], ...] = (
    _hint_merge_conflict,
    _hint_command_not_found,
    _hint_module_not_found,
    _hint_already_exists,
    _hint_no_such_file,
    _hint_permission_denied,
)

# Exit codes with a single unambiguous meaning across shells.
_EXIT_CODE_HINTS: dict[int, str] = {
    126: (
        "Exit 126: the file exists but is not executable — `chmod +x` it or "
        "invoke it through its interpreter (e.g. `bash script.sh`)."
    ),
    127: (
        "Exit 127: the shell could not find the command. Verify the program "
        "name and PATH instead of retrying verbatim."
    ),
    137: (
        "Exit 137: the process was SIGKILLed, usually out-of-memory. Reduce "
        "the workload before retrying."
    ),
}


def command_failure_hint(command: str, exit_code: int, output: str) -> Optional[str]:
    """Return one short recovery hint for a failed command, or None.

    Returns None for clean exits and for informational nonzero exits so a
    successful ``grep`` with no matches is never dressed up as a failure.
    """
    if exit_code == 0:
        return None
    if is_informational_exit(command, exit_code):
        return None
    window = (output or "")[:_OUTPUT_SCAN_CHARS]
    if window:
        for probe in _OUTPUT_HINTS:
            try:
                hint = probe(command or "", window)
            except Exception:
                continue
            if hint:
                return hint
    return _EXIT_CODE_HINTS.get(exit_code)


def blocked_command_recovery(command: str, command_hash: str) -> dict:
    """Secret-free recovery metadata for a full-auto blocked command.

    The retry handle is the command fingerprint the approval seam already
    keys on, so no new artifact is persisted and the raw command text never
    leaves this dict. Approving a handle still requires an operator; nothing
    here re-runs the command.
    """
    from .command_jobs import secret_free_command_preview

    return {
        "retry_handle": command_hash,
        "command_fingerprint": command_hash,
        "command_preview": secret_free_command_preview(command),
        "recovery": (
            "This command was not run and was not saved. An operator can "
            "approve the retry handle above to unblock exactly this command; "
            "otherwise choose a safer approach."
        ),
    }
