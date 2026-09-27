from __future__ import annotations

"""Memory store: durable, cross-session persistent facts and preferences.
"""

import hashlib
import json
import os
import time
import threading
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import List, Optional

MEMORY_PATH = Path(os.path.expanduser("~/.pmharness/memory.json"))
MEMORY_CHAR_LIMIT = 4000
MEMORY_ORIGINS = frozenset({"user", "agent", "tool", "compact"})


@dataclass
class MemoryEntry:
    text: str
    category: str = "general"
    created_at: float = 0.0
    source: str = ""
    id: str = ""
    origin: str = "agent"
    content_sha256: str = ""
    workspace: str = ""


def _content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_origin(origin: str, source: str) -> str:
    raw = (origin or "").strip().lower()
    if raw in MEMORY_ORIGINS:
        return raw
    src = (source or "").strip().lower()
    if src in MEMORY_ORIGINS:
        return src
    return "agent"


def _entry_from_mapping(raw: dict) -> MemoryEntry:
    allowed = {item.name for item in fields(MemoryEntry)}
    payload = {key: value for key, value in raw.items() if key in allowed}
    if "origin" not in payload:
        payload["origin"] = _parse_origin("", str(payload.get("source") or ""))
    else:
        payload["origin"] = _parse_origin(str(payload.get("origin") or ""), str(payload.get("source") or ""))
    entry = MemoryEntry(**payload)
    if entry.text and not entry.content_sha256:
        entry.content_sha256 = _content_digest(entry.text)
    return entry


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
            return [_entry_from_mapping(e) for e in self._load() if isinstance(e, dict)]

    def add(
        self,
        text: str,
        category: str = "general",
        source: str = "agent",
        origin: str = "",
        workspace: str = "",
    ) -> MemoryEntry:
        normalized_text = text.strip().lower()
        with self._lock:
            entries = self._load()
            for e in entries:
                if isinstance(e, dict) and e.get("text", "").strip().lower() == normalized_text:
                    return _entry_from_mapping(e)

            entry = MemoryEntry(
                text=text,
                category=category,
                created_at=time.time(),
                source=source,
                id=uuid.uuid4().hex,
                origin=_parse_origin(origin, source),
                content_sha256=_content_digest(text),
                workspace=(workspace or "").strip(),
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
        normalized = text.strip()
        if not normalized:
            # Whitespace-only text is a no-op: nothing to save, return False.
            return False
        with self._lock:
            entries = self._load()
            hit = False
            for e in entries:
                if e.get("id") == entry_id:
                    clipped = normalized[:MEMORY_CHAR_LIMIT]
                    e["text"] = clipped
                    e["content_sha256"] = _content_digest(clipped)
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
        header = "# Durable memory (persistent across sessions -- user facts and preferences)"
        full_block = "\n".join([header, *(f"- {entry.text}" for entry in entries)])
        if len(full_block) <= MEMORY_CHAR_LIMIT:
            return full_block

        def omission_marker(count: int) -> str:
            noun = "memory" if count == 1 else "memories"
            return (
                f"- {count} {noun} omitted from this prompt (still saved); "
                "use the memory tool with action=list to inspect all saved memories."
            )

        ordered = sorted(
            entries,
            key=lambda entry: (
                0 if entry.category == "preference" else 1,
                0 if entry.origin == "user" else 1,
                -entry.created_at,
            ),
        )
        selected: List[str] = []
        for entry in ordered:
            line = f"- {entry.text}"
            omitted = len(entries) - len(selected) - 1
            candidate = [header, *selected, line]
            if omitted:
                candidate.append(omission_marker(omitted))
            if len("\n".join(candidate)) <= MEMORY_CHAR_LIMIT:
                selected.append(line)

        omitted = len(entries) - len(selected)
        rendered = [header, *selected]
        if omitted:
            rendered.append(omission_marker(omitted))
        return "\n".join(rendered)

    def search(
        self,
        query: str,
        *,
        origin: str = "",
        workspace: str = "",
    ) -> List[MemoryEntry]:
        needle = (query or "").strip().casefold()
        origin_filter = (origin or "").strip().lower()
        workspace_filter = (workspace or "").strip()
        hits: List[MemoryEntry] = []
        for entry in self.list():
            if origin_filter and entry.origin != origin_filter:
                continue
            if workspace_filter and entry.workspace != workspace_filter:
                continue
            if needle and needle not in entry.text.casefold():
                continue
            hits.append(entry)
        return hits
