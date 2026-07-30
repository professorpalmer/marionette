"""Machine-authoritative sanitization of worker cleanliness claims.

When Marionette measured pre-existing dirty paths on the user's live checkout,
worker prose that still asserts a clean working tree / repository is neutralized.
Measured envelope facts win; findings content is otherwise preserved.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional

# Claims that the live working tree / repository / checkout is clean.
_CLEAN_CLAIM_RE = re.compile(
    r"(?is)\b(?:"
    r"(?:the\s+)?(?:working\s+tree|work\s*tree|repository|repo|checkout|"
    r"live\s+checkout|user(?:'s)?\s+checkout)\s+"
    r"(?:is|was|appears(?:\s+to\s+be)?|remains|looks)\s+clean"
    r"|"
    r"(?:working\s+tree|repository|repo|checkout)\s+(?:has|had)\s+no\s+"
    r"(?:dirty|uncommitted)\s+(?:paths?|files?|changes?)"
    r"|"
    r"(?:no|zero)\s+(?:dirty|uncommitted)\s+(?:paths?|files?|changes?)\s+"
    r"(?:in\s+)?(?:the\s+)?(?:working\s+tree|repository|repo|checkout)"
    r"|"
    r"(?:git\s+status\s+(?:is|was|shows|reported)\s+clean)"
    r")\b"
)

CLEAN_TREE_REPLACEMENT = "No new dirty paths or patch were introduced"

_MARIONETTE_ENVELOPE_NOTICE = (
    "Execution provenance (provider, model, tokens, cost, routing) comes from "
    "the Marionette job envelope, not from repository source files. "
    "Your git status describes a disposable managed worker worktree only — "
    "describe that worktree's diff status, never the user's live checkout."
)


def live_dirty_before(provenance: Any) -> List[str]:
    """Extract non-empty live_dirty_paths_before from a provenance dict."""
    if not isinstance(provenance, dict):
        return []
    raw = provenance.get("live_dirty_paths_before") or []
    if not isinstance(raw, list):
        return []
    return [str(p) for p in raw if str(p).strip()]


def sanitize_clean_tree_claims(
    text: str,
    *,
    live_dirty_paths_before: Optional[List[str]] = None,
    provenance: Any = None,
) -> str:
    """Neutralize clean-tree claims when the live checkout was already dirty.

    Replaces matching claims with ``CLEAN_TREE_REPLACEMENT`` and leaves the
    rest of the summary (findings) intact. No-op when there were no
    pre-existing dirty paths.
    """
    body = text if isinstance(text, str) else ""
    if not body:
        return body
    dirty = list(live_dirty_paths_before or [])
    if not dirty:
        dirty = live_dirty_before(provenance)
    if not dirty:
        return body
    sanitized, count = _CLEAN_CLAIM_RE.subn(CLEAN_TREE_REPLACEMENT, body)
    if count:
        # Collapse accidental double spaces after substitution mid-sentence.
        sanitized = re.sub(r"[ \t]{2,}", " ", sanitized)
        sanitized = re.sub(r"\n{3,}", "\n\n", sanitized)
    return sanitized


def marionette_envelope_notice() -> str:
    """Prompt fragment: provenance is Marionette envelope, not repo source."""
    return _MARIONETTE_ENVELOPE_NOTICE
