"""The display transcript: the rows the UI renders, stamped with append time.

Every row records when it entered the transcript (``ts``, epoch ms), so a
turn's wall-clock length can be read back after a reload instead of living
only in the browser that watched it. Stamping happens here, in the one
container every writer goes through; replacing a row in place keeps the
original time.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, SupportsIndex


def _now_ms() -> int:
    return int(time.time() * 1000)


def _stamp(row: Any, ts: int | None = None) -> Any:
    if isinstance(row, dict) and not isinstance(row.get("ts"), (int, float)):
        row["ts"] = ts if ts is not None else _now_ms()
    return row


class DisplayRows(list):
    """Rows already in hand (a loaded transcript) keep what they have: a
    missing time is unknown, never the moment they were loaded."""

    def __reduce_ex__(self, protocol: SupportsIndex):
        # copy/deepcopy/pickle rebuild through the constructor, which keeps
        # rows as they are; the default list protocol would re-append and stamp.
        return (DisplayRows, (list(self),))

    def append(self, row: Any) -> None:
        super().append(_stamp(row))

    def insert(self, index: SupportsIndex, row: Any) -> None:
        super().insert(index, _stamp(row))

    def extend(self, rows: Iterable[Any]) -> None:
        super().extend(_stamp(r) for r in rows)

    def __iadd__(self, rows: Iterable[Any]) -> "DisplayRows":  # type: ignore[override]
        self.extend(rows)
        return self

    def __setitem__(self, index: Any, value: Any) -> None:
        if isinstance(index, slice):
            super().__setitem__(index, [_stamp(r) for r in value])
            return
        prior = self[index]
        prior_ts = prior.get("ts") if isinstance(prior, dict) else None
        super().__setitem__(index, _stamp(value, prior_ts if isinstance(prior_ts, (int, float)) else None))
