"""Single source of truth for path-containment checks.

The harness had three near-identical containment primitives that quietly
disagreed on the boundary case (is the parent directory itself "inside" itself?):

  - conversation.is_safe_path / web_tools.is_safe_path treated ``path == parent``
    as safe -- correct for file tools, where operating on the workspace ROOT
    itself (e.g. list_dir on the repo) is legitimate.
  - worktrees._is_confined treated ``path == parent`` as a violation -- correct
    for worktree confinement, where a managed worktree must live strictly INSIDE
    the managed directory and may never be the managed directory itself.

Two copies of the same security check that differ on a boundary is a latent
confinement bug. Collapse the logic here; the boundary semantics stay explicit
via ``allow_equal`` so each call site keeps its correct, intended behavior.
"""
from __future__ import annotations

import os
from pathlib import Path


def _resolve(path: str) -> str:
    """Resolve symlinks and normalize, without ``os.path.realpath`` hangs on Windows.

    ``os.path.realpath`` on Windows can deadlock or run indefinitely when the
    path does not exist (ntpath iterates parent directories via the Win32 API).
    On POSIX we keep realpath for symlink-resilient confinement; on Windows we
    fall back to ``abspath + normpath`` which is safe and fast.
    """
    if os.name == "nt":
        return os.path.normpath(os.path.abspath(path))
    try:
        return os.path.realpath(path)
    except Exception:
        return os.path.normpath(os.path.abspath(path))


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
        if os.path.commonpath([real_parent, real_path]) != real_parent:
            return False
        if not allow_equal and real_path == real_parent:
            return False
        return True
    except ValueError:
        return False
