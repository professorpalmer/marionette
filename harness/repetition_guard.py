"""Stop a streamed block that turned into an exact repetition loop.

A model can fall into a loop and repeat one phrase or paragraph until it
reaches its output limit. That costs tokens and fills the transcript, and
the looped text in history can start the loop again on the next turn.

``RepetitionWatch`` judges one block (answer or reasoning). It judges only
at fixed character positions, so the result does not change with the
chunk size of the stream. It latches: after a trip it stays tripped.

A block loops when its last ``TAIL_CHARS`` characters end in an exact
period that repeats at least ``MIN_REPEATS`` times and covers at least
``MIN_SHARE`` of the tail. Text with many lines must also have few
distinct lines in that region, so numbered rows are not a loop.
"""

from __future__ import annotations

MIN_CHARS = 16_000
TAIL_CHARS = 16_000
CHECK_EVERY = 1_024
PROBE_CHARS = 60
PROBE_TRIES = 8
MIN_REPEATS = 5
MIN_SHARE = 0.5
MAX_DISTINCT_LINE_SHARE = 0.5


def _looped_span(tail: str) -> int:
    """Length of the exact periodic suffix of ``tail`` that qualifies, else 0."""
    n = len(tail)
    if n < PROBE_CHARS * 2:
        return 0
    probe = tail[-PROBE_CHARS:]
    end = n - 1
    for _ in range(PROBE_TRIES):
        prev = tail.rfind(probe, 0, end)
        if prev < 0:
            return 0
        end = prev + PROBE_CHARS - 1
        period = n - PROBE_CHARS - prev
        repeats = 1
        while (repeats + 1) * period <= n and (
            tail[n - (repeats + 1) * period:n - repeats * period]
            == tail[n - repeats * period:n - (repeats - 1) * period]
        ):
            repeats += 1
        span = repeats * period
        if repeats >= MIN_REPEATS and span >= MIN_SHARE * n:
            return span
    return 0


def _few_distinct_lines(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return True
    return len(set(lines)) <= MAX_DISTINCT_LINE_SHARE * len(lines)


def _tail_loops(tail: str) -> bool:
    span = _looped_span(tail)
    return bool(span) and _few_distinct_lines(tail[-span:])


def is_runaway_repetition(text: str) -> bool:
    """True when the end of ``text`` is a qualifying repetition loop."""
    return len(text) >= MIN_CHARS and _tail_loops(text[-TAIL_CHARS:])


class RepetitionWatch:
    """Incremental, latched loop check for one streamed block."""

    def __init__(self) -> None:
        self._tail = ""
        self._size = 0
        self._next_check = MIN_CHARS
        self.tripped = False

    def feed(self, chunk: str) -> bool:
        if self.tripped or not chunk:
            return self.tripped
        self._tail += chunk
        self._size += len(chunk)
        start = self._size - len(self._tail)
        while self._next_check <= self._size:
            upto = self._next_check - start
            self._next_check += CHECK_EVERY
            if _tail_loops(self._tail[max(0, upto - TAIL_CHARS):upto]):
                self.tripped = True
                break
        self._tail = self._tail[-TAIL_CHARS:]
        return self.tripped
