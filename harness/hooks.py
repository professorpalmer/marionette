from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import tempfile
from typing import Any

from .secure_files import restrict_to_owner
from .diag import note as _diag
from .api.redaction import redact_api_secrets
from .workspace_hook_trust import TRUSTED, evaluate_hook_trust, remember_hooks

logger = logging.getLogger("harness.hooks")

ALLOWED_EVENTS = ["sessionStart", "sessionEnd", "preRun", "postRun"]
_HOOKS_JSON = os.path.join(os.path.expanduser("~/.pmharness"), "hooks.json")
_MAX_CONTEXT = 16 * 1024
_HOOK_TIMEOUT = 15
_SECRET_ENV_NAMES = {"password", "secret", "token", "api_key", "access_token", "refresh_token", "authorization", "cookie"}


def get_hooks() -> list[dict]:
    if os.path.exists(_HOOKS_JSON):
        try:
            with open(_HOOKS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            hooks = data.get("hooks", []) if isinstance(data, dict) else []
            return hooks if isinstance(hooks, list) else []
        except Exception:
            return []
    return []


def normalize_hook_command(command: Any) -> list[str]:
    """Return the argv ``run_hooks`` will execute, or raise ``ValueError``.

    ``_valid_record`` only runs a list[str] argv (shell=False); a bare string
    needs two opt-ins. So the single place a command enters storage parses it
    into argv instead of keeping a string the runner would always skip.
    """
    if isinstance(command, str):
        posix = os.name == "posix"
        try:
            argv = shlex.split(command, posix=posix)
        except ValueError as exc:
            raise ValueError(f"Could not parse command: {exc}") from None
        if not posix:
            # posix=False keeps backslashes intact (right for Windows paths) but
            # also keeps the quote characters inside each token. Strip a matched
            # outer pair so a quoted path is the real path.
            argv = [
                tok[1:-1] if len(tok) > 1 and tok[0] == tok[-1] and tok[0] in "\"'" else tok
                for tok in argv
            ]
    elif isinstance(command, list):
        argv = list(command)
    else:
        raise ValueError("Command must be a string or a list of strings")
    if not argv or any(not isinstance(x, str) or not x or "\x00" in x for x in argv):
        raise ValueError("Command must be a non-empty argv with no NUL bytes")
    return argv


def _migrate_string_commands(hooks: list[dict]) -> list[dict]:
    """Convert stored string commands to argv so saved hooks stay runnable.

    ``legacy_shell`` records are left alone: they are the explicit opt-in to
    shell execution and must keep their string form.
    """
    out: list[dict] = []
    for hook in hooks:
        if (isinstance(hook, dict) and isinstance(hook.get("command"), str)
                and hook.get("legacy_shell") is not True):
            try:
                hook = {**hook, "command": normalize_hook_command(hook["command"])}
            except ValueError:
                pass
        out.append(hook)
    return out


def save_hooks(hooks: list[dict]) -> None:
    hooks = _migrate_string_commands(hooks)
    os.makedirs(os.path.dirname(_HOOKS_JSON), exist_ok=True)
    try:
        temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(_HOOKS_JSON))
        with os.fdopen(temp_fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"hooks": hooks}, f)
        os.replace(temp_path, _HOOKS_JSON)
        if not restrict_to_owner(_HOOKS_JSON):
            _diag("secure_files.restrict_failed", msg=_HOOKS_JSON)
        remember_hooks(_HOOKS_JSON, hooks)
    except Exception:
        logger.error("Failed to save hooks")


def _valid_record(hook: Any, event: str) -> bool:
    if not isinstance(hook, dict) or hook.get("enabled") is not True:
        return False
    if not isinstance(hook.get("id"), str) or not hook["id"] or len(hook["id"]) > 128:
        return False
    if hook.get("event") != event or event not in ALLOWED_EVENTS:
        return False
    command = hook.get("command")
    if isinstance(command, list):
        return bool(command) and all(isinstance(x, str) and x and "\x00" not in x for x in command)
    return isinstance(command, str) and bool(command) and hook.get("legacy_shell") is True and os.environ.get("HARNESS_ALLOW_LEGACY_SHELL_HOOKS") == "1"


def _safe_context(context: Any) -> str:
    value = redact_api_secrets(context if isinstance(context, (dict, list)) else {})
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        encoded = "{}"
    return encoded[:_MAX_CONTEXT]


def _environment(event: str, context_json: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.casefold().split(".")[-1] not in _SECRET_ENV_NAMES}
    env["PMHARNESS_EVENT"] = event
    env["PMHARNESS_CONTEXT_JSON"] = context_json
    return env


def _legacy_shell_argv(command: str) -> list[str]:
    if os.name == "posix":
        return ["/bin/sh", "-c", command]
    return [os.environ.get("COMSPEC") or "cmd.exe", "/c", command]


def _hook_argv(command: Any) -> list[str]:
    if isinstance(command, list):
        return command
    return _legacy_shell_argv(command)


def run_hooks(event: str, context: dict) -> list[dict[str, str]]:
    """Run persisted hooks safely; return one non-sensitive outcome per record."""
    if event not in ALLOWED_EVENTS:
        return []
    context_json = _safe_context(context)
    env = _environment(event, context_json)
    outcomes = []
    for hook in get_hooks():
        if not isinstance(hook, dict) or hook.get("event") != event:
            continue
        hook_id = hook.get("id") if isinstance(hook.get("id"), str) else "invalid"
        if not _valid_record(hook, event):
            outcomes.append({"id": hook_id, "status": "skipped"})
            logger.warning("Hook skipped: invalid or disabled record")
            continue
        trust = evaluate_hook_trust(hook, hooks_json=_HOOKS_JSON)
        if trust != TRUSTED:
            outcomes.append({"id": hook_id, "status": trust})
            logger.warning("Hook skipped: %s", trust)
            continue
        try:
            completed = subprocess.run(
                _hook_argv(hook["command"]),
                shell=False,
                env=env,
                timeout=_HOOK_TIMEOUT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            status = "executed" if completed.returncode == 0 else "error"
        except subprocess.TimeoutExpired:
            status = "timeout"
        except Exception:
            status = "error"
        outcomes.append({"id": hook_id, "status": status})
    return outcomes
