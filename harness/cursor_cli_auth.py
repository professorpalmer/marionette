from __future__ import annotations

"""Cursor Agent CLI auth helpers (agent login / status / logout / models).

Distinct from CURSOR_API_KEY credential pools and from platform adapter
``which('cursor')``. Auth lives in the CLI's own session store; Marionette
never holds or rotates a bearer for this path.

``agent status`` is ~1s+ on Windows; Settings / provider listing can call
this path many times per open. Cache + lock so concurrent callers share one
spawn instead of stacking slow CLI processes.
"""

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from pmharness.drivers.cursor_cli import (
    DEFAULT_CURSOR_CLI_MODELS,
    INSTALL_HINT,
    resolve_agent_binary,
)

_STATUS_TIMEOUT = 8
_STATUS_CACHE_TTL = 30.0
_status_lock = threading.Lock()
_status_cache: Optional[Dict[str, Any]] = None
_status_cache_at = 0.0


def invalidate_status_cache() -> None:
    global _status_cache, _status_cache_at
    with _status_lock:
        _status_cache = None
        _status_cache_at = 0.0


def _run_agent(args: list[str], *, timeout: int = _STATUS_TIMEOUT) -> subprocess.CompletedProcess:
    binary = resolve_agent_binary()
    if not binary:
        raise FileNotFoundError(INSTALL_HINT)
    return subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _parse_status_json(stdout: str) -> Dict[str, Any]:
    text = (stdout or "").strip()
    if not text:
        return {}
    # Prefer a trailing JSON object (some CLIs print banners then JSON).
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return {}


def _authenticated_from_status(data: dict, stdout: str) -> tuple[bool, str]:
    """Return (authenticated, label) from structured or text status output."""
    if data:
        # Common shapes: {loggedIn}, {authenticated}, {isAuthenticated} (Agent CLI).
        truthy = (
            data.get("loggedIn") is True
            or data.get("authenticated") is True
            or data.get("isAuthenticated") is True
        )
        falsy = (
            data.get("loggedIn") is False
            or data.get("authenticated") is False
            or data.get("isAuthenticated") is False
        )
        if truthy:
            account = data.get("account")
            account_email = account.get("email") if isinstance(account, dict) else None
            label = (
                data.get("email")
                or data.get("user")
                or account_email
                or data.get("label")
                or "cursor-account"
            )
            return True, str(label)
        if falsy:
            return False, ""
        user = data.get("user") or data.get("email")
        if user:
            return True, str(user)
        api_src = (data.get("apiKeySource") or data.get("api_key_source") or "")
        if str(api_src).lower() in ("login", "cursor", "account"):
            return True, str(data.get("email") or "cursor-account")
        # Explicit unauthenticated status string from Agent CLI.
        if str(data.get("status") or "").lower() in ("unauthenticated", "logged_out", "signed_out"):
            return False, ""

    lower = (stdout or "").lower()
    if "not logged" in lower or "not authenticated" in lower:
        return False, ""
    if "logged in" in lower:
        # Try to pull an email-ish token.
        for token in (stdout or "").replace("✓", " ").split():
            if "@" in token:
                return True, token.strip(" .,;")
        return True, "cursor-account"
    return False, ""


def _get_status_uncached() -> Dict[str, Any]:
    """Report agent binary presence + login state (no bearer tokens)."""
    binary = resolve_agent_binary()
    if not binary:
        return {
            "ok": False,
            "installed": False,
            "authenticated": False,
            "binary": None,
            "label": "",
            "error": f"Cursor Agent CLI not found. {INSTALL_HINT}",
            "install_hint": INSTALL_HINT,
            "auth_kind": "cursor_account",
        }

    stdout = ""
    data: Dict[str, Any] = {}
    try:
        proc = _run_agent(["status", "--format", "json"])
        stdout = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        data = _parse_status_json(proc.stdout or "")
        if not data:
            # Fallback: plain status / whoami text.
            proc2 = _run_agent(["status"])
            stdout = (proc2.stdout or "") + ("\n" + (proc2.stderr or ""))
            data = _parse_status_json(proc2.stdout or "")
            if not data:
                try:
                    proc3 = _run_agent(["whoami", "--format", "json"])
                    data = _parse_status_json(proc3.stdout or "")
                    stdout = (proc3.stdout or "") or stdout
                except Exception:
                    pass
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "installed": True,
            "authenticated": False,
            "binary": binary,
            "label": "",
            "error": "agent status timed out",
            "auth_kind": "cursor_account",
        }
    except Exception as e:
        return {
            "ok": False,
            "installed": True,
            "authenticated": False,
            "binary": binary,
            "label": "",
            "error": str(e),
            "auth_kind": "cursor_account",
        }

    authed, label = _authenticated_from_status(data, stdout)
    return {
        "ok": True,
        "installed": True,
        "authenticated": authed,
        "binary": binary,
        "label": label if authed else "",
        "error": None if authed else "Not signed in. Click Sign in to run `agent login`.",
        "auth_kind": "cursor_account",
        "raw": data or None,
    }


