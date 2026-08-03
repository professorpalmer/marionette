"""Diagnostics that turn a failed ``edit_file`` into a one-turn recovery.

Three failure shapes dominate: the edit already landed and is being re-sent,
``old_str`` is ambiguous, and ``old_str`` is a whitespace near-miss of real
file text. Each one is cheap to detect and expensive to leave unexplained.

Nothing here relaxes matching: ``edit_file`` still writes only on an exact,
unique ``old_str``. These helpers only describe *why* the exact match failed.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Optional

# An already-applied claim on a tiny string is coincidence, not evidence.
_MIN_ALREADY_APPLIED_CHARS = 8
_MATCH_LOCATION_CAP = 5
_SNIPPET_CHARS = 80


def is_edit_already_applied(content: str, old_str: str, new_str: str) -> bool:
    """True when the file already contains exactly what the edit would produce.

    Deliberately conservative so a typo'd edit is never mistaken for a no-op:
    ``new_str`` must be substantial and present verbatim, and when it differs
    from ``old_str`` the old text must be gone (still-present old text means
    the edit is at best half applied).
    """
    if not new_str or len(new_str.strip()) < _MIN_ALREADY_APPLIED_CHARS:
        return False
    if new_str not in content:
        return False
    if old_str == new_str:
        return True
    if content.count(new_str) != 1 or old_str in content:
        return False
    # A target that merely appears elsewhere is not evidence that this edit
    # landed. Require the old/new blocks to retain meaningful context; wholly
    # unrelated replacements remain an honest not-found failure.
    if len(old_str) > 20000 or len(new_str) > 20000:
        return False
    similarity = SequenceMatcher(None, old_str, new_str, autojunk=False).ratio()
    return similarity >= 0.35


def describe_match_locations(
    content: str,
    needle: str,
    *,
    cap: int = _MATCH_LOCATION_CAP,
) -> str:
    """Render up to ``cap`` occurrences of ``needle`` as ``L<line>: <snippet>``.

    Gives the pilot enough to disambiguate in one follow-up instead of
    re-reading the whole file to find the duplicates.
    """
    if not needle:
        return ""
    rows: list[str] = []
    total = 0
    start = content.find(needle)
    while start != -1:
        total += 1
        if len(rows) < cap:
            rows.append(f"  L{_line_number_at(content, start)}: {_line_snippet(content, start)}")
        start = content.find(needle, start + 1)
    if total > len(rows):
        rows.append(f"  ... and {total - len(rows)} more")
    return "\n".join(rows)


def _line_number_at(content: str, index: int) -> int:
    return content.count("\n", 0, index) + 1


def _line_snippet(content: str, index: int) -> str:
    line_start = content.rfind("\n", 0, index) + 1
    line_end = content.find("\n", line_start)
    if line_end == -1:
        line_end = len(content)
    snippet = content[line_start:line_end].strip()
    if len(snippet) > _SNIPPET_CHARS:
        snippet = snippet[: _SNIPPET_CHARS - 3] + "..."
    return snippet


def _collapse_spaces(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text)


def _strip_each_line(text: str) -> str:
    return "\n".join(line.strip() for line in text.split("\n"))


def whitespace_near_miss_hint(content: str, old_str: str) -> Optional[str]:
    """Name the whitespace difference that defeated an exact match, or None.

    Ordered from the most specific difference to the least so the pilot gets
    the narrowest correction it can act on.
    """
    if not old_str or old_str in content:
        return None

    if "\r\n" in content and "\r\n" not in old_str and old_str in content.replace("\r\n", "\n"):
        return (
            "The file uses CRLF line endings but old_str uses bare LF. Re-read "
            "the file and copy the line endings exactly."
        )

    stripped = old_str.strip()
    if stripped and stripped != old_str and stripped in content:
        return (
            "old_str matches once the leading/trailing whitespace is removed. "
            "Drop the surrounding blank space (or include the real surrounding "
            "text) and retry."
        )

    if "\t" in old_str and old_str.expandtabs(4) in content:
        return (
            "old_str is tab-indented but the file uses spaces. Copy the "
            "indentation from read_file output verbatim."
        )
    if "\t" in content and "\t" not in old_str:
        space_indented = re.sub(r"^\t+", lambda m: " " * (4 * len(m.group(0))), content, flags=re.M)
        if old_str in space_indented:
            return (
                "The file is tab-indented but old_str uses spaces. Copy the "
                "indentation from read_file output verbatim."
            )

    if _strip_each_line(old_str) in _strip_each_line(content):
        return (
            "old_str matches line-for-line except for indentation. Re-read the "
            "target lines and copy their leading whitespace exactly."
        )

    if _collapse_spaces(old_str) in _collapse_spaces(content):
        return (
            "old_str matches except for the amount of inline whitespace. "
            "Copy the spacing from read_file output exactly."
        )

    return None


def verify_written_text(path: str, expected: str) -> Optional[str]:
    """Re-read ``path`` and return an error message when it differs.

    Catches the failure mode an atomic write cannot rule out on its own: the
    replace succeeded but the bytes on disk are not what was intended (a racing
    writer, a truncated flush, an unexpected filesystem translation). Returns
    None when the file matches.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            on_disk = f.read()
    except OSError as exc:
        return f"post-write verification failed: could not re-read {path}: {exc}"
    if on_disk == expected:
        return None
    return (
        f"post-write verification failed for {path}: on-disk content differs "
        f"from the intended write (wrote {len(expected)} chars, read back "
        f"{len(on_disk)}). The change did not persist — re-read the file and retry."
    )
