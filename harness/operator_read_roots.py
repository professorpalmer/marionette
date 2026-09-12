from __future__ import annotations

"""Operator-owned extra read roots for pilot tools and worker swarms.

Writes stay workspace-confined. Reads may include workspace.json recents and
absolute / ~/ paths the user named on this turn, minus credential directories.
"""

import json
import os
import re
from typing import Iterable, List, Optional

_EXTRA_READ_ROOTS_ENV = "PUPPETMASTER_EXTRA_READ_ROOTS"

_ABS_PATH_RE = re.compile(
    r"(?:"
    r"~(?:/[^\s\"'`]+)+"
    r"|"
    r"/(?:Users|home|Volumes)/[^\s\"'`]+"
    r"|"
    r"[A-Za-z]:[\\/][^\s\"'`]+"
    r")"
)

_DENIED_DIR_NAMES = frozenset(
    {
        ".ssh",
        ".aws",
        ".gnupg",
        ".netrc",
        ".kube",
        ".config",
        "credentials",
    }
)


def _denied_root(path: str) -> bool:
    parts = os.path.normpath(os.path.abspath(path)).replace("\\", "/").split("/")
    lowered = [p.lower() for p in parts if p]
    if any(name in _DENIED_DIR_NAMES for name in lowered):
        return True
    if any(p.endswith(".env") or p == "id_rsa" or p.endswith("_rsa") for p in lowered):
        return True
    return False


def extract_named_paths(text: str) -> List[str]:
    """Absolute or ~/ paths mentioned in operator text."""
    found: List[str] = []
    seen = set()
    for match in _ABS_PATH_RE.finditer(text or ""):
        raw = match.group(0).rstrip(".,;:)")
        key = raw
        if key in seen:
            continue
        seen.add(key)
        found.append(raw)
    return found


def resolve_existing_root(
    raw: str, *, home: str = "", require_home: bool = True
) -> Optional[str]:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("~"):
        text = os.path.join(home or os.path.expanduser("~"), text[2:].lstrip("/\\"))
    try:
        path = os.path.realpath(os.path.abspath(os.path.expanduser(text)))
    except (OSError, ValueError):
        return None
    if not os.path.exists(path):
        return None
    root = path if os.path.isdir(path) else os.path.dirname(path)
    if not root or _denied_root(root):
        return None
    if require_home:
        home_root = os.path.realpath(home or os.path.expanduser("~"))
        try:
            common = os.path.commonpath([root, home_root])
        except ValueError:
            return None
        if os.path.realpath(common) != home_root:
            return None
    return root


def load_workspace_recents(state_home: str = "") -> List[str]:
    home = (state_home or "").strip() or os.path.expanduser("~/.pmharness")
    path = os.path.join(home, "workspace.json")
    if not os.path.isfile(path):
        path = os.path.join(home, "state", "workspace.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    recents = data.get("recents") or []
    out: List[str] = []
    for item in recents:
        root = resolve_existing_root(str(item or ""), require_home=False)
        if root:
            out.append(root)
    return out


def collect_operator_read_roots(
    user_message: str,
    *,
    recents: Optional[Iterable[str]] = None,
    home: str = "",
    state_home: str = "",
) -> List[str]:
    """Recents plus named existing operator paths, credential dirs excluded."""
    roots: List[str] = []
    seen = set()

    def _add(raw: str, *, require_home: bool) -> None:
        root = resolve_existing_root(raw, home=home, require_home=require_home)
        if not root or root in seen:
            return
        seen.add(root)
        roots.append(root)

    for item in recents if recents is not None else load_workspace_recents(state_home):
        _add(str(item or ""), require_home=False)
    for item in extract_named_paths(user_message or ""):
        _add(item, require_home=True)
    return roots


def export_extra_read_roots_env(roots: Iterable[str]) -> str:
    """Stamp PUPPETMASTER_EXTRA_READ_ROOTS for in-process and subprocess workers."""
    cleaned = [str(r).strip() for r in roots if str(r).strip()]
    joined = os.pathsep.join(cleaned)
    if joined:
        os.environ[_EXTRA_READ_ROOTS_ENV] = joined
    elif _EXTRA_READ_ROOTS_ENV in os.environ:
        os.environ.pop(_EXTRA_READ_ROOTS_ENV, None)
    return joined
