from __future__ import annotations

"""Session scratch bindings — durable L1 key/value, not agent memory.

JSON under the session state_dir so values survive turns and compaction
(transcript summarization must not clear them). Ephemeral working notes for
the current session only; distinct from durable cross-session ``memory``.

Corrupt on-disk JSON fails closed: reads/writes raise instead of pretending
the store is empty (which would let the next save clobber recoverable data).
``clear()`` quarantines a corrupt file then resets.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

SCRATCH_FILENAME = "session_scratch.json"

MAX_SCRATCH_KEYS = 64
MAX_SCRATCH_VALUE_CHARS = 8_000
MAX_SCRATCH_TOTAL_CHARS = 32_000


class ScratchStoreError(ValueError):
    """Raised when a scratch write would exceed size/key caps or IO fails."""


class ScratchStoreCorrupt(ScratchStoreError):
    """On-disk scratch JSON exists but cannot be loaded safely."""


class SessionScratchStore:
    """Load/save session scratch JSON under a session state_dir."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir or ""
        self.path = (
            os.path.join(self.state_dir, SCRATCH_FILENAME) if self.state_dir else ""
        )
        self.last_quarantine_path: str = ""

    def _load_raw(self) -> Dict[str, str]:
        if not self.path or not os.path.isfile(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            raise ScratchStoreCorrupt(
                f"scratch store unreadable at {self.path}: {exc}"
            ) from exc
        if not raw.strip():
            raise ScratchStoreCorrupt(
                f"scratch store is empty/corrupt at {self.path}"
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ScratchStoreCorrupt(
                f"scratch store JSON corrupt at {self.path}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise ScratchStoreCorrupt(
                f"scratch store must be a JSON object at {self.path} "
                f"(got {type(data).__name__})"
            )
        out: Dict[str, str] = {}
        for key, value in data.items():
            k = str(key or "").strip()
            if not k:
                continue
            out[k] = "" if value is None else str(value)
        return out

    def _quarantine_corrupt(self) -> str:
        """Move a bad store aside. Returns quarantine path (or "")."""
        self.last_quarantine_path = ""
        if not self.path or not os.path.isfile(self.path):
            return ""
        stamp = int(time.time())
        dest = f"{self.path}.corrupt.{stamp}"
        try:
            os.replace(self.path, dest)
            self.last_quarantine_path = dest
            return dest
        except OSError:
            return ""

    def _save_raw(self, data: Dict[str, str]) -> None:
        if not self.path or not self.state_dir:
            raise ScratchStoreError("scratch store has no state_dir")
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(data, indent=2, ensure_ascii=False))
            os.replace(tmp, self.path)
        except OSError as exc:
            raise ScratchStoreError(
                f"failed to persist scratch store at {self.path}: {exc}"
            ) from exc

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
        """Clear all keys. Returns how many were removed.

        If the on-disk file is corrupt, quarantine it and reset to empty
        (recovery path — the only write allowed against a corrupt store).
        """
        try:
            data = self._load_raw()
        except ScratchStoreCorrupt as exc:
            quarantined = self._quarantine_corrupt()
            if not quarantined and self.path and os.path.isfile(self.path):
                raise ScratchStoreError(
                    f"scratch store corrupt and could not quarantine "
                    f"({self.path}); refusing to overwrite: {exc}"
                ) from exc
            self._save_raw({})
            return 0
        self.last_quarantine_path = ""
        n = len(data)
        if n or (self.path and os.path.isfile(self.path)):
            self._save_raw({})
        return n

    def to_dict(self) -> Dict[str, Any]:
        data = self._load_raw()
        return {
            "keys": sorted(data.keys()),
            "count": len(data),
            "total_chars": self._total_chars(data),
        }
