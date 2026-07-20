"""Single source of truth for path-containment checks.

The harness had three near-identical containment primitives that quietly
disagreed on the boundary case (is the parent directory itself "inside" itself?):

  - ``is_safe_path`` (file tools) treats ``path == parent`` as safe -- operating
    on the workspace ROOT itself (e.g. list_dir on the repo) is legitimate.
  - worktrees._is_confined treated ``path == parent`` as a violation -- correct
    for worktree confinement, where a managed worktree must live strictly INSIDE
    the managed directory and may never be the managed directory itself.

Two copies of the same security check that differ on a boundary is a latent
confinement bug. Collapse the logic here; the boundary semantics stay explicit
via ``allow_equal`` so each call site keeps its correct, intended behavior.
``is_safe_path`` is the allow_equal=True wrapper -- import it from here, do not
redefine it in tool_dispatch / web_tools / conversation.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import OrderedDict
from typing import Optional

# Cache git toplevel lookups by resolved workspace path. Nested workspaces under
# the same clone hit this on every read_file; subprocess cost is not worth repeating.
# Bounded + TTL so a long-lived backend that touches many workspaces cannot leak
# memory or serve a stale toplevel after a repo is moved.
_GIT_TOPLEVEL_CACHE_CAP = 256
_GIT_TOPLEVEL_CACHE_TTL_S = 300.0
_git_toplevel_lock = threading.Lock()
# key -> (toplevel_or_None, expires_at_monotonic); OrderedDict for LRU eviction.
_git_toplevel_cache: OrderedDict[str, tuple[Optional[str], float]] = OrderedDict()


def _resolve(path: str) -> str:
    """Resolve symlinks and normalize, without ``os.path.realpath`` hangs on Windows.

    ``os.path.realpath`` on Windows can deadlock or run indefinitely when the
    path does not exist (ntpath iterates parent directories via the Win32 API).
    On POSIX we keep realpath for symlink-resilient confinement; on Windows we
    fall back to ``abspath + normpath`` which is safe and fast.

    All path normalization inside this module must go through ``_resolve`` — never
    call bare ``os.path.realpath`` on caller-supplied paths (they may not exist).
    """
    if os.name == "nt":
        normalized = os.path.normpath(os.path.abspath(path))
        # Plain abspath keeps 8.3 short names (e.g. RUNNER~1 on CI), which
        # breaks containment comparisons against long-form spellings of the
        # same location. realpath only misbehaves on NONEXISTENT paths, so
        # resolve the longest existing ancestor (safe, single resolution)
        # and reattach the nonexistent tail unchanged.
        existing = normalized
        tail: list[str] = []
        try:
            while existing and not os.path.exists(existing):
                head, part = os.path.split(existing)
                if head == existing or not part:
                    return normalized
                tail.append(part)
                existing = head
            resolved = os.path.realpath(existing)
        except Exception:
            return normalized
        if tail:
            return os.path.normpath(os.path.join(resolved, *reversed(tail)))
        return resolved
    try:
        return os.path.realpath(path)
    except Exception:
        return os.path.normpath(os.path.abspath(path))


def _git_toplevel_cache_get(key: str) -> tuple[bool, Optional[str]]:
    """Return ``(hit, value)`` under the module lock; expired entries are dropped."""
    now = time.monotonic()
    with _git_toplevel_lock:
        hit = _git_toplevel_cache.get(key)
        if hit is None:
            return False, None
        value, expires_at = hit
        if now >= expires_at:
            del _git_toplevel_cache[key]
            return False, None
        _git_toplevel_cache.move_to_end(key)
        return True, value


def _git_toplevel_cache_put(key: str, toplevel: Optional[str]) -> None:
    """Store a lookup result with TTL; evict oldest entries past the cap."""
    expires_at = time.monotonic() + _GIT_TOPLEVEL_CACHE_TTL_S
    with _git_toplevel_lock:
        _git_toplevel_cache[key] = (toplevel, expires_at)
        _git_toplevel_cache.move_to_end(key)
        while len(_git_toplevel_cache) > _GIT_TOPLEVEL_CACHE_CAP:
            _git_toplevel_cache.popitem(last=False)

def path_within(path: str, parent: str, *, allow_equal: bool) -> bool:
    """Return True if ``path`` resolves inside ``parent`` (symlinks resolved).

    allow_equal=True  -> ``path == parent`` counts as inside (file tools: the
                         workspace root is a valid operation target).
    allow_equal=False -> ``path == parent`` is rejected (confinement: must be
                         strictly nested, never the boundary directory itself).

    Never raises: an unresolvable / cross-volume comparison returns False (fail
    closed, the safe default for a security check).
    """
    try:
        real_path = _resolve(path)
        real_parent = _resolve(parent)
        # Windows drive/component casing drifts across APIs (env vs dialog vs
        # agent-reported paths). Fold before comparing so containment does not
        # spuriously deny an absolute path that is under the open workspace.
        if os.name == "nt":
            real_path = os.path.normcase(real_path)
            real_parent = os.path.normcase(real_parent)
        if os.path.commonpath([real_parent, real_path]) != real_parent:
            return False
        if not allow_equal and real_path == real_parent:
            return False
        return True
    except ValueError:
        return False


def is_safe_path(path: str, parent: str) -> bool:
    """True if ``path`` is inside ``parent`` (the workspace root itself counts).

    File-tool / PDF / web-download confinement wrapper around
    ``path_within(..., allow_equal=True)``. Single definition -- re-export from
    call sites; do not copy this function into other modules.
    """
    return path_within(path, parent, allow_equal=True)


def _strip_file_uri(raw: str) -> str:
    """Normalize ``file://`` / ``file:///C:/...`` forms to a filesystem path."""
    text = (raw or "").strip()
    if not text.lower().startswith("file:"):
        return text
    rest = text[5:]
    if rest.startswith("///"):
        rest = rest[3:]
    elif rest.startswith("//"):
        rest = rest[2:]
    elif rest.startswith("/"):
        rest = rest[1:]
    # file:///C:/foo → C:/foo (keep leading slash for POSIX /home/...)
    if len(rest) >= 2 and rest[1] == ":" and rest[0] == "/":
        rest = rest[1:]
    elif len(rest) >= 3 and rest[0] == "/" and rest[2] == ":" and rest[1].isalpha():
        rest = rest[1:]
    return rest