def get_status(*, refresh: bool = False) -> Dict[str, Any]:
    """Cached status. Concurrent callers share one ``agent`` spawn."""
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
    """Sentinel for Provider.key() — never a real secret; presence means ready."""
    # Env override first — avoids spawning agent for tests / power users.
    if (os.environ.get("CURSOR_CLI_LOGIN") or "").strip() in ("1", "true", "yes"):
        return "1"
    if is_authenticated():
        return "1"
    return None


def _resolve_workspace(workspace: Optional[str] = None) -> Optional[str]:
    raw = (workspace or os.environ.get("HARNESS_REPO") or "").strip()
    if not raw:
        return None
    try:
        return os.path.abspath(raw)
    except OSError:
        return raw


def ensure_workspace_trusted(workspace: Optional[str] = None) -> Dict[str, Any]:
    """Headless-trust the open project for Cursor Agent CLI.

    Marionette already chose this directory; without ``--trust`` the Agent CLI
    blocks non-interactive ``--print`` runs with "Workspace Trust Required".
    Call after Sign-in succeeds (and the pilot always passes ``--trust`` too).
    """
    binary = resolve_agent_binary()
    if not binary:
        return {
            "ok": False,
            "trusted": False,
            "workspace": None,
            "error": f"Cursor Agent CLI not found. {INSTALL_HINT}",
        }
    path = _resolve_workspace(workspace)
    if not path or not os.path.isdir(path):
        return {
            "ok": False,
            "trusted": False,
            "workspace": path,
            "error": "No open project directory to trust (set workspace / HARNESS_REPO).",
        }
    # Tiny no-op print: records trust for this workspace when the CLI persists it.
    # Driver also passes --trust on every turn so chat works even if this fails.
    args = [
        "--print",
        "--trust",
        "--mode", "ask",
        "--workspace", path,
        "ok",
    ]
    try:
        proc = _run_agent(args, timeout=45)
    except Exception as e:
        return {
            "ok": False,
            "trusted": False,
            "workspace": path,
            "error": str(e),
        }
    err_text = ((proc.stderr or "") + "\n" + (proc.stdout or "")).lower()
    blocked = "workspace trust" in err_text and "required" in err_text
    trusted = proc.returncode == 0 and not blocked
    if trusted:
        prewarm_agent()
    return {
        "ok": trusted,
        "trusted": trusted,
        "workspace": path,
        "returncode": proc.returncode,
        "error": None if trusted else (
            (proc.stderr or proc.stdout or "trust probe failed").strip()[:400]
        ),
    }


def prewarm_agent() -> None:
    """Best-effort background touch of the Agent CLI (binary + node graph).

    Does not cut the ~10s per-turn model cold start, but avoids first-use
    PATH/resolve stalls after Sign in. Fire-and-forget.
    """
    binary = resolve_agent_binary()
    if not binary:
        return

    def _run() -> None:
        try:
            _run_agent(["about"], timeout=30)
        except Exception:
            pass

    threading.Thread(target=_run, name="cursor-cli-prewarm", daemon=True).start()


