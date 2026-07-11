from __future__ import annotations

"""Seed a managed edit worktree with live-tree files the goal needs.

``git worktree add`` checks out HEAD. Untracked (and dirty-but-referenced)
files in the live repo are invisible to the worker -- historically producing
empty diffs, hallucinated paths like ``C:\\dev\\null``, and "file not found"
failures. Copy any goal-referenced paths that exist in the live tree into the
worktree before the edit engine runs.
"""

import os
import shutil
from typing import Iterable, Optional

from harness.implement_guards import extract_goal_paths, resolve_repo_file


def seed_worktree_from_goal(repo: str, wt_path: str, goal: str) -> list[str]:
    """Copy goal-referenced live files into ``wt_path`` when missing or different.

    Returns the list of relative paths seeded (posix-style). Best-effort: never
    raises for individual copy failures.
    """
    if not repo or not wt_path or not goal:
        return []
    seeded: list[str] = []
    for token in extract_goal_paths(goal):
        src = resolve_repo_file(repo, token)
        if not src:
            # Directory mention: seed untracked/dirty files under it.
            dir_src = _resolve_repo_dir(repo, token)
            if dir_src:
                for rel in _iter_files_under(repo, dir_src):
                    if _copy_into_worktree(repo, wt_path, rel):
                        seeded.append(rel)
            continue
        try:
            rel = os.path.relpath(src, os.path.abspath(repo)).replace("\\", "/")
        except Exception:
            continue
        if _copy_into_worktree(repo, wt_path, rel):
            seeded.append(rel)
    # Dedup while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for r in seeded:
        if r in seen:
            continue
        seen.add(r)
        out.append(r)
    return out


def seed_untracked_matching(repo: str, wt_path: str, prefixes: Iterable[str]) -> list[str]:
    """Copy untracked live files under any of ``prefixes`` into the worktree."""
    seeded: list[str] = []
    for prefix in prefixes or []:
        dir_src = _resolve_repo_dir(repo, prefix) or resolve_repo_file(repo, prefix)
        if dir_src and os.path.isdir(dir_src):
            for rel in _iter_files_under(repo, dir_src):
                dst = os.path.join(wt_path, rel.replace("/", os.sep))
                if os.path.exists(dst):
                    continue
                if _copy_into_worktree(repo, wt_path, rel):
                    seeded.append(rel)
    return seeded


def _resolve_repo_dir(repo: str, rel_or_abs: str) -> Optional[str]:
    if not repo or not rel_or_abs:
        return None
    repo_abs = os.path.abspath(repo)
    if os.path.isabs(rel_or_abs):
        path = os.path.abspath(rel_or_abs)
    else:
        path = os.path.abspath(os.path.join(repo_abs, rel_or_abs.replace("\\", os.sep)))
    try:
        common = os.path.commonpath([repo_abs, path])
        if os.path.normcase(common) != os.path.normcase(repo_abs):
            return None
    except ValueError:
        return None
    if os.path.isdir(path):
        return path
    return None


def _iter_files_under(repo: str, abs_dir: str) -> list[str]:
    repo_abs = os.path.abspath(repo)
    out: list[str] = []
    skip = {".git", "node_modules", ".venv", "__pycache__", ".codegraph", "dist", "build"}
    try:
        for root, dirs, files in os.walk(abs_dir):
            dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
            for name in files:
                if name.startswith("."):
                    continue
                full = os.path.join(root, name)
                try:
                    rel = os.path.relpath(full, repo_abs).replace("\\", "/")
                except Exception:
                    continue
                out.append(rel)
    except Exception:
        return []
    return out


def _copy_into_worktree(repo: str, wt_path: str, rel: str) -> bool:
    src = os.path.join(os.path.abspath(repo), rel.replace("/", os.sep))
    dst = os.path.join(wt_path, rel.replace("/", os.sep))
    if not os.path.isfile(src):
        return False
    try:
        if os.path.isfile(dst):
            # Skip identical content (cheap size+mtime, then bytes).
            try:
                if (os.path.getsize(src) == os.path.getsize(dst)
                        and os.path.getmtime(src) == os.path.getmtime(dst)):
                    return False
                with open(src, "rb") as a, open(dst, "rb") as b:
                    if a.read() == b.read():
                        return False
            except Exception:
                pass
        os.makedirs(os.path.dirname(dst) or wt_path, exist_ok=True)
        shutil.copy2(src, dst)
        return True
    except Exception:
        return False
