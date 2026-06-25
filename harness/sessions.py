from __future__ import annotations

"""Sessions: lightweight named chat sessions persisted to a JSON sidecar so the
UI can list/create/switch them (the Cursor/Hermes sidebar pattern). Each session
has its own ConversationalSession transcript in memory; this module persists the
LIST + which is active. Transcript bodies live with the live session objects.
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class SessionMeta:
    id: str
    title: str
    created: float
    active: bool = False


class SessionStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._sessions: list[dict] = []
        self._active: Optional[str] = None
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                data = json.load(open(self.path))
                self._sessions = data.get("sessions", [])
                self._active = data.get("active")
            except Exception:
                self._sessions, self._active = [], None

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        json.dump({"sessions": self._sessions, "active": self._active}, open(self.path, "w"))

    def list(self) -> list[dict]:
        return [{**s, "active": s["id"] == self._active} for s in self._sessions]

    def create(self, title: Optional[str] = None) -> dict:
        sid = uuid.uuid4().hex[:12]
        meta = asdict(SessionMeta(id=sid, title=title or "New session", created=time.time()))
        self._sessions.append(meta)
        self._active = sid
        self._save()
        return {**meta, "active": True}

    def switch(self, sid: str) -> dict:
        if not any(s["id"] == sid for s in self._sessions):
            return {"ok": False, "error": "unknown session"}
        self._active = sid
        self._save()
        return {"ok": True, "active": sid}

    @property
    def active(self) -> Optional[str]:
        return self._active
