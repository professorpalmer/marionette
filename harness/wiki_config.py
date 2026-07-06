"""Persistence for the portable-llm-wiki gated owner surface.

Stores WIKI_API_BASE + WIKI_OWNER_TOKEN in ~/.pmharness/wiki.json (chmod 600) so the
wiki graph view reaches the tenant manifest/graph behind share-tier gating -- the same
surface the portable-llm-wiki MCP uses. Loaded into the process env on startup so
WikiClient (which reads those env vars) picks them up.
"""
import json
import os

from .secure_files import restrict_to_owner

_WIKI_FILE = os.path.join(os.path.expanduser("~/.pmharness"), "wiki.json")


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
        if not os.path.exists(p) and os.path.exists(_WIKI_FILE):
            return _WIKI_FILE
        return p
    return _WIKI_FILE


def get_wiki_config() -> dict:
    p = _path()
    if not os.path.exists(p):
        return {"api_base": "", "has_token": False}
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            d = json.load(f)
        return {"api_base": d.get("api_base", ""), "has_token": bool(d.get("owner_token"))}
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
    if api_base is not None:
        cur["api_base"] = api_base.strip()
    if owner_token is not None:
        # empty string clears the token
        if owner_token.strip():
            cur["owner_token"] = owner_token.strip()
        else:
            cur.pop("owner_token", None)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(cur, f)
        os.replace(tmp, p)
        restrict_to_owner(p)
    except Exception:
        pass
    _apply_to_env(cur)
    return {"api_base": cur.get("api_base", ""), "has_token": bool(cur.get("owner_token"))}


def _apply_to_env(cfg: dict):
    if cfg.get("api_base"):
        os.environ["WIKI_API_BASE"] = cfg["api_base"]
    if cfg.get("owner_token"):
        os.environ["WIKI_OWNER_TOKEN"] = cfg["owner_token"]


def load_wiki_config_on_startup():
    p = _path()
    if not os.path.exists(p):
        return
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            cfg = json.load(f)
        _apply_to_env(cfg)
    except Exception:
        pass
