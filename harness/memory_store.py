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
from typing import Callable, List, Optional

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


class SealedMemoryStore(MemoryStore):
    """Memory that only sealed sessions see, sealed under the master key."""

    PURPOSE = "memory"

    def __init__(self, path: Optional[str] = None):
        from . import seal

        super().__init__(path or os.path.join(seal.seal_dir(), "memory.json"))

    def _load(self) -> List[dict]:
        # No silent empty list: a locked or damaged file must not be overwritten.
        from . import seal

        if not self.path.exists():
            return []
        val = seal.memory_codec().open_json(self.PURPOSE, self.path.read_bytes())
        return val if isinstance(val, list) else []

    def _save(self, entries: List[dict]) -> None:
        from . import seal
        from .session_files import write_bytes_atomic

        write_bytes_atomic(str(self.path), seal.memory_codec().seal_json(self.PURPOSE, entries))


class SessionMemory(MemoryStore):
    """Plain memory, plus sealed memory while the owning session is sealed.

    A sealed session reads both layers and writes new entries only to the
    sealed layer. It never writes text into the plain layer.
    """

    def __init__(
        self,
        plain: MemoryStore,
        session_id_fn: Callable[[], str],
        sealed: Optional[MemoryStore] = None,
    ):
        self._plain = plain
        self._sealed_store = sealed
        self._session_id_fn = session_id_fn
        self._lock = threading.Lock()

    def _sealed(self) -> Optional[MemoryStore]:
        from . import seal

        if not seal.is_sealed(self._session_id_fn() or ""):
            return None
        if self._sealed_store is None:
            self._sealed_store = SealedMemoryStore()
        return self._sealed_store

    # Raw snapshot and rollback (harness_refine) act on plain memory only:
    # refine is off in sealed sessions.
    def _load(self) -> List[dict]:
        return self._plain._load()

    def _save(self, entries: List[dict]) -> None:
        self._plain._save(entries)

    def render_block(self) -> str:
        # The system prompt block is plain memory. Sealed memory gets its own
        # section (ConversationalSession._bind_sealed_memory_block).
        return self._plain.render_block()

    def sealed_block(self) -> str:
        sealed = self._sealed()
        return sealed.render_block() if sealed is not None else ""

    def list(self) -> List[MemoryEntry]:
        sealed = self._sealed()
        return self._plain.list() + (sealed.list() if sealed is not None else [])

    def add(self, text: str, category: str = "general", source: str = "agent",
            origin: str = "", workspace: str = "") -> MemoryEntry:
        sealed = self._sealed()
        if sealed is None:
            return self._plain.add(text, category, source, origin, workspace)
        normalized = text.strip().lower()
        for entry in self._plain.list():
            if entry.text.strip().lower() == normalized:
                return entry
        return sealed.add(text, category, source, origin, workspace)

    def remove(self, entry_id: str) -> bool:
        sealed = self._sealed()
        if sealed is not None and sealed.remove(entry_id):
            return True
        return self._plain.remove(entry_id)

    def update(self, entry_id: str, text: str) -> bool:
        sealed = self._sealed()
        if sealed is None:
            return self._plain.update(entry_id, text)
        if sealed.update(entry_id, text):
            return True
        if any(entry.id == entry_id for entry in self._plain.list()):
            raise ValueError(
                "this memory is shared with plain sessions; an encrypted session "
                "cannot change its text (remove it and add a new one)"
            )
        return False

    def clear(self) -> int:
        sealed = self._sealed()
        return self._plain.clear() + (sealed.clear() if sealed is not None else 0)
