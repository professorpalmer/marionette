from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from typing import Any

from .secure_files import restrict_to_owner
from .diag import note as _diag
from .api.redaction import redact_api_secrets

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


def save_hooks(hooks: list[dict]) -> None:
    os.makedirs(os.path.dirname(_HOOKS_JSON), exist_ok=True)
    try:
        temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(_HOOKS_JSON))
        with os.fdopen(temp_fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"hooks": hooks}, f)
        os.replace(temp_path, _HOOKS_JSON)
        if not restrict_to_owner(_HOOKS_JSON):
            _diag("secure_files.restrict_failed", msg=_HOOKS_JSON)
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
