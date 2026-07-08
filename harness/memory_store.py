from __future__ import annotations

"""Memory store: durable, cross-session persistent facts and preferences.
"""

import json
import os
import time
import threading
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

MEMORY_PATH = Path(os.path.expanduser("~/.pmharness/memory.json"))
MEMORY_CHAR_LIMIT = 4000


@dataclass
class MemoryEntry:
    text: str
    category: str = "general"
    created_at: float = 0.0
    source: str = ""
    id: str = ""


class MemoryStore:
    def __init__(self, path: Optional[str] = None):
        self.path = Path(path) if path else MEMORY_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _load(self) -> List[dict]:
        if not self.path.exists():
            return []
        try:
            val = json.loads(self.path.read_text())
            if isinstance(val, list):
                return val
            return []
        except Exception:
            return []

    def _save(self, entries: List[dict]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        # Python 3.9 floor: Path.write_text has no newline=; open() enforces UTF-8 + LF.
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(entries, indent=2))
        os.replace(tmp, self.path)

    def list(self) -> List[MemoryEntry]:
        with self._lock:
            return [MemoryEntry(**e) for e in self._load()]

    def add(self, text: str, category: str = "general", source: str = "agent") -> MemoryEntry:
        normalized_text = text.strip().lower()
        with self._lock:
            entries = self._load()
            for e in entries:
                if e.get("text", "").strip().lower() == normalized_text:
                    return MemoryEntry(**e)

            entry = MemoryEntry(
                text=text,
                category=category,
                created_at=time.time(),
                source=source,
                id=uuid.uuid4().hex
            )
            entries.append(asdict(entry))
            self._save(entries)
            return entry

    def remove(self, entry_id: str) -> bool:
        with self._lock:
            entries = self._load()
            orig_len = len(entries)
            entries = [e for e in entries if e.get("id") != entry_id]
            if len(entries) < orig_len:
                self._save(entries)
                return True
            return False

    def update(self, entry_id: str, text: str) -> bool:
        with self._lock:
            entries = self._load()
            hit = False
            for e in entries:
                if e.get("id") == entry_id:
                    e["text"] = text
                    hit = True
            if hit:
                self._save(entries)
                return True
            return False

    def clear(self) -> int:
        with self._lock:
            entries = self._load()
            count = len(entries)
            self._save([])
            return count

    def total_chars(self) -> int:
        return sum(len(e.text) for e in self.list())

    def over_budget(self) -> bool:
        return self.total_chars() > MEMORY_CHAR_LIMIT

    def render_block(self) -> str:
        entries = self.list()
        if not entries:
            return ""
        items = "\n".join(f"- {e.text}" for e in entries)
        return f"# Durable memory (persistent across sessions -- user facts and preferences)\n{items}"
