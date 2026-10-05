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

# A dirty monorepo checkout can list tens of thousands of paths. Durable
# provenance keeps exact counts plus bounded samples; every persisted job row
# and artifact must stay small regardless of checkout size.
LIVE_DIRTY_SAMPLE_CAP = 50
_LIVE_DIRTY_LIST_KEYS = (
    "live_dirty_paths_before", "live_dirty_paths_after",
    "live_dirty_added", "live_dirty_removed",
)


def _dirty_paths(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    return [str(p) for p in raw if str(p).strip()]


def _stored_count(provenance: dict, key: str, sample: List[str]) -> int:
    count = provenance.get(key)
    if type(count) is int and count >= len(sample):
        return count
    return len(sample)


def bound_live_dirty_provenance(provenance: Any) -> Any:
    """Return provenance with live-dirty path lists replaced by bounded facts.

    Stores ``dirty_count_before/after``, samples of at most
    ``LIVE_DIRTY_SAMPLE_CAP`` paths under the original list keys, and a bounded
    added/removed delta. Idempotent: counts and the delta are computed once,
    from the full lists, and a bounded row passes through unchanged.
    """
    if not isinstance(provenance, dict):
        return provenance
    if not any(key in provenance for key in _LIVE_DIRTY_LIST_KEYS):
        return provenance
    out = dict(provenance)
    before = _dirty_paths(provenance.get("live_dirty_paths_before"))
    after = _dirty_paths(provenance.get("live_dirty_paths_after"))
    count_before = _stored_count(provenance, "dirty_count_before", before)
    count_after = _stored_count(provenance, "dirty_count_after", after)
    if "live_dirty_added" not in provenance or "live_dirty_removed" not in provenance:
        # Only an unbounded row still has the full lists the delta needs.
        before_set, after_set = set(before), set(after)
        added = [p for p in after if p not in before_set]
        removed = [p for p in before if p not in after_set]
        out["live_dirty_added_count"] = len(added)
        out["live_dirty_removed_count"] = len(removed)
    else:
        added = _dirty_paths(provenance.get("live_dirty_added"))
        removed = _dirty_paths(provenance.get("live_dirty_removed"))
    out["dirty_count_before"] = count_before
    out["dirty_count_after"] = count_after
    out["live_dirty_paths_before"] = before[:LIVE_DIRTY_SAMPLE_CAP]
    out["live_dirty_paths_after"] = after[:LIVE_DIRTY_SAMPLE_CAP]
    out["live_dirty_added"] = added[:LIVE_DIRTY_SAMPLE_CAP]
    out["live_dirty_removed"] = removed[:LIVE_DIRTY_SAMPLE_CAP]
    return out


def artifact_worker_provenance(provenance: Any) -> dict:
    """Job provenance for a result artifact: counts only, no path samples.

    The job row owns the bounded samples; artifact read surfaces fall back to
    the parent row when they need them.
    """
    if not isinstance(provenance, dict):
        return {}
    return {k: v for k, v in bound_live_dirty_provenance(provenance).items()
            if k not in _LIVE_DIRTY_LIST_KEYS}


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
