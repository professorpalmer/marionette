"""Neutralize dest-repo git hooks and fsmonitor on dest spawns.

Path confinement is not enough: ``git -C repo`` still reads that repo's
``.git/config`` (aliases, hooksPath, fsmonitor, diff.external). Dest
spawns prefix ``git_extra_args``. Do not wipe system/global gitconfig —
that drops Windows ``core.autocrlf`` and makes a just-committed tree
look dirty. Never raises.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional, Tuple

_GIT_EXTRA_ARGS = (
    "-c",
    "core.hooksPath=",
    "-c",
    "core.fsmonitor=",
    "-c",
    "core.fsmonitorHook=",
    "-c",
    "alias.help=help",
)


def git_spawn_env(base: Optional[Mapping[str, str]] = None) -> dict:
    """Copy ``base`` (default ``os.environ``) for dest git spawns."""
    try:
        return dict(os.environ if base is None else base)
    except Exception:
        return {}


def git_extra_args() -> Tuple[str, ...]:
    """``git -c`` flags that disable hooks, fsmonitor, and ``alias.help``."""
    return _GIT_EXTRA_ARGS
