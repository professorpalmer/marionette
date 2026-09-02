"""Plugin capability ids and consent hashes (defaults OFF).

Known ids are a tiny allowlist. Unknown ids fail closed — they are never
implicitly granted and ``parse_requested_capabilities`` raises.
The default requested set is empty (no capabilities) until a plugin
declares them and the user consents.
"""

from __future__ import annotations

import hashlib
from typing import Any, FrozenSet, Iterable, Mapping

from .agent_plugins import AgentPluginError

# Privilege-style ids only. Unknown ids fail closed (see parse).
KNOWN_CAPABILITY_IDS = frozenset({"fs", "mcp", "browser", "shell"})


def capability_set_hash(ids: Iterable[str]) -> str:
    """Return sha256 hex of the sorted unique capability ids."""
    unique = sorted(set(ids))
    hasher = hashlib.sha256()
    for item in unique:
        hasher.update(item.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def parse_requested_capabilities(raw: Any) -> FrozenSet[str]:
    """Parse a requested capability list. ``None`` / empty → OFF.

    Raises :class:`AgentPluginError` on unknown ids or a non-list payload.
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, str) or not isinstance(raw, (list, tuple, set, frozenset)):
        raise AgentPluginError("requested capabilities must be a list of ids")
    parsed = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise AgentPluginError("requested capabilities must be a list of ids")
        cap = item.strip()
        if cap not in KNOWN_CAPABILITY_IDS:
            raise AgentPluginError(f"unknown capability id: {cap}")
        parsed.append(cap)
    return frozenset(parsed)


def requested_capabilities_from_manifest(manifest: Mapping[str, Any]) -> FrozenSet[str]:
    """Read requested ids from ``extensions.marionette.requested_capabilities``.

    Missing or empty → default OFF. Unknown ids fail closed.
    """
    extensions = manifest.get("extensions")
    if not isinstance(extensions, dict):
        return frozenset()
    marionette = extensions.get("marionette")
    if not isinstance(marionette, dict):
        return frozenset()
    if "requested_capabilities" not in marionette:
        return frozenset()
    return parse_requested_capabilities(marionette.get("requested_capabilities"))
