"""Structured diff / change-summary utility.

Reason over DELTAS, not full dumps. Given two text blobs (two versions of a
file, or two tool outputs), produce a compact one-line summary describing the
change as a handful of per-region deltas instead of re-dumping the whole file.

Pure functions, stdlib-only (difflib). Self-contained: no coupling to the
conversation layer. Other layers can call this later to shrink context.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import List

__all__ = ["Region", "line_diff_regions", "summarize_change"]


@dataclass(frozen=True)
class Region:
    """A contiguous changed region, addressed by 1-based new-file line numbers.

    start_line / end_line refer to lines in the NEW blob for replace/insert
    regions. For a pure deletion (nothing left in the new blob at that spot),
    start_line == end_line marks the new-file line the deletion sits before, and
    added == 0.
    """

    start_line: int
    end_line: int
    added: int
    removed: int


def _split_lines(text: str) -> List[str]:
    """Split into lines without keeping newline terminators.

    An empty string yields an empty list (zero lines), not [""].
    """
    if text == "":
        return []
    return text.split("\n")


def line_diff_regions(old: str, new: str) -> List[Region]:
    """Return the list of changed regions between old and new.

    Equal spans produce no region. Each replace/insert/delete opcode from
    difflib.SequenceMatcher becomes exactly one Region.
    """
    old_lines = _split_lines(old)
    new_lines = _split_lines(new)

    matcher = SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    regions: List[Region] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed = i2 - i1
        added = j2 - j1
        if tag == "delete":
            # Nothing added; anchor at the new-file line the deletion precedes.
            start = j1 + 1
            regions.append(
                Region(start_line=start, end_line=start, added=0, removed=removed)
            )
        else:  # "replace" or "insert"
            start = j1 + 1
            end = j2  # inclusive last new line
            regions.append(
                Region(
                    start_line=start,
                    end_line=end,
                    added=added,
                    removed=removed,
                )
            )
    return regions


def _format_region(region: Region) -> str:
    if region.start_line == region.end_line:
        loc = "L{0}".format(region.start_line)
    else:
        loc = "L{0}-L{1}".format(region.start_line, region.end_line)
    return "{0} (+{1}/-{2})".format(loc, region.added, region.removed)


def summarize_change(
    old: str,
    new: str,
    *,
    max_regions: int = 20,
    context: int = 0,
) -> str:
    """Summarize the change between two blobs as a compact one-line delta.

    Returns a short human-readable string such as::

        3 lines changed across 2 region(s): L12-L15 (+3/-1), L40 (+0/-2)

    Edge cases:
      - identical inputs -> "no change"
      - empty old -> "new file, N lines"
      - empty new -> "deleted, N lines"
      - more than max_regions regions -> cap and append "(+K more regions)"

    The ``context`` parameter is accepted for API symmetry with diff tooling;
    it does not change the compact summary (this util deliberately never dumps
    file contents), but callers may pass it through uniformly.
    """
    if max_regions < 1:
        max_regions = 1
    # Touch context so linters/callers see it as a real accepted knob; it has
    # no effect on the compact, content-free summary.
    _ = context

    if old == new:
        return "no change"

    if old == "":
        n = len(_split_lines(new))
        return "new file, {0} line{1}".format(n, "" if n == 1 else "s")

    if new == "":
        n = len(_split_lines(old))
        return "deleted, {0} line{1}".format(n, "" if n == 1 else "s")

    regions = line_diff_regions(old, new)
    if not regions:
        # Content differs only in a way that yields no line regions (e.g. a
        # trailing newline difference collapses to equal line lists).
        return "no change"

    total_changed = sum(r.added + r.removed for r in regions)
    region_count = len(regions)

    shown = regions[:max_regions]
    parts = [_format_region(r) for r in shown]
    suffix = ""
    hidden = region_count - len(shown)
    if hidden > 0:
        suffix = " (+{0} more region{1})".format(hidden, "" if hidden == 1 else "s")

    return "{0} line{1} changed across {2} region{3}: {4}{5}".format(
        total_changed,
        "" if total_changed == 1 else "s",
        region_count,
        "" if region_count == 1 else "s",
        ", ".join(parts),
        suffix,
    )
