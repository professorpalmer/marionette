"""Resolve the git checkout workers should use for a workspace root.

Marionette Home opens ``~/.marionette`` (parent) while the real clone lives at
``~/.marionette/marionette``. Swarm/parallel/implement dispatch must pin
workers to that checkout — not the non-git parent — so briefs and cwd match.
"""
from __future__ import annotations

import os
import threading

from .paths import git_toplevel

_cache_lock = threading.Lock()
_cache: dict[str, str] = {}


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
       (Marionette Home layout). Multiple git children are ambiguous — leave
       ``root`` unchanged.
    3. Anything else: return ``root`` unchanged.

    Results are cached by normalized absolute path so hot dispatch paths do
    not re-probe git or readdir every turn.
    """
    if not (root or "").strip():
        return root or ""
    try:
        key = os.path.normpath(os.path.abspath(root))
    except Exception:
        key = (root or "").strip()
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            return cached
    result = _resolve_uncached(key)
    with _cache_lock:
        _cache[key] = result
    return result


def _is_git_checkout(path: str) -> bool:
    marker = os.path.join(path, ".git")
    return os.path.isdir(marker) or os.path.isfile(marker)


def _resolve_uncached(root: str) -> str:
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
                    if len(git_children) > 1:
                        return root
            except Exception:
                continue
        if len(git_children) == 1:
            child = git_children[0]
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
        return root
    except Exception:
        return root
