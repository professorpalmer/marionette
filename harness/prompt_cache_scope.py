from __future__ import annotations

"""Builder-declared prompt-cache watermarks and rotation-stable cache scope.

Stdlib-only. ``prompt_cache_scope`` hashes a conversation identity /
compression-lineage ROOT — never the physical ``session_id`` string.
"""

import hashlib
from typing import Dict, Optional

# name -> prefix text. Process-local; builders register at startup.
_STABLE_PREFIXES: Dict[str, str] = {}


def register_stable_prefix(name: str, text: str) -> None:
    """Register a builder-declared stable prefix watermark.

    Re-registering the same name replaces the previous text. Empty names
    are ignored so a blank register cannot shadow real watermarks.
    """
    key = (name or "").strip()
    if not key:
        return
    _STABLE_PREFIXES[key] = text if text is not None else ""


def find_stable_prefix(text: str) -> Optional[str]:
    """Return the registered prefix name if ``text`` starts with that prefix.

    Longest matching prefix wins when more than one is registered.
    """
    blob = text if isinstance(text, str) else ""
    if not blob or not _STABLE_PREFIXES:
        return None
    best_name = None
    best_len = -1
    for name, prefix in _STABLE_PREFIXES.items():
        if not prefix:
            continue
        if blob.startswith(prefix) and len(prefix) > best_len:
            best_name = name
            best_len = len(prefix)
    return best_name


def prompt_cache_scope(conversation_key: str) -> str:
    """Return sha256 hex of a rotation-stable conversation identity.

    ``conversation_key`` is a hashed conversation identity or compression-
    lineage ROOT. The digest never embeds a physical session id: callers
    must not pass ``harness_session_id`` / ``session_id`` as the key.
    """
    raw = conversation_key if isinstance(conversation_key, str) else ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def clear_stable_prefixes() -> None:
    """Drop all registered prefixes. Tests only."""
    _STABLE_PREFIXES.clear()
