"""Resolve the git checkout workers should use for a workspace root.

Marionette Home opens ``~/.marionette`` (parent) while the real clone lives at
``~/.marionette/marionette``. Swarm/parallel/implement dispatch must pin
workers to that checkout — not the non-git parent — so briefs and cwd match.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional, Tuple

from .paths import git_toplevel

_cache_lock = threading.Lock()
# key -> (resolved_path, expires_at_monotonic)
_cache: dict[str, Tuple[str, float]] = {}

# Prefer these basenames (case-insensitive) when Home has multiple first-level
# git children. First match in this order wins (marionette over pm-harness).
_PREFERRED_GIT_CHILD_PRIORITY = ("marionette", "pm-harness", "pmharness")
_PREFERRED_GIT_CHILD_NAMES = frozenset(_PREFERRED_GIT_CHILD_PRIORITY)

# Short TTL so mid-session git layout changes (init child, delete .git) are
# picked up without forcing every hot dispatch path to readdir + probe.
_CACHE_TTL_SECONDS = 30.0


def clear_effective_repo_cache() -> None:
    """Drop the per-root resolve cache (tests / rare path invalidation)."""
    with _cache_lock:
        _cache.clear()


def resolve_effective_repo(root: str) -> str:
    """Map a workspace root to the git checkout workers should analyze/edit.

    1. If ``root`` is inside a git work tree, return ``git rev-parse
       --show-toplevel`` for it (never raises — falls back to ``root``).
    2. Else if ``root`` contains exactly one first-level child directory that
       is a git checkout (has a ``.git`` file or directory), return that child
       (Marionette Home layout).
    3. Else if multiple git children exist, pick the highest-priority preferred
       basename (``marionette`` > ``pm-harness`` > ``pmharness``,
       case-insensitive). No preferred match leaves ``root`` unchanged.
    4. Anything else: return ``root`` unchanged.

    Results are cached by normalized absolute path with a short TTL so hot
    dispatch paths do not re-probe every turn, but mid-session git layout
    changes still refresh. Never mutates caller config.
    """
    if not (root or "").strip():
        return root or ""
    try:
        key = os.path.normpath(os.path.abspath(root))
    except Exception:
        key = (root or "").strip()
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            result, expires = cached
            if now < expires:
                return result
    result = _resolve_uncached(key)
    with _cache_lock:
        _cache[key] = (result, time.monotonic() + float(_CACHE_TTL_SECONDS))
    return result


def _is_git_checkout(path: str) -> bool:
    marker = os.path.join(path, ".git")
    return os.path.isdir(marker) or os.path.isfile(marker)


def _preferred_git_child(git_children: list[str]) -> Optional[str]:
    """Return the highest-priority preferred-name git child, or None."""
    by_name = {
        os.path.basename(child).lower(): child for child in git_children
    }
    for name in _PREFERRED_GIT_CHILD_PRIORITY:
        child = by_name.get(name)
        if child is not None:
            return child
    return None


def _return_child_toplevel(child: str) -> str:
    # Marker-first (same rationale as _resolve_uncached fast path).
    if _is_git_checkout(child):
        try:
            return os.path.normpath(os.path.abspath(child))
        except Exception:
            return child
    try:
        child_top = git_toplevel(child)
        if child_top:
            return child_top
    except Exception:
        pass
    try:
        return os.path.normpath(os.path.abspath(child))
    except Exception:
        return child


def _resolve_uncached(root: str) -> str:
    # Fast path: root itself is a checkout. Prefer the filesystem .git marker
    # over `git rev-parse` so mocked subprocess.run in tests (and other
    # Popen patches) cannot rewrite a valid path into garbage stdout.
    if _is_git_checkout(root):
        try:
            return os.path.normpath(os.path.abspath(root))
        except Exception:
            return root
    try:
        top = git_toplevel(root)
        if top:
            return top
    except Exception:
        pass
    try:
        if not os.path.isdir(root):
            return root
        git_children: list[str] = []
        for name in os.listdir(root):
            child = os.path.join(root, name)
            try:
                if not os.path.isdir(child):
                    continue
                if _is_git_checkout(child):
                    git_children.append(child)
            except Exception:
                continue
        if len(git_children) == 1:
            return _return_child_toplevel(git_children[0])
        if len(git_children) > 1:
            preferred = _preferred_git_child(git_children)
            if preferred is not None:
                return _return_child_toplevel(preferred)
        return root
    except Exception:
        return root
