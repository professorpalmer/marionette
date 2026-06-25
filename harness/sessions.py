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
    archived: bool = False


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
        return [{**s, "active": s["id"] == self._active, "archived": s.get("archived", False)} for s in self._sessions]

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

    def delete(self, sid: str) -> Optional[str]:
        self._sessions = [s for s in self._sessions if s["id"] != sid]
        if self._active == sid:
            if self._sessions:
                most_recent = max(self._sessions, key=lambda s: s.get("created", 0))
                self._active = most_recent["id"]
            else:
                self._active = None
        self._save()
        return self._active

    def archive(self, sid: str, archived: bool = True) -> None:
        for s in self._sessions:
            if s["id"] == sid:
                s["archived"] = archived
                break
        self._save()

    @property
    def active(self) -> Optional[str]:
        return self._active


def save_transcript(state_dir: str, session_id: str, messages: list) -> None:
    if not session_id:
        return
    # Sanitize session_id to prevent directory traversal
    safe_sid = "".join(c for c in session_id if c.isalnum() or c in ("-", "_"))
    if not safe_sid:
        return
    trans_dir = os.path.join(state_dir, "transcripts")
    os.makedirs(trans_dir, exist_ok=True)
    p = os.path.join(trans_dir, f"{safe_sid}.json")
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(messages, f, indent=2)
        os.replace(tmp, p)
    except Exception:
        pass


def load_transcript(state_dir: str, session_id: str) -> list:
    if not session_id:
        return []
    safe_sid = "".join(c for c in session_id if c.isalnum() or c in ("-", "_"))
    if not safe_sid:
        return []
    p = os.path.join(state_dir, "transcripts", f"{safe_sid}.json")
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []
