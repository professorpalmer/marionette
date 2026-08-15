"""Workspace-aware preflight for ``run_command``.

Pilots routinely launch ``python -m pytest`` on the system interpreter and
``npm test`` at a monorepo root whose frontend lives in ``webapp/``. Those
commands are doomed before they start; post-failure hints in
``command_hints`` never rewrite them.

This module applies *unambiguous* rewrites only:

* Bare ``python`` / ``python3`` / ``pytest`` (including ``python -m pytest``)
  -> the workspace ``.venv`` interpreter, when that binary exists and the
  command is not already using it.
* ``npm`` / ``npx`` / ``pnpm`` / ``yarn`` when the repo has no root
  ``package.json`` but ``webapp/package.json`` exists -> run in ``webapp/``.
  Command text is left alone; scripts are never invented.

Ambiguous commands (pipelines, ``cd``, explicit prefix/cwd flags) are left
untouched. The functions never raise.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from typing import Any, Dict, List, Optional

# Error headers appear early; scanning further only adds false positives.
_OUTPUT_SCAN_CHARS = 4000

_BARE_PYTHONS = frozenset({"python", "python3"})
_BARE_PYTEST = "pytest"
_NODE_CLIENTS = frozenset({"npm", "npx", "pnpm", "yarn"})
_NODE_DIR_FLAGS = frozenset({"--prefix", "--cwd", "--dir", "-C"})
_SHELL_CONTROL = frozenset({"&&", "||", "|", ";", "&", ">", ">>", "<", "$(", "`"})
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_WIN_SUFFIXES = (".exe", ".cmd", ".bat")

# Output shapes that mean "wrong interpreter / wrong cwd", not a product bug.
_PYTEST_MODULE_RE = re.compile(
    r"(?:ModuleNotFoundError|ImportError): No module named '?_?pytest",
)
_NPM_PACKAGE_JSON_RE = re.compile(
    r"ENOENT[^\n]*package\.json|package\.json[^\n]*ENOENT|"
    r"Could not (?:read|find) package\.json|"
    r"Couldn't find a package\.json|"
    r"No package\.json",
    re.I,
)
_CMD_NOT_FOUND_RE = re.compile(
    r"(?:bash: line \d+: |bash: |sh: \d*:? ?|zsh: )?(python3?|pytest): "
    r"(?:command )?not found",
)


def _identity(command: str, repo: str, argv: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "command": command or "",
        "cwd": repo or "",
        "argv": list(argv or []),
        "rewritten": False,
        "reason": None,
        "kind": None,
    }


def _program_name(token: str) -> str:
    base = (token or "").strip().strip("\"'").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if os.name == "nt":
        lowered = base.lower()
        for suffix in _WIN_SUFFIXES:
            if lowered.endswith(suffix):
                return base[: -len(suffix)]
    return base


def _is_bare(token: str, names: frozenset) -> bool:
    raw = (token or "").strip().strip("\"'")
    if not raw or "/" in raw or "\\" in raw:
        return False
    return _program_name(raw) in names


def _tokenize(command: str) -> Optional[List[str]]:
    text = command or ""
    if not text.strip():
        return []
    try:
        tokens = shlex.split(text)
    except ValueError:
        return None
    return tokens


def _skip_env_assignments(tokens: List[str]) -> int:
    idx = 0
    while idx < len(tokens) and _ENV_ASSIGNMENT_RE.match(tokens[idx]):
        idx += 1
    return idx


def _has_shell_control(tokens: List[str]) -> bool:
    for token in tokens:
        if token in _SHELL_CONTROL:
            return True
        if token.startswith(">") or token.startswith("<"):
            return True
    return False


def _join_shell(tokens: List[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(tokens)
    return " ".join(shlex.quote(part) for part in tokens)


def venv_python_path(repo: str) -> Optional[str]:
    """Absolute path to ``repo/.venv``'s python, or None if it is absent."""
    if not repo:
        return None
    if os.name == "nt":
        rel = os.path.join(".venv", "Scripts", "python.exe")
    else:
        rel = os.path.join(".venv", "bin", "python")
    path = os.path.join(repo, rel)
    try:
        if os.path.isfile(path):
            return path
    except Exception:
        return None
    return None


def _same_interpreter(token: str, venv_python: str, repo: str) -> bool:
    raw = (token or "").strip().strip("\"'")
    if not raw:
        return False
    try:
        candidate = raw if os.path.isabs(raw) else os.path.join(repo, raw)
        return os.path.normcase(os.path.normpath(candidate)) == os.path.normcase(
            os.path.normpath(venv_python)
        )
    except Exception:
        return False


def _rewrite_interpreter(
    tokens: List[str],
    prog_idx: int,
    venv_python: str,
) -> List[str]:
    program = _program_name(tokens[prog_idx])
    prefix = tokens[:prog_idx]
    rest = tokens[prog_idx + 1 :]
    if program == _BARE_PYTEST:
        return prefix + [venv_python, "-m", "pytest"] + rest
    return prefix + [venv_python] + rest


def resolve_command_preflight(command: str, repo: str) -> Dict[str, Any]:
    """Return an unambiguous rewrite of ``command`` for ``repo``, or the original.

    Result keys: ``command``, ``cwd``, ``argv``, ``rewritten``, ``reason``,
    ``kind`` (None, ``interpreter_rewrite``, or ``cwd_rewrite``).
    ``env_prerequisite`` is a post-run failure class from
    ``classify_env_prerequisite_failure``, not a rewrite kind.
    """
    try:
        return _resolve_command_preflight(command, repo)
    except Exception:
        return _identity(command or "", repo or "")


def _resolve_command_preflight(command: str, repo: str) -> Dict[str, Any]:
    text = command if isinstance(command, str) else ""
    workspace = repo if isinstance(repo, str) else ""
    tokens = _tokenize(text)
    if tokens is None:
        return _identity(text, workspace)
    if not tokens:
        return _identity(text, workspace, [])

    if "`" in text or "$(" in text or _has_shell_control(tokens):
        return _identity(text, workspace, tokens)

    prog_idx = _skip_env_assignments(tokens)
    if prog_idx >= len(tokens):
        return _identity(text, workspace, tokens)

    program_token = tokens[prog_idx]
    if _program_name(program_token) == "cd":
        return _identity(text, workspace, tokens)

    venv_python = venv_python_path(workspace)
    if venv_python and (
        _is_bare(program_token, _BARE_PYTHONS)
        or _is_bare(program_token, frozenset({_BARE_PYTEST}))
    ):
        if not _same_interpreter(program_token, venv_python, workspace):
            rewritten_tokens = _rewrite_interpreter(tokens, prog_idx, venv_python)
            rewritten_command = _join_shell(rewritten_tokens)
            return {
                "command": rewritten_command,
                "cwd": workspace,
                "argv": rewritten_tokens,
                "rewritten": True,
                "reason": (
                    "Using workspace .venv interpreter: " + rewritten_command
                ),
                "kind": "interpreter_rewrite",
            }
        return _identity(text, workspace, tokens)

    if _is_bare(program_token, _NODE_CLIENTS):
        if any(token in _NODE_DIR_FLAGS for token in tokens[prog_idx + 1 :]):
            return _identity(text, workspace, tokens)
        root_pkg = os.path.join(workspace, "package.json") if workspace else ""
        webapp_pkg = (
            os.path.join(workspace, "webapp", "package.json") if workspace else ""
        )
        try:
            root_exists = bool(root_pkg) and os.path.isfile(root_pkg)
            webapp_exists = bool(webapp_pkg) and os.path.isfile(webapp_pkg)
        except Exception:
            return _identity(text, workspace, tokens)
        if (not root_exists) and webapp_exists:
            webapp_cwd = os.path.join(workspace, "webapp")
            return {
                "command": text,
                "cwd": webapp_cwd,
                "argv": tokens,
                "rewritten": True,
                "reason": (
                    "No package.json at workspace root; running in webapp/."
                ),
                "kind": "cwd_rewrite",
            }

    return _identity(text, workspace, tokens)


def classify_env_prerequisite_failure(
    command: str,
    exit_code: int,
    output: str,
) -> Optional[str]:
    """Return ``env_prerequisite`` when output matches a known env-gap shape.

    Does not change ok/status semantics; callers attach this beside the
    existing post-failure hint. Returns None for clean or informational exits.
    """
    try:
        if int(exit_code) == 0:
            return None
    except (TypeError, ValueError):
        return None
    try:
        from .command_hints import is_informational_exit

        if is_informational_exit(command or "", int(exit_code)):
            return None
    except Exception:
        pass
    window = (output or "")[:_OUTPUT_SCAN_CHARS]
    if not window:
        return None
    try:
        if _PYTEST_MODULE_RE.search(window):
            return "env_prerequisite"
        if _NPM_PACKAGE_JSON_RE.search(window):
            return "env_prerequisite"
        if _CMD_NOT_FOUND_RE.search(window):
            return "env_prerequisite"
    except Exception:
        return None
    return None
