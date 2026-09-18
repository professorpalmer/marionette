#!/usr/bin/env python3
"""Cursor beforeSubmitPrompt: Jev skill suggestion + refined silly-humans."""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("HARNESS_JEV", "1")

ROOTS = []
here = Path(__file__).resolve()
if here.parent.name == "scripts":
    ROOTS.append(here.parents[1])
ROOTS.append(Path.home() / "Projects" / "marionette")
env_root = os.environ.get("MARIONETTE_ROOT")
if env_root:
    ROOTS.insert(0, Path(env_root))
for root in ROOTS:
    try:
        if (root / "harness" / "jev" / "hook.py").is_file():
            sys.path.insert(0, str(root))
            break
    except OSError:
        continue

from harness.jev.hook import run_stdin


def main() -> int:
    try:
        raw = sys.stdin.read()
    except Exception:
        print("{}")
        return 0
    try:
        print(run_stdin(raw))
    except Exception:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
