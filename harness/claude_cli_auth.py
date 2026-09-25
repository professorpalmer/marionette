from __future__ import annotations

"""Claude Code CLI auth helpers (claude login / oauthAccount).

Distinct from the Anthropic Messages API key provider. Auth lives in the
Claude Code session store; Marionette never copies the OAuth token onto
ANTHROPIC_API_KEY.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from pmharness.drivers.claude_cli import (
    DEFAULT_CLAUDE_CLI_MODELS,
    INSTALL_HINT,
    claude_cli_login_is_sentinel,
    resolve_claude_binary,
    subscription_child_env,
)

_STATUS_TIMEOUT = 8
_STATUS_CACHE_TTL = 30.0
_status_lock = threading.Lock()
_status_cache: Optional[Dict[str, Any]] = None
_status_cache_at = 0.0


def reset_for_tests() -> None:
    invalidate_status_cache()


def invalidate_status_cache() -> None:
    global _status_cache, _status_cache_at
    with _status_lock:
        _status_cache = None
        _status_cache_at = 0.0


def _config_home() -> Path:
    raw = (os.environ.get("CLAUDE_CONFIG_HOME") or "").strip()
    if raw:
        return Path(raw)
    return Path.home()


def read_oauth_account(home: Optional[Path] = None) -> Optional[dict]:
    """Return oauthAccount when a real Claude Code login is present.

    File existence is not enough — ~/.claude.json survives logout. Require
    accountUuid or emailAddress, matching Puppetmaster platform_billing.
    """
    root = home if home is not None else _config_home()
    path = root / ".claude.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        data = None
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    if isinstance(oauth, dict) and (oauth.get("accountUuid") or oauth.get("emailAddress")):
        return oauth
    creds = root / ".claude" / ".credentials.json"
    try:
        if creds.is_file() and creds.stat().st_size > 2:
            return {"emailAddress": "claude-account", "source": "credentials"}
    except OSError:
        return None
    return None


def _run_claude(args: list[str], *, timeout: int = _STATUS_TIMEOUT) -> subprocess.CompletedProcess:
    binary = resolve_claude_binary()
    if not binary:
        raise FileNotFoundError(INSTALL_HINT)
    run_kwargs: dict = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
        "stdin": subprocess.DEVNULL,
        "env": subscription_child_env(),
    }
    if sys.platform == "win32":
        run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run([binary, *args], **run_kwargs)


def _oauth_label(oauth: dict) -> str:
    email = oauth.get("emailAddress") or oauth.get("email")
    if email:
        return str(email)
    seat = oauth.get("seatTier") or oauth.get("subscriptionType")
    if seat:
        return f"Claude {seat}"
    return "claude-account"


def _get_status_uncached() -> Dict[str, Any]:
    binary = resolve_claude_binary()
    oauth = read_oauth_account()
    if oauth is not None:
        return {
            "ok": True,
            "installed": bool(binary),
            "authenticated": True,
            "binary": binary,
            "label": _oauth_label(oauth),
            "error": None if binary else f"Signed in, but Claude Code CLI not found. {INSTALL_HINT}",
            "install_hint": INSTALL_HINT,
            "auth_kind": "claude_account",
            "billing": "plan",
        }
    if not binary:
        return {
            "ok": False,
            "installed": False,
            "authenticated": False,
            "binary": None,
            "label": "",
            "error": f"Claude Code CLI not found. {INSTALL_HINT}",
            "install_hint": INSTALL_HINT,
            "auth_kind": "claude_account",
        }
    return {
        "ok": True,
        "installed": True,
        "authenticated": False,
        "binary": binary,
        "label": "",
        "error": "Not signed in. Click Sign in to run `claude auth login`.",
        "install_hint": INSTALL_HINT,
        "auth_kind": "claude_account",
        "billing": "unknown",
    }


def get_status(*, refresh: bool = False) -> Dict[str, Any]:
    global _status_cache, _status_cache_at
    with _status_lock:
        now = time.monotonic()
        if (
            not refresh
            and _status_cache is not None
            and (now - _status_cache_at) < _STATUS_CACHE_TTL
        ):
            return dict(_status_cache)
        result = _get_status_uncached()
        _status_cache = result
        _status_cache_at = time.monotonic()
        return dict(result)


def is_authenticated() -> bool:
    try:
        return bool(get_status().get("authenticated"))
    except Exception:
        return False


def login_token_if_ready() -> Optional[str]:
    """Sentinel for Provider.key() — never a real secret."""
    if claude_cli_login_is_sentinel(os.environ.get("CLAUDE_CODE_LOGIN") or ""):
        return "1"
    if is_authenticated():
        return "1"
    return None


def start_login(workspace: Optional[str] = None) -> Dict[str, Any]:
    invalidate_status_cache()
    binary = resolve_claude_binary()
    if not binary:
        return {
            "ok": False,
            "launched": False,
            "command": "claude auth login",
            "error": f"Claude Code CLI not found. {INSTALL_HINT}",
            "install_hint": INSTALL_HINT,
            "hint": INSTALL_HINT,
        }
    cmd = [binary, "auth", "login"]
    launched = False
    launch_error = None
    try:
        kwargs: Dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
            "env": subscription_child_env(),
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
        launched = True
    except Exception as e:
        launch_error = str(e)
    return {
        "ok": True,
        "launched": launched,
        "command": " ".join(cmd),
        "provider": "claude-code",
        "auth_kind": "claude_account",
        "workspace": (workspace or os.environ.get("HARNESS_REPO") or "").strip() or None,
        "hint": (
            "Complete Sign-in in the browser window that opens, then wait — "
            "Marionette polls ~/.claude.json oauthAccount. "
            "The spawned claude process drops ANTHROPIC_API_KEY so Max/Pro is used."
        ),
        "error": launch_error,
        "poll_interval": 3,
        "expires_in": 900,
    }


def logout() -> Dict[str, Any]:
    invalidate_status_cache()
    binary = resolve_claude_binary()
    if not binary:
        return {
            "ok": False,
            "error": f"Claude Code CLI not found. {INSTALL_HINT}",
        }
    try:
        proc = _run_claude(["auth", "logout"], timeout=30)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    invalidate_status_cache()
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[:500],
        "stderr": (proc.stderr or "")[:500],
    }


def list_models(*, live: bool = False) -> List[str]:
    _ = live
    return list(DEFAULT_CLAUDE_CLI_MODELS)