def start_login(workspace: Optional[str] = None) -> Dict[str, Any]:
    """Launch or guide `agent login`. Does not store/rotate pool bearers."""
    invalidate_status_cache()
    binary = resolve_agent_binary()
    if not binary:
        return {
            "ok": False,
            "launched": False,
            "command": "agent login",
            "error": f"Cursor Agent CLI not found. {INSTALL_HINT}",
            "install_hint": INSTALL_HINT,
            "hint": INSTALL_HINT,
        }

    cmd = [binary, "login"]
    launched = False
    launch_error = None
    try:
        kwargs: Dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            # Detach into a new console so the browser/device flow can proceed.
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
        launched = True
    except Exception as e:
        launch_error = str(e)

    ws = _resolve_workspace(workspace)
    return {
        "ok": True,
        "launched": launched,
        "command": " ".join(cmd),
        "provider": "cursor-cli",
        "auth_kind": "cursor_account",
        "workspace": ws,
        "hint": (
            "Complete Sign-in in the Cursor login window / terminal, then wait — "
            "Marionette polls `agent status`, then trusts the open project for "
            "headless Agent CLI (Cursor account, not an API key pool)."
        ),
        "error": launch_error,
        "poll_interval": 3,
        "expires_in": 900,
    }


def logout() -> Dict[str, Any]:
    invalidate_status_cache()
    binary = resolve_agent_binary()
    if not binary:
        return {
            "ok": False,
            "error": f"Cursor Agent CLI not found. {INSTALL_HINT}",
        }
    try:
        proc = _run_agent(["logout"], timeout=30)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    invalidate_status_cache()
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[:500],
        "stderr": (proc.stderr or "")[:500],
    }


# Cursor's plan-level router — Marionette already selects the pilot explicitly.
_CURSOR_CLI_EXCLUDED_MODELS = frozenset({"auto"})


def _parse_agent_models_text(out: str) -> List[str]:
    """Parse ``agent models`` plain text: ``id - Description`` (or bare id)."""
    ids: List[str] = []
    seen = set()
    for raw in (out or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("available models") or lower.startswith("tip:"):
            continue
        if set(line) <= set("-="):
            continue
        # Primary form from current Agent CLI: "composer-2.5 - Composer 2.5"
        if " - " in line:
            token = line.split(" - ", 1)[0].strip()
        elif " — " in line:
            token = line.split(" — ", 1)[0].strip()
        else:
            token = line.split()[0].strip(",;")
        if not token or token.startswith("-"):
            continue
        # Skip section headers like "Model" / "Models"
        if token.lower() in ("model", "models"):
            continue
        if token.lower() in _CURSOR_CLI_EXCLUDED_MODELS:
            continue
        if token not in seen:
            seen.add(token)
            ids.append(token)
    return ids


def _filter_cursor_cli_models(ids: List[str]) -> List[str]:
    """Drop Cursor router / non-pilot ids from a live or curated list."""
    out: List[str] = []
    seen = set()
    for mid in ids:
        if not mid or mid.lower() in _CURSOR_CLI_EXCLUDED_MODELS:
            continue
        if mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


def list_models(*, live: bool = False) -> List[str]:
    """Model ids for the Cursor CLI pilot.

    Hot paths that must stay fast use curated defaults. Models catalog /
    refresh pass ``live=True`` to run ``agent models`` (multi-second).
    """
    curated = _filter_cursor_cli_models(list(DEFAULT_CURSOR_CLI_MODELS))
    if not live:
        return curated

    binary = resolve_agent_binary()
    if not binary:
        return curated

    # Prefer plain `agent models` — current CLI rejects `--format json`.
    for args in (
        ["models"],
        ["--list-models"],
        ["models", "--format", "json"],
    ):
        try:
            proc = _run_agent(args, timeout=45)
        except Exception:
            continue
        out = (proc.stdout or "").strip()
        if not out:
            continue
        # JSON array or {models: [...]} (if a future CLI supports it)
        try:
            data = json.loads(out)
            if isinstance(data, list):
                ids = [str(x.get("id") or x.get("name") or x) for x in data if x]
                ids = _filter_cursor_cli_models(ids)
                if ids:
                    return ids
            if isinstance(data, dict):
                models = data.get("models") or data.get("data") or []
                ids = []
                for m in models:
                    if isinstance(m, dict):
                        mid = m.get("id") or m.get("name")
                        if mid:
                            ids.append(str(mid))
                    elif isinstance(m, str):
                        ids.append(m)
                ids = _filter_cursor_cli_models(ids)
                if ids:
                    return ids
        except json.JSONDecodeError:
            pass
        ids = _parse_agent_models_text(out)
        if ids:
            return ids

    return curated
