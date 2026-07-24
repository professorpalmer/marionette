"""Stream identity + delta batching for dual-channel providers (Codex/Sol).

Deltas are owned by ``(channel, stream_id)``, never by arrival order. A short
batch window coalesces same-identity frames before they hit the SSE ring so
word-sized tokens cannot burn the 512-frame replay buffer. Batching is not a
substitute for identity — barriers always flush first.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

# Coalesce same-(channel, stream_id) deltas until either threshold trips.
STREAM_DELTA_BATCH_MS = 40.0
STREAM_DELTA_BATCH_CHARS = 80


def normalize_delta_payload(val: Any) -> Tuple[str, Dict[str, Any]]:
    """Accept legacy ``str`` callbacks or rich ``{text, stream_id, ...}`` dicts."""
    if isinstance(val, dict):
        text = val.get("text")
        if text is None:
            text = val.get("delta") or ""
        text_s = text if isinstance(text, str) else str(text or "")
        meta: Dict[str, Any] = {}
        sid = val.get("stream_id")
        if sid is not None and str(sid).strip():
            meta["stream_id"] = str(sid)
        oi = val.get("output_index")
        if oi is not None:
            try:
                meta["output_index"] = int(oi)
            except (TypeError, ValueError):
                pass
        channel = val.get("channel")
        if isinstance(channel, str) and channel.strip():
            meta["channel"] = channel.strip().lower()
        return text_s, meta
    if val is None:
        return "", {}
    return (val if isinstance(val, str) else str(val)), {}


def delta_identity_key(meta: Dict[str, Any], *, default_channel: str) -> Tuple[str, str]:
    channel = str(meta.get("channel") or default_channel or "").strip().lower()
    stream_id = str(meta.get("stream_id") or "").strip()
    return channel, stream_id


class StreamDeltaBatch:
    """Accumulate same-identity text until time/char threshold or an explicit flush."""

    def __init__(
        self,
        *,
        max_ms: float = STREAM_DELTA_BATCH_MS,
        max_chars: int = STREAM_DELTA_BATCH_CHARS,
    ) -> None:
        self.max_ms = float(max_ms)
        self.max_chars = max(1, int(max_chars))
        self._channel = ""
        self._stream_id = ""
        self._text_parts: list = []
        self._meta: Dict[str, Any] = {}
        self._opened_at = 0.0

    @property
    def pending(self) -> bool:
        return bool(self._text_parts)

    def _key(self) -> Tuple[str, str]:
        return self._channel, self._stream_id

    def should_flush_for(self, channel: str, stream_id: str) -> bool:
        if not self.pending:
            return False
        return (channel, stream_id) != self._key()

    def overdue(self, now: Optional[float] = None) -> bool:
        if not self.pending:
            return False
        now = time.monotonic() if now is None else now
        if (now - self._opened_at) * 1000.0 >= self.max_ms:
            return True
        return sum(len(p) for p in self._text_parts) >= self.max_chars

    def push(self, text: str, meta: Dict[str, Any], *, default_channel: str) -> Optional[Dict[str, Any]]:
        """Append ``text``. Returns a flushed payload on identity change or overdue."""
        if not text:
            return None
        channel, stream_id = delta_identity_key(meta, default_channel=default_channel)
        flushed = None
        if self.should_flush_for(channel, stream_id):
            flushed = self.flush()
        if not self.pending:
            self._channel = channel
            self._stream_id = stream_id
            self._meta = dict(meta)
            self._opened_at = time.monotonic()
        else:
            # Keep first-seen identity fields; allow later keys to fill gaps.
            for k, v in meta.items():
                if k not in self._meta and v is not None:
                    self._meta[k] = v
        self._text_parts.append(text)
        if self.overdue():
            # Flush immediately so char/time thresholds do not wait for the
            # next identity change. If this push already flushed a prior
            # identity, return that first — the new overdue buffer is picked
            # up by the drain loop's _flush_overdue peek.
            if flushed is not None:
                return flushed
            return self.flush()
        return flushed

    def flush(self) -> Optional[Dict[str, Any]]:
        if not self._text_parts:
            return None
        text = "".join(self._text_parts)
        payload = dict(self._meta)
        payload["text"] = text
        if self._channel:
            payload["channel"] = self._channel
        if self._stream_id:
            payload["stream_id"] = self._stream_id
        self._text_parts = []
        self._meta = {}
        self._channel = ""
        self._stream_id = ""
        self._opened_at = 0.0
        return payload
