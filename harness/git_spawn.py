"""Neutralize inherited git config on dest-repo git spawns.

Path confinement is not enough: ``git -C repo`` still reads that repo's
``.git/config`` (aliases, hooksPath, fsmonitor, diff.external) plus
system/global gitconfig. Dest spawns copy git_spawn_env and prefix
git_extra_args. Never raises.
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


def _null_gitconfig() -> str:
    return "NUL" if os.name == "nt" else "/dev/null"


def git_spawn_env(base: Optional[Mapping[str, str]] = None) -> dict:
    """Copy ``base`` (default ``os.environ``) and pin system/global gitconfig off."""
    try:
        env = dict(os.environ if base is None else base)
    except Exception:
        env = {}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = _null_gitconfig()
    return env


def git_extra_args() -> Tuple[str, ...]:
    """``git -c`` flags that disable hooks, fsmonitor, and ``alias.help``."""
    return _GIT_EXTRA_ARGS