def _looks_absolute(path: str) -> bool:
    """True for POSIX abs, Windows drive abs, or UNC — including forward slashes."""
    if not path:
        return False
    if os.path.isabs(path):
        return True
    if len(path) >= 3 and path[1] == ":" and path[2] in "/\\":
        return True
    if path.startswith("\\\\") or path.startswith("//"):
        return True
    return False


def is_git_restricted_path(rel_posix: str) -> bool:
    """True when a repo-relative path targets the ``.git`` directory itself.

    Matches ``.git`` and ``.git/...`` only — not ``.gitignore`` or ``.github``.
    """
    parts = [p for p in rel_posix.replace("\\", "/").split("/") if p and p != "."]
    return ".git" in parts


def git_toplevel(repo: str) -> Optional[str]:
    """Return the git work-tree toplevel containing ``repo``, or None.

    Uses ``git rev-parse --show-toplevel``. When the open workspace is a nested
    subdirectory of a larger clone, callers can widen *read* roots to this
    toplevel while keeping writes confined to ``repo``. Results are cached by
    resolved path (including a negative cache when not inside a work tree).
    """
    if not repo:
        return None
    try:
        key = _resolve(repo)
    except Exception:
        key = os.path.normpath(os.path.abspath(repo))
    hit, cached = _git_toplevel_cache_get(key)
    if hit:
        return cached
    toplevel: Optional[str] = None
    try:
        proc = subprocess.run(
            ["git", "-C", key, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if proc.returncode == 0:
            out = (proc.stdout or "").strip()
            if out:
                toplevel = _resolve(out)
    except Exception:
        toplevel = None
    _git_toplevel_cache_put(key, toplevel)
    return toplevel


def resolve_workspace_path(repo: str, user_path: str) -> tuple[str, str]:
    """Resolve a user/editor path to ``(absolute_path, repo_relative_posix)``.

    Accepts workspace-relative paths **or** absolute paths that fall under
    ``repo``. Normalizes separators and uses the same resolve/containment
    rules as ``path_within``. Raises ``ValueError`` when the path is missing
    or escapes the workspace.
    """
    raw = _strip_file_uri(user_path)
    if not raw:
        raise ValueError("Missing path")
    if not repo:
        raise ValueError("No open workspace")

    repo_abs = _resolve(repo)

    if _looks_absolute(raw):
        abs_path = _resolve(raw)
    else:
        abs_path = _resolve(
            os.path.join(repo_abs, raw.replace("/", os.sep).replace("\\", os.sep))
        )

    if not path_within(abs_path, repo_abs, allow_equal=True):
        raise ValueError("Access denied: path escapes workspace")

    try:
        rel = os.path.relpath(abs_path, repo_abs)
    except ValueError as exc:
        raise ValueError("Access denied: path escapes workspace") from exc
    rel_posix = rel.replace("\\", "/")
    if rel_posix == ".." or rel_posix.startswith("../"):
        raise ValueError("Access denied: path escapes workspace")
    if rel_posix == ".":
        rel_posix = ""
    return abs_path, rel_posix
