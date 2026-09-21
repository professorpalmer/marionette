"""Absorb chat Completions snapshot replays into incremental text.

OpenRouter/Gemini often resend the whole message (or message+message) after
true token deltas. Cursor CLI already skips that; Completions did not.

Only a cumulative snapshot is absorbed: ``incoming`` starts with the whole
``accumulated`` text. Prefix / suffix / crumb heuristics are deliberately
absent -- a later ``###`` chunk is not a replay of the opening ``###``, and a
lone ``**`` delta is half of a bold marker. Anything dropped here diverges the
streamed bubble from the final message and paints the answer twice.
"""

from __future__ import annotations

import re

# Snapshot floor: a "snapshot" of fewer chars than this is just a short delta.
STREAM_SNAPSHOT_MIN_CHUNK = 12

_BLANK_SPLIT = re.compile(r"\n\s*\n")


def collapse_repeated_thought_blocks(text, min_chunk=STREAM_SNAPSHOT_MIN_CHUNK):
    """Keep one copy when the whole body is the same paragraph repeated."""
    raw = text or ""
    trimmed = raw.strip()
    if not trimmed:
        return raw
    parts = [part.strip() for part in _BLANK_SPLIT.split(trimmed) if part.strip()]
    if (
        len(parts) >= 2
        and len(parts[0]) >= min_chunk
        and all(part == parts[0] for part in parts)
    ):
        return parts[0]
    return raw


def absorb_stream_snapshot(accumulated, incoming, min_chunk=STREAM_SNAPSHOT_MIN_CHUNK):
    """Return the new suffix to append, or empty when ``incoming`` is a replay."""
    acc = accumulated or ""
    inc = incoming or ""
    if not inc:
        return ""
    collapsed_inc = collapse_repeated_thought_blocks(inc, min_chunk=min_chunk)
    if not acc:
        return collapsed_inc
    if inc.startswith(acc):
        rest = inc[len(acc):]
        if not rest.strip():
            # Exact replay. Short acc is a repeated character ("#" + "#"),
            # not a Completions snapshot.
            return "" if len(acc) >= min_chunk else inc
        if len(acc) >= min_chunk and rest.strip() == acc.strip():
            return ""
        if collapsed_inc != inc and collapsed_inc.startswith(acc):
            return collapsed_inc[len(acc):]
        return rest
    if len(acc) >= min_chunk and inc.strip() == acc.strip():
        return ""
    if len(acc) >= min_chunk and collapsed_inc.strip() == acc.strip():
        return ""
    return inc
