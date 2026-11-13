"""Line diff receipt for one agent file write (edit_file, write_file, hash_edit).

The transcript edit row shows the +/- counts and opens the patch. Text
files only, bounded so a large rewrite does not bloat the transcript.
Best effort: any failure gives ``None`` and the row shows no diff.
"""

from __future__ import annotations

import difflib
import os
from typing import Optional

# Files above this size get no diff (a read and a diff of the whole file
# runs on the tool hot path).
MAX_DIFF_SOURCE_BYTES = 1024 * 1024
# The stored patch keeps at most this many lines; counts stay exact.
MAX_PATCH_LINES = 400


def read_text_for_diff(path: str) -> Optional[str]:
    """Text of ``path`` before a write. A missing file is "" (a create).

    ``None`` when the file is too large, binary, or unreadable.
    """
    try:
        if not os.path.exists(path):
            return ""
        if os.path.getsize(path) > MAX_DIFF_SOURCE_BYTES:
            return None
        with open(path, "rb") as f:
            raw = f.read()
        if b"\0" in raw:
            return None
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def edit_diff(before: Optional[str], path: str, display_path: str) -> Optional[dict]:
    """``{added, removed, patch, truncated}`` for the write that changed
    ``path`` from ``before``, or ``None`` when no text diff is possible."""
    if before is None:
        return None
    after = read_text_for_diff(path)
    if after is None:
        return None
    try:
        lines = list(difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{display_path}" if before else "/dev/null",
            tofile=f"b/{display_path}",
            n=3,
        ))
    except Exception:
        return None
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    truncated = len(lines) > MAX_PATCH_LINES
    shown = lines[:MAX_PATCH_LINES]
    patch = "".join(ln if ln.endswith("\n") else ln + "\n" for ln in shown)
    return {"added": added, "removed": removed, "patch": patch, "truncated": truncated}
