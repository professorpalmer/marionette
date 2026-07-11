"""Persistence for the portable-llm-wiki gated owner surface.

Stores WIKI_API_BASE + WIKI_OWNER_TOKEN in wiki.json (chmod 600) so the wiki
graph view reaches the tenant manifest/graph behind share-tier gating -- the
same surface the portable-llm-wiki MCP uses. Loaded into the process env on
startup so WikiClient (which reads those env vars) picks them up.

Preferred path: ~/.pmharness/state/wiki.json (or $HARNESS_STATE_DIR/wiki.json).
Legacy ~/.pmharness/wiki.json is still read and migrated.
"""
from __future__ import annotations

import json
import os
from urllib.parse import parse_qs, urlparse

from .secure_files import restrict_to_owner
from .diag import note as _diag

_LEGACY_WIKI_FILE = os.path.join(os.path.expanduser("~/.pmharness"), "wiki.json")
_DEFAULT_STATE_WIKI = os.path.join(
    os.path.expanduser("~/.pmharness"), "state", "wiki.json"
)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_HOSTED_FRONTEND_HOSTS = {"portablellm.wiki", "www.portablellm.wiki"}
_HOSTED_API_HOST = "api.portablellm.wiki"


def _path() -> str:
    state_dir = os.environ.get("HARNESS_STATE_DIR", "")
    if state_dir:
        p = os.path.join(state_dir, "wiki.json")
        # MIGRATION: earlier builds (and any run without HARNESS_STATE_DIR) wrote
        # wiki.json to ~/.pmharness/wiki.json. Once the stable state dir was
        # anchored to ~/.pmharness/state, _path() pointed into state/ and a
        # previously-saved config became invisible -- the wiki read as "not
        # connected" despite valid saved creds. If the state-dir copy is missing
        # but the legacy parent file exists, adopt the legacy file so the config
        # keeps working across the state-dir move.
        if not os.path.exists(p) and os.path.exists(_LEGACY_WIKI_FILE):
            return _LEGACY_WIKI_FILE
        return p
    # Prefer the stable state path for new writes; still read legacy if that is
    # the only copy on disk.
    if os.path.exists(_DEFAULT_STATE_WIKI):
        return _DEFAULT_STATE_WIKI
    if os.path.exists(_LEGACY_WIKI_FILE):
        return _LEGACY_WIKI_FILE
    return _DEFAULT_STATE_WIKI


def is_hosted_portablellm_base(base: str) -> bool:
    """True when api_base points at portablellm.wiki (frontend or API host)."""
    if not (base or "").strip():
        return False
    try:
        host = (urlparse(base.strip()).hostname or "").lower()
    except Exception:
        return False
    return host == _HOSTED_API_HOST or host in _HOSTED_FRONTEND_HOSTS


