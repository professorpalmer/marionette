from __future__ import annotations

"""Session scratch bindings — durable L1 key/value, not agent memory.

JSON under the session state_dir so values survive turns and compaction
(transcript summarization must not clear them). Ephemeral working notes for
the current session only; distinct from durable cross-session ``memory``.
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple

SCRATCH_FILENAME = "session_scratch.json"

MAX_SCRATCH_KEYS = 64
MAX_SCRATCH_VALUE_CHARS = 8_000
MAX_SCRATCH_TOTAL_CHARS = 32_000


class ScratchStoreError(ValueError):
    """Raised when a scratch write would exceed size/key caps."""


class SessionScratchStore:
    """Load/save session scratch JSON under a session state_dir."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir or ""
        self.path = (
            os.path.join(self.state_dir, SCRATCH_FILENAME) if self.state_dir else ""
        )

    def _load_raw(self) -> Dict[str, str]:
        if not self.path or not os.path.isfile(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return {}
            out: Dict[str, str] = {}
            for key, value in data.items():
                k = str(key or "").strip()
                if not k:
                    continue
                out[k] = "" if value is None else str(value)
            return out
        except Exception:
            return {}

    def _save_raw(self, data: Dict[str, str]) -> None:
        if not self.path or not self.state_dir:
            return
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(data, indent=2, ensure_ascii=False))
            os.replace(tmp, self.path)
        except Exception:
            pass

    @staticmethod
    def _total_chars(data: Dict[str, str]) -> int:
        return sum(len(k) + len(v) for k, v in data.items())

    def get(self, key: str) -> Optional[str]:
        cleaned = (key or "").strip()
        if not cleaned:
            return None
        return self._load_raw().get(cleaned)

    def set(self, key: str, value: str) -> None:
        cleaned = (key or "").strip()
        if not cleaned:
            raise ScratchStoreError("scratch key must be non-empty")
        text = "" if value is None else str(value)
        if len(text) > MAX_SCRATCH_VALUE_CHARS:
            raise ScratchStoreError(
                f"scratch value exceeds {MAX_SCRATCH_VALUE_CHARS} chars "
                f"(got {len(text)})"
            )
        data = self._load_raw()
        replacing = cleaned in data
        if not replacing and len(data) >= MAX_SCRATCH_KEYS:
            raise ScratchStoreError(
                f"scratch store is full ({MAX_SCRATCH_KEYS} keys max)"
            )
        trial = dict(data)
        trial[cleaned] = text
        total = self._total_chars(trial)
        if total > MAX_SCRATCH_TOTAL_CHARS:
            raise ScratchStoreError(
                f"scratch store would exceed {MAX_SCRATCH_TOTAL_CHARS} total chars "
                f"(got {total})"
            )
        self._save_raw(trial)

    def delete(self, key: str) -> bool:
        cleaned = (key or "").strip()
        if not cleaned:
            return False
        data = self._load_raw()
        if cleaned not in data:
            return False
        del data[cleaned]
        self._save_raw(data)
        return True

    def list(self) -> List[Tuple[str, int]]:
        """Return ``(key, value_char_len)`` pairs sorted by key."""
        data = self._load_raw()
        return sorted((k, len(v)) for k, v in data.items())

    def clear(self) -> int:
        """Clear all keys. Returns how many were removed."""
        data = self._load_raw()
        n = len(data)
        if n:
            self._save_raw({})
        return n

    def to_dict(self) -> Dict[str, Any]:
        data = self._load_raw()
        return {
            "keys": sorted(data.keys()),
            "count": len(data),
            "total_chars": self._total_chars(data),
        }
