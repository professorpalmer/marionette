from __future__ import annotations

"""Seed a managed edit worktree with live-tree files the goal needs.

``git worktree add`` checks out HEAD. Untracked (and dirty-but-referenced)
files in the live repo are invisible to the worker -- historically producing
empty diffs, hallucinated paths like ``C:\\dev\\null``, and "file not found"
failures. Copy any goal-referenced paths that exist in the live tree into the
worktree before the edit engine runs.

Seeding is dynamic: explicit path tokens in the goal AND dirty/untracked files
whose path components match significant goal words (so "fix the kotoba ad"
still seeds ``addons/kotoba/...`` when those files are on disk in the indexed
workspace).
"""

import os
import re
import shutil
import subprocess
from typing import Iterable, Optional

from harness.implement_guards import extract_goal_paths, resolve_repo_file

# Cap dynamic copies so a vague goal cannot flood the worktree.
_MAX_DYNAMIC_SEED = 250

_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "to", "of", "in", "on", "at", "by", "for",
    "from", "with", "into", "onto", "over", "under", "this", "that", "these",
    "those", "it", "its", "is", "are", "be", "been", "was", "were", "do", "does",
    "did", "can", "could", "should", "would", "will", "just", "please", "need",
    "needs", "make", "made", "add", "adds", "added", "fix", "fixes", "fixed",
    "update", "updates", "updated", "change", "changes", "changed", "edit",
    "edits", "edited", "rewrite", "rewrites", "implement", "implements",
    "create", "creates", "created", "write", "writes", "written", "read",
    "file", "files", "code", "worker", "workers", "swarm", "job", "jobs",
    "run", "runs", "running", "use", "using", "via", "also", "then", "than",
    "when", "where", "what", "which", "who", "how", "why", "all", "any",
    "some", "each", "every", "both", "more", "most", "other", "only", "own",
    "same", "such", "too", "very", "not", "no", "nor", "so", "if", "as",
    "but", "about", "after", "before", "between", "during", "without",
    "through", "again", "further", "once", "here", "there", "out", "up",
    "down", "off", "new", "old", "good", "bad", "bug", "bugs", "issue",
    "issues", "task", "tasks", "goal", "goals", "help", "me", "my", "our",
    "your", "you", "we", "they", "them", "their", "his", "her", "repo",
    "project", "workspace", "directory", "folder", "path", "paths",
})

_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{1,}")


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

    # Dynamic pass: dirty/untracked on-disk files matched by goal tokens.
    # Covers vague goals ("fix the kotoba ad") when the live tree already
    # has the files CodeGraph / the pilot are talking about.
    for rel in _matching_live_paths(repo, goal):
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


def goal_match_tokens(goal: str) -> set[str]:
    """Significant tokens used to match live dirty/untracked paths to a goal."""
    tokens: set[str] = set()
    for path_tok in extract_goal_paths(goal):
        norm = path_tok.replace("\\", "/").strip("/").lower()
        if not norm:
            continue
        tokens.add(norm)
        for part in norm.split("/"):
            if not part or part in (".", ".."):
                continue
            tokens.add(part)
            if "." in part:
                tokens.add(part.rsplit(".", 1)[0])
    for m in _WORD_RE.finditer(goal or ""):
        w = m.group(0).lower()
        if len(w) < 3 or w in _STOPWORDS:
            continue
        tokens.add(w)
        if "." in w:
            stem = w.rsplit(".", 1)[0]
            if len(stem) >= 3 and stem not in _STOPWORDS:
                tokens.add(stem)
    return tokens


def _matching_live_paths(repo: str, goal: str) -> list[str]:
    tokens = goal_match_tokens(goal)
    if not tokens:
        return []
    out: list[str] = []
    for rel in _list_live_dirty_paths(repo):
        if not _path_matches_tokens(rel, tokens):
            continue
        out.append(rel)
        if len(out) >= _MAX_DYNAMIC_SEED:
            break
    return out


def _path_matches_tokens(rel: str, tokens: set[str]) -> bool:
    rel_l = rel.replace("\\", "/").lower()
    parts = [p for p in rel_l.split("/") if p]
    if not parts:
        return False
    base = parts[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    candidates = set(parts)
    candidates.add(base)
    candidates.add(stem)
    candidates.add(rel_l)
    return bool(candidates & tokens)


def _list_live_dirty_paths(repo: str) -> list[str]:
    """Relative paths that differ from HEAD in the live tree (dirty + untracked)."""
    if not repo or not os.path.isdir(repo):
        return []
    try:
        p = subprocess.run(
            [
                "git", "-C", repo, "status", "--porcelain", "-uall",
                "--ignore-submodules=all",
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
    except Exception:
        return []
    if p.returncode != 0:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in p.stdout.splitlines():
        if len(line) < 4:
            continue
        path_part = line[3:]
        # renames: "old -> new"
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        path_part = path_part.strip().strip('"').replace("\\", "/")
        if not path_part or path_part.endswith("/"):
            continue
        if path_part in seen:
            continue
        seen.add(path_part)
        src = os.path.join(os.path.abspath(repo), path_part.replace("/", os.sep))
        if os.path.isfile(src):
            out.append(path_part)
    return out


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