def is_remote_wiki_base(base: str) -> bool:
    """True when api_base is a non-loopback URL (hosted or self-hosted remote)."""
    if not (base or "").strip():
        return False
    try:
        host = (urlparse(base.strip()).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return host not in _LOCAL_HOSTS


def parse_wiki_connection_string(raw: str) -> dict:
    """Normalize a pasted wiki URL into {api_base, owner_token}.

    Accepts:
      - https://portablellm.wiki/<tenant>/llm?t=<token>  (personal LLM URL)
      - https://portablellm.wiki/<tenant>
      - https://api.portablellm.wiki/t/<tenant>           (already correct)
      - any other http(s) base (passed through, optional ?t= token)
    """
    text = (raw or "").strip()
    if not text:
        return {"api_base": "", "owner_token": None}
    try:
        u = urlparse(text)
    except Exception:
        return {"api_base": text.rstrip("/"), "owner_token": None}

    host = (u.hostname or "").lower()
    path = (u.path or "").strip("/")
    qs = parse_qs(u.query or "")
    token = None
    for key in ("t", "token"):
        vals = qs.get(key) or []
        if vals and vals[0]:
            token = vals[0].strip() or None
            break

    if host in _HOSTED_FRONTEND_HOSTS:
        parts = [p for p in path.split("/") if p]
        tenant = parts[0] if parts else ""
        if tenant and tenant not in {"llm", "t", "owner", "api"}:
            return {
                "api_base": f"https://{_HOSTED_API_HOST}/t/{tenant}",
                "owner_token": token,
            }

    if host == _HOSTED_API_HOST:
        # Keep /t/<tenant> (drop trailing junk like /llm).
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "t":
            return {
                "api_base": f"https://{_HOSTED_API_HOST}/t/{parts[1]}",
                "owner_token": token,
            }
        if path:
            return {
                "api_base": f"https://{_HOSTED_API_HOST}/{path}",
                "owner_token": token,
            }

    # Pass-through for local / self-hosted bases; strip query from api_base.
    if u.scheme and host:
        api_base = f"{u.scheme}://{u.netloc}"
        if path:
            api_base = f"{api_base}/{path}"
        return {"api_base": api_base.rstrip("/"), "owner_token": token}

    return {"api_base": text.rstrip("/"), "owner_token": token}


def normalize_wiki_config(cfg: dict) -> dict:
    """Return a copy with api_base (and optional token-from-URL) normalized."""
    out = dict(cfg or {})
    parsed = parse_wiki_connection_string(str(out.get("api_base") or ""))
    if parsed.get("api_base") is not None:
        out["api_base"] = parsed["api_base"]
    if parsed.get("owner_token") and not out.get("owner_token"):
        out["owner_token"] = parsed["owner_token"]
    return out


def get_wiki_config() -> dict:
    p = _path()
    if not os.path.exists(p):
        return {"api_base": "", "has_token": False}
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            d = json.load(f)
        d = normalize_wiki_config(d if isinstance(d, dict) else {})
        return {
            "api_base": d.get("api_base", ""),
            "has_token": bool(d.get("owner_token")),
        }
    except Exception:
        return {"api_base": "", "has_token": False}


def set_wiki_config(api_base: str = None, owner_token: str = None) -> dict:
    p = _path()
    cur = {}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                cur = json.load(f)
        except Exception:
            cur = {}
        if not isinstance(cur, dict):
            cur = {}

    url_token = None
    if api_base is not None:
        parsed = parse_wiki_connection_string(api_base)
        cur["api_base"] = parsed.get("api_base", "")
        url_token = parsed.get("owner_token")

    if owner_token is not None:
        # empty string clears the token
        if owner_token.strip():
            cur["owner_token"] = owner_token.strip()
        else:
            cur.pop("owner_token", None)
    elif url_token:
        # Personal LLM URL pasted into the base field alone is enough.
        cur["owner_token"] = url_token

    cur = normalize_wiki_config(cur)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(cur, f)
        os.replace(tmp, p)
        if not restrict_to_owner(p):
            _diag("secure_files.restrict_failed", msg=p)
    except Exception:
        pass
    _apply_to_env(cur)
    return {"api_base": cur.get("api_base", ""), "has_token": bool(cur.get("owner_token"))}


def _apply_to_env(cfg: dict):
    if cfg.get("api_base"):
        os.environ["WIKI_API_BASE"] = cfg["api_base"]
    if cfg.get("owner_token"):
        os.environ["WIKI_OWNER_TOKEN"] = cfg["owner_token"]


def _persist_cfg(cfg: dict) -> None:
    p = _path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(cfg, f)
        os.replace(tmp, p)
        restrict_to_owner(p)
    except Exception:
        pass


def load_wiki_config_on_startup():
    p = _path()
    if not os.path.exists(p):
        return
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            return
        normalized = normalize_wiki_config(cfg)
        _apply_to_env(normalized)
        # Rewrite when frontend tenant URLs or personal LLM URLs need migration
        # to api.portablellm.wiki/t/<tenant>.
        if normalized != cfg:
            _persist_cfg(normalized)
            # If we read the legacy path, also seed the preferred state path so
            # future boots do not depend on the migration branch.
            preferred = (
                os.path.join(os.environ["HARNESS_STATE_DIR"], "wiki.json")
                if os.environ.get("HARNESS_STATE_DIR")
                else _DEFAULT_STATE_WIKI
            )
            if os.path.normpath(p) != os.path.normpath(preferred):
                try:
                    os.makedirs(os.path.dirname(preferred), exist_ok=True)
                    tmp = preferred + ".tmp"
                    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                        json.dump(normalized, f)
                    os.replace(tmp, preferred)
                    restrict_to_owner(preferred)
                except Exception:
                    pass
    except Exception:
        pass
