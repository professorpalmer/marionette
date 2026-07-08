from __future__ import annotations

"""AUTO-VERIFY: a FAST, project-appropriate check that runs inline in an
interactive pilot turn right after the agent edits files, so it can self-correct
before handing control back to the user.

This is the interactive-pilot cousin of the autonomous run's `verify_cmd`
machinery. The autonomous loop runs a full operator-supplied command between
whole cycles; here we run something SUB-10s and SCOPED TO THE CHANGED FILES
(syntax check / typecheck of just what was edited), because it executes inline in
a chat turn and must not stall the conversation with a full test suite.

Design constraints (see task spec):
  - stdlib only (subprocess/os), deterministic, no emojis.
  - detect_verify_command is conservative: return None when nothing sensible and
    fast is found rather than guessing an expensive command.
  - run_verify never raises; it returns (passed, truncated_output).
  - Prefer a per-file scoped check built from the changed files.
  - Builders return argv lists where feasible; run_verify uses shell=False for
    lists and shell=True only for legacy string commands (compound shell forms
    like `cd X && npx ...` or `make check`).
"""

import os
import shlex
import subprocess
from typing import List, Optional, Union

# Cap on output fed back into the chat transcript. Kept small: this is an inline
# tool observation, not a full log.
MAX_OUTPUT = 4000

# Module-level DEFAULT check used when scoping to changed files is not possible
# but we still know the ecosystem. Deliberately empty by default -- callers fall
# back to None (skip) rather than run something slow/unscoped.
DEFAULT = ""

# Command may be an argv list (preferred, shell=False) or a legacy shell string.
VerifyCommand = Union[str, List[str]]


def _rel(repo: str, path: str) -> str:
    """Best-effort repo-relative path for a possibly-absolute edited path."""
    try:
        if os.path.isabs(path):
            return os.path.relpath(path, repo)
        return path
    except Exception:
        return path


def _exists(repo: str, *parts: str) -> bool:
    return os.path.exists(os.path.join(repo, *parts))


