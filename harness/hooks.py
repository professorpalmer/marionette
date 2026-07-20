from __future__ import annotations

import os
import json
import uuid
import tempfile
import subprocess
import logging

from .secure_files import restrict_to_owner
from .diag import note as _diag

logger = logging.getLogger("harness.hooks")

ALLOWED_EVENTS = ["sessionStart", "sessionEnd", "preRun", "postRun"]
_HOOKS_JSON = os.path.join(os.path.expanduser("~/.pmharness"), "hooks.json")

def get_hooks() -> list[dict]:
    if os.path.exists(_HOOKS_JSON):
        try:
            with open(_HOOKS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("hooks", [])
        except Exception:
            pass
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
    except Exception as e:
        logger.error(f"Failed to save hooks: {e}")

def run_hooks(event: str, context: dict) -> None:
    if event not in ALLOWED_EVENTS:
        logger.warning(f"Unknown hook event: {event}")
        return
        
    hooks = get_hooks()
    enabled_hooks = [h for h in hooks if h.get("event") == event and h.get("enabled", True)]
    
    if not enabled_hooks:
        return
        
    env = os.environ.copy()
    env["PMHARNESS_EVENT"] = event
    for k, v in context.items():
        env[f"PMHARNESS_{k.upper()}"] = str(v)
        
    context_json = json.dumps(context)
    
    for h in enabled_hooks:
        cmd = h.get("command")
        if not cmd:
            continue
            
        # shell=False with an explicit shell wrapper, per subprocess guidance.
        # /bin/sh on POSIX; cmd.exe on Windows (which has no /bin/sh).
        shell_wrapper = (
            ["/bin/sh", "-c", cmd] if os.name == "posix" else ["cmd", "/c", cmd]
        )
        try:
            # shell=False with an explicit wrapper argv; never shell=True + user cmd.
            subprocess.run(
                shell_wrapper,
                shell=False,
                input=context_json,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=15,
                env=env
            )
        except subprocess.TimeoutExpired:
            logger.error(f"Hook {h.get('id')} timed out")
        except Exception as e:
            logger.error(f"Hook {h.get('id')} failed: {e}")
