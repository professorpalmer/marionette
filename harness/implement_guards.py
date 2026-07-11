from __future__ import annotations

"""Hard dispatch guards for run_implement / run_parallel.

The harness's product edge is fanning hard work across workers. A single
``run_implement`` that asks one worker to REWRITE a 700+ line file is the
anti-pattern we exist to prevent -- refuse it at the tool layer and tell the
pilot to split via ``run_parallel``.
"""

import os
import re
from typing import Optional

# Default ceiling: one worker may own this many lines of a single-file rewrite
# before the harness refuses and forces a multi-worker split.
_DEFAULT_MAX_LINES = 250

# Goals that clearly ask for a full-file rewrite / regenerate, not a small edit.
_REWRITE_RE = re.compile(
    r"\b(?:"
    r"rewrite|re-?write|regenerate|re-?generate|"
    r"complete\s+rewrite|full\s+rewrite|"
    r"replace\s+(?:the\s+)?(?:entire|whole)\s+(?:file|contents?)|"
    r"from\s+scratch"
    r")\b",
    re.IGNORECASE,
)

# Path-like tokens: posix or windows, with a common source extension.
_PATH_RE = re.compile(
    r"(?:"
    r"[A-Za-z]:[\\/][^\s'\"`|;<>]+"  # Windows abs
    r"|"
    r"(?:\.{0,2}[\\/])?[A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+)+"  # rel multi-seg
    r"|"
    r"[A-Za-z0-9_.-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|rb|php|lua|cs|cpp|c|h|swift|kt|m|mm)"
    r")"
)


def max_single_file_rewrite_lines() -> int:
    raw = (os.environ.get("HARNESS_IMPLEMENT_MAX_FILE_LINES", "") or "").strip()
    if not raw:
        return _DEFAULT_MAX_LINES
    try:
        return max(50, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_LINES


def extract_goal_paths(goal: str) -> list[str]:
    """Best-effort path tokens from a worker goal string (deduped, order preserved)."""
    found: list[str] = []
    seen: set[str] = set()
    for m in _PATH_RE.finditer(goal or ""):
        raw = m.group(0).strip().strip("'\"`")
        # Drop trailing punctuation the regex may have absorbed.
        raw = raw.rstrip(").,;:]}")
        if not raw:
            continue
        key = raw.replace("\\", "/").lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(raw)
    return found


def resolve_repo_file(repo: str, rel_or_abs: str) -> Optional[str]:
    """Return an absolute path under ``repo`` when the candidate exists as a file."""
    if not repo or not rel_or_abs:
        return None
    repo_abs = os.path.abspath(repo)
    cand = rel_or_abs
    if os.path.isabs(cand):
        path = os.path.abspath(cand)
    else:
        path = os.path.abspath(os.path.join(repo_abs, cand.replace("\\", os.sep)))
    try:
        # Must stay inside the repo (no .. escapes).
        common = os.path.commonpath([repo_abs, path])
        if os.path.normcase(common) != os.path.normcase(repo_abs):
            return None
    except ValueError:
        return None
    if os.path.isfile(path):
        return path
    return None


def file_line_count(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def check_oversized_single_file_rewrite(goal: str, repo: str) -> Optional[str]:
    """Return a refusal message when ``goal`` asks one worker to rewrite a huge file.

    Returns ``None`` when the goal is fine to dispatch as-is. Disable with
    ``HARNESS_IMPLEMENT_FANOUT_GUARD=0``.
    """
    if (os.environ.get("HARNESS_IMPLEMENT_FANOUT_GUARD", "1") or "1").strip() in (
        "0", "false", "no", "off",
    ):
        return None
    goal = (goal or "").strip()
    if not goal or not repo:
        return None
    if not _REWRITE_RE.search(goal):
        return None

    # Sectioned goals ("lines 1-200", "part 2 of 4") are already fan-out shaped.
    if re.search(r"\b(?:lines?\s+\d+\s*[-–—]\s*\d+|part\s+\d+\s+of\s+\d+|section\s+\d+)\b",
                 goal, re.IGNORECASE):
        return None

    ceiling = max_single_file_rewrite_lines()
    oversized: list[tuple[str, int]] = []
    for token in extract_goal_paths(goal):
        path = resolve_repo_file(repo, token)
        if not path:
            continue
        lines = file_line_count(path)
        if lines > ceiling:
            try:
                rel = os.path.relpath(path, os.path.abspath(repo)).replace("\\", "/")
            except Exception:
                rel = path
            oversized.append((rel, lines))

    if not oversized:
        return None

    parts = ", ".join(f"{rel} ({n} lines)" for rel, n in oversized)
    return (
        f"REFUSED: single-worker rewrite of oversized file(s): {parts}. "
        f"Harness ceiling is {ceiling} lines per worker "
        f"(HARNESS_IMPLEMENT_MAX_FILE_LINES). Split into run_parallel goals "
        f"that each own a disjoint section (e.g. lines 1-{ceiling}, "
        f"{ceiling + 1}-{ceiling * 2}, ...) or supporting files only. "
        f"This is the fan-out the harness exists to enforce -- do not re-issue "
        f"the same whole-file rewrite on one worker."
    )
