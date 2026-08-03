"""Steering helpers for ``search_files``: multi-path input and zero-match hints.

A bare "no matches" is the least useful answer a search can give: the pilot
cannot tell a genuine absence from wrong casing, a hidden file, or an
over-escaped regex. These helpers describe the near miss without changing what
counts as a match.

Everything here is engine-agnostic. The caller supplies a bounded ``count``
probe so ripgrep and the pure-Python fallback produce the same hints.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Optional, Sequence

# Probe kinds a caller must be able to count. Bounded on purpose: at most
# three extra scans, and the literal probe only runs for regex-looking queries.
PROBE_CASE_INSENSITIVE = "case_insensitive"
PROBE_HIDDEN = "hidden"
PROBE_LITERAL = "literal"

_REGEX_METACHAR_RE = re.compile(r"[.\[\](){}?*+^$\\|]")
_PATH_SPLIT_RE = re.compile(r"[\s,]+")


def is_multiline_query(query: str) -> bool:
    """True when the query spans lines and needs whole-text (not per-line) matching."""
    return "\n" in (query or "")


def has_regex_metacharacters(query: str) -> bool:
    return bool(_REGEX_METACHAR_RE.search(query or ""))


def resolve_search_paths(raw: object, repo: str) -> tuple[list[str], list[str]]:
    """Normalize the ``path`` argument into (search_paths, skipped_paths).

    Backward compatible by construction: a missing path yields the repo-root
    default and any single string that resolves on disk is returned unchanged,
    spaces included. Only a string that does *not* resolve is reconsidered as a
    whitespace/comma separated list, and only when at least one part exists.
    """
    if isinstance(raw, (list, tuple)):
        candidates = [str(part).strip() for part in raw if str(part).strip()]
        if not candidates:
            return [""], []
        existing = [p for p in candidates if _exists_under(p, repo)]
        missing = [p for p in candidates if p not in existing]
        return (existing or candidates[:1]), missing

    text = str(raw or "").strip()
    if not text:
        return [""], []
    if _exists_under(text, repo):
        return [text], []

    parts = [p for p in _PATH_SPLIT_RE.split(text) if p]
    if len(parts) < 2:
        return [text], []
    existing = [p for p in parts if _exists_under(p, repo)]
    if not existing:
        return [text], []
    return existing, [p for p in parts if p not in existing]


def _exists_under(path: str, repo: str) -> bool:
    absolute = path if os.path.isabs(path) else os.path.join(repo, path)
    return os.path.exists(absolute)


def skipped_paths_note(skipped: Sequence[str], *, cap: int = 3) -> str:
    """One-line note naming the path arguments that did not resolve."""
    if not skipped:
        return ""
    shown = ", ".join(skipped[:cap])
    if len(skipped) > cap:
        shown += f" (+{len(skipped) - cap} more)"
    return f"(skipped {len(skipped)} path(s) that do not exist: {shown})"


def zero_match_steering_hint(
    query: str,
    count_probe: Callable[[str], int],
) -> Optional[str]:
    """Return one hint explaining a zero-match search, or None.

    ``count_probe(kind)`` must return the number of matches a relaxed variant
    of the query would find; any failure should be reported as 0 rather than
    raised. Probes run in order of how often they explain the miss, and the
    first hit wins so at most one hint is ever produced.
    """
    case_hits = _safe_count(count_probe, PROBE_CASE_INSENSITIVE)
    if case_hits > 0:
        return (
            f"0 exact matches, but {case_hits} case-insensitive match(es) — "
            "the query's casing is probably wrong."
        )

    hidden_hits = _safe_count(count_probe, PROBE_HIDDEN)
    if hidden_hits > 0:
        return (
            f"0 matches in visible files, but {hidden_hits} match(es) in hidden "
            "or ignored files, which are skipped by default. Pass the hidden "
            "path explicitly to include them."
        )

    if has_regex_metacharacters(query):
        literal_hits = _safe_count(count_probe, PROBE_LITERAL)
        if literal_hits > 0:
            return (
                f"0 regex matches, but {literal_hits} literal match(es) — the "
                "query's regex metacharacters need escaping, or pass a plainer "
                "substring."
            )

    return None


def _safe_count(count_probe: Callable[[str], int], kind: str) -> int:
    try:
        return int(count_probe(kind) or 0)
    except Exception:
        return 0
