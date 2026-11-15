"""One model of account state for Settings > Accounts and for error text.

Each provider has one row with one state. The state comes from the same
store the pilot reads: the credential pool first (the drivers call
``resolve_entry_for_env``), then the key the provider gets from env or
keys.json, then the CLI login for Claude Code and Cursor CLI.

A state changes only after a real event: a sign-in, a saved key, a request
that failed, or a probe (``probe_account``) that sends one request with the
credential the pilot uses.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import credential_pool as cp
from .diag import note as _diag

# signed_in: a plan sign-in that works. api_key: a key that works.
# expired: a sign-in that only a new sign-in can fix. rejected: the provider
# refused the key. limited: rate limit or quota, waits for a cooldown.
# off: turned off in Settings. not_set: nothing stored.
# not_installed: the CLI that holds the login is missing.
STATES = (
    "signed_in", "api_key", "expired", "rejected", "limited",
    "off", "not_set", "not_installed",
)

# How a provider can connect. The first one is the primary action when
# the provider has no working credential.
_SIGN_IN = "sign_in"
_API_KEY = "api_key"
_CLI_LOGIN = "cli_login"

_METHODS: Dict[str, Tuple[str, ...]] = {
    "anthropic": (_SIGN_IN, _API_KEY),
    "openai-codex": (_SIGN_IN,),
    "claude-code": (_CLI_LOGIN,),
    "cursor-cli": (_CLI_LOGIN,),
    "nous": (_SIGN_IN,),
    "xai": (_API_KEY, _SIGN_IN),
}

# Names a person knows. Provider ids stay in the payload.
_NAMES: Dict[str, str] = {
    "anthropic": "Claude (Anthropic)",
    "openai-codex": "ChatGPT Codex",
    "claude-code": "Claude Code CLI",
    "cursor-cli": "Cursor CLI",
    "nous": "Nous Portal",
    "xai": "xAI Grok",
}

# Pools that hold credentials for a row. xAI sign-in lives in its own pool.
_POOLS: Dict[str, Tuple[str, ...]] = {"xai": ("xai", "xai-oauth")}

# Rows not shown: local servers and Bedrock have their own settings pages.
_HIDDEN = frozenset({"local", "bedrock"})

# Labels that name where a key came from, not whose account it is.
_SOURCE_LABELS = ("env:", "keys.json", "seed")

_checks: Dict[str, Dict[str, Any]] = {}
_checks_lock = threading.Lock()


@dataclass
class AccountRow:
    provider: str
    name: str
    state: str
    # Who is signed in, or the masked key. Empty when not known.
    account: str
    # One short sentence about the state, for the row.
    detail: str
    # The one primary action: sign_in, add_key, cli_login, turn_on, install,
    # or "" when the row works.
    action: str
    methods: List[str]
    # Latest probe: {ok, status, message, at}. None when never probed.
    check: Optional[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _masked(token: str) -> str:
    token = (token or "").strip()
    return ("...." + token[-4:]) if len(token) > 8 else "...."


def _account_label(entry: cp.PooledCredential) -> str:
    account = str((entry.extra or {}).get("account") or "").strip()
    if account:
        return account
    label = (entry.label or "").strip()
    if (not label or label.startswith(_SOURCE_LABELS)
            or label in (entry.provider, f"{entry.provider}-settings")):
        return _masked(entry.runtime_token)
    return label


def _pool_entries(provider: str) -> List[cp.PooledCredential]:
    out: List[cp.PooledCredential] = []
    for name in _POOLS.get(provider, (provider,)):
        if name in cp.known_pool_providers():
            out.extend(sorted(cp.load_pool(name).entries(), key=lambda e: e.priority))
    return out


def _is_sign_in(entry: cp.PooledCredential) -> bool:
    return entry.auth_type == cp.AUTH_TYPE_OAUTH


def _row_from_pool(provider: str, entries: List[cp.PooledCredential]) -> Optional[Tuple[str, str, str]]:
    """(state, account, detail) from pool entries, or None without entries."""
    if not entries:
        return None
    now = time.time()
    pools = {name: cp.load_pool(name) for name in {e.provider for e in entries}}
    for entry in entries:
        if entry.runtime_token and pools[entry.provider]._healthy(entry, now):
            if _is_sign_in(entry):
                detail = ("Signed in. The sign-in renews on its own."
                          if entry.refresh_token else
                          "Sign-in token without renewal. Sign in again when it expires.")
                return "signed_in", _account_label(entry), detail
            return "api_key", _account_label(entry), "API key set."
    entry = entries[0]
    account = _account_label(entry)
    if entry.last_status == cp.STATUS_DEAD or entry.last_error_code in (401, 403):
        if _is_sign_in(entry):
            return "expired", account, "Sign-in expired. Sign in again."
        return "rejected", account, "The provider rejected this key. Add a new key."
    return "limited", account, "Rate limit or quota reached. It clears after a cooldown."


def _cli_row(provider: str) -> Tuple[str, str, str]:
    try:
        if provider == "claude-code":
            from .claude_cli_auth import get_status
        else:
            from .cursor_cli_auth import get_status
        status = get_status()
    except Exception as exc:
        _diag("accounts.cli_status", exc)
        return "not_set", "", "Login status is not available."
    if not status.get("installed") and not status.get("authenticated"):
        return "not_installed", "", str(status.get("error") or "CLI not found.")
    if status.get("authenticated"):
        return "signed_in", str(status.get("label") or ""), "Signed in with the CLI login."
    return "not_set", "", "Not signed in."


def _action_for(state: str, methods: Tuple[str, ...]) -> str:
    if state in ("signed_in", "api_key", "limited"):
        return ""
    if state == "off":
        return "turn_on"
    if state == "not_installed":
        return "install"
    if state == "expired":
        return _CLI_LOGIN if _CLI_LOGIN in methods else _SIGN_IN
    if state == "rejected":
        return "add_key"
    first = methods[0]
    return "add_key" if first == _API_KEY else first


def account_row(provider) -> AccountRow:
    """The one row for a provider (a ``harness.providers.Provider``)."""
    name = provider.name
    methods = _METHODS.get(name, (_API_KEY,))
    display = _NAMES.get(name) or getattr(provider, "display_name", "") or name
    state = account = detail = ""
    if provider._is_disconnected():
        state, detail = "off", "Turned off. The pilot does not use it."
    elif _CLI_LOGIN in methods:
        state, account, detail = _cli_row(name)
    else:
        from_pool = _row_from_pool(name, _pool_entries(name))
        if from_pool is not None:
            state, account, detail = from_pool
        elif provider.key():
            state, account, detail = "api_key", _masked(provider.key() or ""), "API key set."
        else:
            state, detail = "not_set", "Not connected."
    with _checks_lock:
        check = dict(_checks[name]) if name in _checks else None
    return AccountRow(
        provider=name, name=display, state=state, account=account, detail=detail,
        action=_action_for(state, methods), methods=list(methods), check=check,
    )


def _providers():
    from .providers import PROVIDERS
    return [p for p in PROVIDERS if p.name not in _HIDDEN]


def account_rows() -> List[Dict[str, Any]]:
    rows = []
    for provider in _providers():
        try:
            rows.append(account_row(provider).to_dict())
        except Exception as exc:
            _diag("accounts.row", exc, msg=provider.name)
    return rows


def find_provider(name: str):
    for provider in _providers():
        if provider.name == name:
            return provider
    return None


def auth_failure_text(provider_name: str) -> str:
    """Pilot error text for a rejected credential, named after the row."""
    provider = find_provider(provider_name)
    if provider is None:
        return ("pilot: the provider rejected the credential (authentication "
                "failed). Fix it in Settings > Accounts.")
    row = account_row(provider)
    if row.state == "expired":
        return (f"pilot: {row.name} sign-in expired. Sign in again in "
                "Settings > Accounts.")
    if row.state == "signed_in" and _SIGN_IN in row.methods:
        return (f"pilot: {row.name} rejected the sign-in. Sign in again in "
                "Settings > Accounts.")
    if _CLI_LOGIN in row.methods:
        return (f"pilot: the {row.name} login was rejected. Sign in again in "
                "Settings > Accounts.")
    return (f"pilot: {row.name} rejected the API key (authentication failed). "
            "Add a new key in Settings > Accounts.")


def sign_out_account(provider_name: str) -> Dict[str, Any]:
    """Remove every sign-in entry of a row and a sign-in token copy in keys.json.

    API key entries stay: "Remove key" is the key action.
    """
    provider = find_provider(provider_name)
    if provider is None:
        return {"error": f"unknown provider: {provider_name}"}
    for entry in _pool_entries(provider.name):
        if _is_sign_in(entry):
            cp.remove_entry(entry.provider, entry.id)
    from .keys import forget_stored_sign_in_token
    forget_stored_sign_in_token(provider.name)
    with _checks_lock:
        _checks.pop(provider.name, None)
    return {"row": account_row(provider).to_dict()}


# -- probe -----------------------------------------------------------------

_PROBE_TIMEOUT = 15


def _http_status(url: str, headers: Dict[str, str]) -> Tuple[Optional[int], str]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:
            return int(resp.status), ""
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
            message = str((json.loads(body).get("error") or {}).get("message") or "")
        except Exception:
            message = ""
        return int(exc.code), message[:200]
    except Exception as exc:
        return None, f"{type(exc).__name__}: network error"


def _probe_request(provider, token: str) -> Tuple[Optional[int], str]:
    """One authenticated read with ``token``. (HTTP status, provider message)."""
    name = provider.name
    if name == "anthropic":
        from .oauth_anthropic import is_anthropic_oauth_token
        headers = {"anthropic-version": "2023-06-01"}
        if is_anthropic_oauth_token(token):
            headers["Authorization"] = f"Bearer {token}"
            headers["anthropic-beta"] = "oauth-2025-04-20"
        else:
            headers["x-api-key"] = token
        return _http_status("https://api.anthropic.com/v1/models?limit=1", headers)
    if name == "openrouter":
        # /models is public on OpenRouter; /key needs a valid key.
        return _http_status("https://openrouter.ai/api/v1/key",
                            {"Authorization": f"Bearer {token}"})
    if name == "openai-codex":
        from .model_fetch import _fetch_codex_oauth_models
        return (200, "") if _fetch_codex_oauth_models(token) else (None, "no models returned")
    if name == "gemini":
        return _http_status("https://generativelanguage.googleapis.com/v1beta/models",
                            {"x-goog-api-key": token})
    base = (getattr(provider, "base_url", "") or "").rstrip("/")
    if not base or getattr(provider, "api_mode", "") == "anthropic_messages":
        return None, "no probe for this provider"
    if name in ("opencode-go", "opencode-zen"):
        from .opencode_go import driver_base_url
        base = driver_base_url(base).rstrip("/")
    return _http_status(base + "/models",
                        {"Authorization": f"Bearer {token}", "User-Agent": "Marionette"})


def _record_check(name: str, ok: bool, status: Optional[int], message: str) -> None:
    with _checks_lock:
        _checks[name] = {"ok": ok, "status": status, "message": message, "at": time.time()}


def probe_account(provider_name: str) -> Dict[str, Any]:
    """Send one request with the credential the pilot uses, then report the row.

    A 401 or 403 goes through ``report_failure``: a sign-in renews or turns
    expired, a key turns rejected. A success clears an old failure mark.
    """
    provider = find_provider(provider_name)
    if provider is None:
        return {"error": f"unknown provider: {provider_name}"}
    name = provider.name
    methods = _METHODS.get(name, (_API_KEY,))
    if provider._is_disconnected():
        return {"row": account_row(provider).to_dict()}
    if _CLI_LOGIN in methods:
        try:
            if name == "claude-code":
                from .claude_cli_auth import get_status
            else:
                from .cursor_cli_auth import get_status
            status = get_status(refresh=True)
            _record_check(name, bool(status.get("authenticated")), None,
                          "" if status.get("authenticated") else str(status.get("error") or ""))
        except Exception as exc:
            _record_check(name, False, None, f"{type(exc).__name__}")
        return {"row": account_row(provider).to_dict()}

    entries = _pool_entries(name)
    entry = entries[0] if entries else None
    for candidate in entries:
        pool = cp.load_pool(candidate.provider)
        if candidate.runtime_token and pool._healthy(candidate, time.time()):
            entry = candidate
            break
    token = entry.runtime_token if entry is not None else (provider.key() or "")
    if not token:
        _record_check(name, False, None, "No credential to check.")
        return {"row": account_row(provider).to_dict()}
    if entry is not None and entry.auth_type == cp.AUTH_TYPE_OAUTH and cp._expires_soon(entry):
        cp.refresh_oauth_entry(entry)
        token = entry.runtime_token

    status, message = _probe_request(provider, token)
    if status in (401, 403) and entry is not None:
        renewed = cp.report_failure(entry.provider, entry.id, status_code=status,
                                    message=message or f"HTTP {status}")
        if renewed and renewed == entry.runtime_token:
            status, message = _probe_request(provider, renewed)
    if status is not None and 200 <= status < 300:
        if entry is not None and entry.last_status != cp.STATUS_OK:
            with cp._lock:
                entry.last_status = cp.STATUS_OK
                entry.last_status_at = None
                entry.last_error_code = None
                entry.last_error_message = None
                cp._persist_all()
        _record_check(name, True, status, "")
    elif status is None and message == "no probe for this provider":
        with _checks_lock:
            _checks.pop(name, None)
    else:
        if status in (401, 403) and entry is None:
            message = message or "The provider rejected this key."
        _record_check(name, False, status, message or (f"HTTP {status}" if status else ""))
    row = account_row(provider).to_dict()
    if status in (401, 403) and entry is None and row["state"] == "api_key":
        row.update(state="rejected", detail="The provider rejected this key. Add a new key.",
                   action="add_key")
    return {"row": row}


def clear_checks_for_tests() -> None:
    with _checks_lock:
        _checks.clear()
