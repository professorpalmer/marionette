"""Reveal a filesystem path in the OS file manager.

Used by ``/api/file/reveal`` so the FILES tree works even when the Electron
preload bridge is missing ``fs.revealInFolder`` (stale shell) or the UI is
talking HTTP-only. Path containment is the caller's job -- this only opens.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional


def reveal_in_file_manager(abs_path: str) -> Optional[str]:
    """Open Finder/Explorer/xdg on ``abs_path``. Returns an error string or None."""
    raw = (abs_path or "").strip()
    if not raw:
        return "missing path"
    target = os.path.realpath(os.path.expanduser(raw))
    if not target:
        return "missing path"
    if not os.path.exists(target):
        return "Path not found"
    try:
        if sys.platform == "win32":
            # explorer's /select quirks: comma glued to the path, no quotes.
            # CREATE_NO_WINDOW is applied globally via harness.win_console.
            subprocess.Popen(["explorer", "/select," + os.path.normpath(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", target])
        else:
            parent = target if os.path.isdir(target) else (os.path.dirname(target) or target)
            subprocess.Popen(["xdg-open", parent])
    except Exception as e:
        return str(e)
    return None