def _read(repo: str, *parts: str) -> str:
    try:
        with open(os.path.join(repo, *parts), "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _has_package_json_script(repo: str, *scripts: str) -> bool:
    """True if package.json declares any of the named npm scripts."""
    import json

    raw = _read(repo, "package.json")
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    declared = data.get("scripts")
    if not isinstance(declared, dict):
        return False
    return any(s in declared for s in scripts)


def _makefile_has_target(repo: str, *targets: str) -> str | None:
    """Return the first present Makefile target name (as a make invocation
    fragment), else None. Only inspects line-leading `target:` declarations."""
    raw = _read(repo, "Makefile") or _read(repo, "makefile")
    if not raw:
        return None
    present = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if ":" not in stripped or stripped.startswith("\t"):
            continue
        name = stripped.split(":", 1)[0].strip()
        if name and all(c.isalnum() or c in "-_./" for c in name):
            present.add(name)
    for t in targets:
        if t in present:
            return t
    return None


def _find_tsconfig(repo: str, changed_files: list[str]) -> str | None:
    """Locate the nearest tsconfig.json, preferring one that scopes the edited
    project dir (e.g. webapp/tsconfig.json when the edit is under webapp/)."""
    ts_changed = [
        _rel(repo, p) for p in changed_files
        if p.endswith((".ts", ".tsx", ".mts", ".cts"))
    ]
    # Prefer a tsconfig that sits at or above an edited file's directory.
    for rel in ts_changed:
        d = os.path.dirname(rel)
        while True:
            cand = os.path.join(d, "tsconfig.json") if d else "tsconfig.json"
            if _exists(repo, cand):
                return cand
            if not d:
                break
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    # Common webapp layout.
    if _exists(repo, "webapp", "tsconfig.json"):
        return os.path.join("webapp", "tsconfig.json")
    if _exists(repo, "tsconfig.json"):
        return "tsconfig.json"
    return None


def _changed_of(changed_files: list[str], *exts: str) -> list[str]:
    return [p for p in changed_files if p.endswith(exts)]


def _npx_argv() -> str:
    """Resolve the npx launcher for list-form argv.

    On Windows, CreateProcess cannot run the npm/npx .cmd shims without a shell;
    keep compound `cd && npx` builders on the string+shell path instead. For
    root-level list argv we still use bare 'npx' (PATH resolution via shell=False
    works for .exe; .cmd may need PATHEXT). Prefer list form for python/py_compile
    and root tsc; subdir tsc stays string+shell.
    """
    return "npx"


def _tsc_command(tsconfig: str) -> VerifyCommand:
    """Build the `npx tsc --noEmit` invocation for a repo-relative tsconfig.

    When the tsconfig sits in a subproject (e.g. webapp/tsconfig.json), run
    from that directory via a compound shell string (`cd X && npx ...`): list
    argv cannot express `cd` without changing run_verify's cwd semantics, and
    on Windows npm/npx are .cmd shims that need shell=True. Root-level tsconfig
    returns an argv list (shell=False).
    """
    project_dir = os.path.dirname(tsconfig)
    if project_dir:
        tsc = _shell_join("npx", "tsc", "--noEmit", "-p", os.path.basename(tsconfig))
        return f"cd {_shell_join(project_dir)} && {tsc}"
    return [_npx_argv(), "tsc", "--noEmit", "-p", tsconfig]


def _shell_join(*args: str) -> str:
    """Join argv tokens into a shell command string.

    POSIX shells (sh/bash) expect shlex.quote per token. cmd.exe (Windows,
    shell=True) expects subprocess.list2cmdline semantics (double quotes).
    """
    if os.name == "nt":
        return subprocess.list2cmdline(list(args))
    return " ".join(shlex.quote(a) for a in args)


def _command_display(cmd: VerifyCommand) -> str:
    """Human-readable form for events / feedback (list -> shell-joined)."""
    if isinstance(cmd, list):
        return _shell_join(*cmd)
    return cmd


def build_scoped_command(repo: str, changed_files: list[str]) -> Optional[VerifyCommand]:
    """Given the changed files, build a FAST per-file syntax/type check scoped to
    just those files, or None when scoping is not possible.

    - Changed .py files    -> argv list: ``[python, -m, py_compile, ...]``
    - Changed .ts/.tsx     -> argv list for root tsconfig, or shell string when
                               the tsconfig lives in a subproject (`cd && npx`).

    Returns None if there are no supported changed files to scope to.
    """
    changed_files = list(changed_files or [])

    py = _changed_of(changed_files, ".py")
    ts = _changed_of(changed_files, ".ts", ".tsx", ".mts", ".cts")

    # Prefer typechecking TS/TSX when present (a package.json project).
    if ts:
        tsconfig = _find_tsconfig(repo, changed_files)
        if tsconfig:
            return _tsc_command(tsconfig)
        # No tsconfig: fall back to a loose per-file tsc syntax check (argv list).
        rel_ts = [_rel(repo, p) for p in ts]
        return [_npx_argv(), "tsc", "--noEmit", *rel_ts]

    if py:
        import sys

        py_exe = sys.executable or "python"
        rel_py = [_rel(repo, p) for p in py]
        return [py_exe, "-m", "py_compile", *rel_py]

    return None


def detect_verify_command(
    repo: str, changed_files: list[str] | None = None
) -> Optional[VerifyCommand]:
    """Detect a FAST, project-appropriate check from repo markers, scoped to the
    edited files when possible. Returns an argv list when feasible, a shell
    string for compound forms (`cd && ...`, `make`), or None when nothing
    sensible/fast is found.

    Priority (an explicit operator override is handled by the caller, not here):
      1. A webapp/ or package.json project declaring a `typecheck`/`build` script
         -> a `tsc --noEmit` (project-scoped via tsconfig when available).
      2. A Python project (pyproject.toml/setup.py) with a tests dir -> a FAST
         syntax check of the CHANGED .py files (py_compile), NOT a full pytest.
      3. A Makefile with a `check`/`lint` target -> `make <target>` (string).
      4. Else None.

    Keep it conservative and FAST (sub-10s ideal): this runs inline in a chat
    turn, so a scoped syntax/typecheck of the CHANGED files always beats a full
    test suite.
    """
    if not repo or not os.path.isdir(repo):
        return None
    changed_files = list(changed_files or [])

    is_web = _exists(repo, "webapp") or _exists(repo, "package.json")
    web_has_check = (
        _has_package_json_script(repo, "typecheck", "build")
        or _has_package_json_script_in(repo, "webapp", "typecheck", "build")
    )
    is_python = _exists(repo, "pyproject.toml") or _exists(repo, "setup.py")
    py_has_tests = _exists(repo, "tests") or _exists(repo, "test")

    ts_changed = _changed_of(changed_files, ".ts", ".tsx", ".mts", ".cts")
    py_changed = _changed_of(changed_files, ".py")

    # When we know exactly what was edited, the CHANGED FILES pick the check --
    # a mixed repo (webapp/ AND pyproject.toml) must run tsc for a .tsx edit and
    # a python syntax check for a .py edit, not always the first marker.
    if ts_changed and is_web:
        scoped = build_scoped_command(repo, changed_files)
        if scoped:
            return scoped
    if py_changed and is_python and py_has_tests:
        scoped = build_scoped_command(repo, py_changed)
        if scoped:
            return scoped

    # 1. TypeScript / webapp project -> scoped tsc --noEmit (marker-driven, e.g.
    #    when no specific changed files were supplied but a check script exists).
    if is_web and web_has_check:
        tsconfig = _find_tsconfig(repo, changed_files)
        if tsconfig:
            return _tsc_command(tsconfig)
        return [_npx_argv(), "tsc", "--noEmit"]

    # 2. Python project with tests but nothing python edited this turn: a full
    #    pytest is too slow inline, so skip.
    if is_python and py_has_tests and not py_changed:
        return None

    # 3. Makefile check/lint target (compound make stays on string+shell path).
    target = _makefile_has_target(repo, "check", "lint")
    if target:
        return f"make {target}"

    return None


def _has_package_json_script_in(repo: str, subdir: str, *scripts: str) -> bool:
    """package.json script check for a nested project dir (e.g. webapp/)."""
    import json

    raw = _read(repo, subdir, "package.json")
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    declared = data.get("scripts")
    if not isinstance(declared, dict):
        return False
    return any(s in declared for s in scripts)


def run_verify(
    repo: str,
    command: VerifyCommand,
    changed_files: list[str] | None = None,
    timeout: int = 30,
    cancel_event=None,
) -> tuple[bool, str]:
    """Run `command` with cwd=repo and a timeout. Return (passed, output) where
    output is truncated to MAX_OUTPUT chars. NEVER raises.

    Accepts an argv list (preferred: subprocess with shell=False) or a legacy
    shell string (shell=True fallback for operator overrides and compound forms
    like `cd webapp && npx tsc ...`).

    `changed_files` is accepted so callers can pass the same list used to build
    the command; it is not required here (the command is already scoped).
    """
    if not command:
        return True, ""
    cwd = repo or None
    use_shell = not isinstance(command, (list, tuple))
    display = _command_display(command)

    try:
        res = subprocess.run(
            command if use_shell else list(command),
            shell=use_shell,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        passed = (res.returncode == 0)
        output = res.stdout or ""
    except subprocess.TimeoutExpired as te:
        out = te.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        passed = False
        output = f"{out}\n[auto-verify timed out after {timeout} seconds]"
    except Exception as e:  # noqa: BLE001 - never raise into the chat turn
        passed = False
        output = f"[auto-verify could not run '{display}': {e}]"

    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + "\n[output truncated]"
    return passed, output
