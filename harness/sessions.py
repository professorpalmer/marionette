from __future__ import annotations

"""Sessions: lightweight named chat sessions persisted to a JSON sidecar so the
UI can list/create/switch them (the Cursor/Hermes sidebar pattern). Each session
has its own ConversationalSession transcript in memory; this module persists the
LIST + which is active. Transcript bodies live with the live session objects.
"""

import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Optional, Any


@dataclass
class SessionMeta:
    id: str
    title: str
    created: float
    active: bool = False
    archived: bool = False
    repo: str = ""
    branch: str = ""


class SessionStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._sessions: list[dict] = []
        self._active: Optional[str] = None
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                self._sessions = data.get("sessions", [])
                self._active = data.get("active")
            except Exception:
                self._sessions, self._active = [], None

    def _save(self) -> None:
        with self._lock:
            # Atomic write (temp + os.replace) so a crash or concurrent reader never
            # sees a truncated session file -- matches memory_store/rule_store/keys.
            target_dir = os.path.dirname(self.path) or "."
            os.makedirs(target_dir, exist_ok=True)
            payload = {"sessions": self._sessions, "active": self._active}
            tmp_fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".sessions_")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="") as f:
                    json.dump(payload, f)
                os.replace(tmp_path, self.path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

    def list(self) -> list[dict]:
        return [{
            **s,
            "active": s["id"] == self._active,
            "archived": s.get("archived", False),
            "repo": s.get("repo", ""),
            "branch": s.get("branch", ""),
        } for s in self._sessions]

    def create(self, title: Optional[str] = None, repo: str = "", branch: str = "") -> dict:
        with self._lock:
            sid = uuid.uuid4().hex[:12]
            meta = asdict(SessionMeta(id=sid, title=title or "New session", created=time.time(), repo=repo, branch=branch))
            self._sessions.append(meta)
            self._active = sid
            self._save()
            return {**meta, "active": True}

    def switch(self, sid: str) -> dict:
        with self._lock:
            if not any(s["id"] == sid for s in self._sessions):
                return {"ok": False, "error": "unknown session"}
            self._active = sid
            self._save()
            return {"ok": True, "active": sid}

    def delete(self, sid: str) -> Optional[str]:
        with self._lock:
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
        with self._lock:
            for s in self._sessions:
                if s["id"] == sid:
                    s["archived"] = archived
                    break
            self._save()

    def set_title_if_default(self, sid: str, title: str) -> None:
        with self._lock:
            for s in self._sessions:
                if s["id"] == sid:
                    current = s.get("title", "")
                    if not current or current == "New session":
                        s["title"] = title
                        self._save()
                    break

    def rename(self, sid: str, title: str) -> bool:
        with self._lock:
            for s in self._sessions:
                if s["id"] == sid:
                    s["title"] = title
                    self._save()
                    return True
            return False

    def stamp_session(self, sid: str, repo: str, branch: str) -> None:
        with self._lock:
            for s in self._sessions:
                if s["id"] == sid:
                    s["repo"] = repo
                    s["branch"] = branch
                    self._save()
                    break

    @property
    def active(self) -> Optional[str]:
        return self._active


def derive_title(prompt: str) -> str:
    if not prompt:
        return "New session"
    import re
    lines = prompt.splitlines()
    first_line = ""
    for line in lines:
        cleaned = re.sub(r'```[a-zA-Z0-9_\-+]*', '', line)
        cleaned = re.sub(r'`', '', cleaned)
        cleaned = re.sub(r'[*_~#\-+>]', '', cleaned)
        cleaned = ' '.join(cleaned.split())
        if cleaned:
            first_line = cleaned
            break
    if not first_line:
        return "New session"
    words = first_line.split()
    truncated_words = []
    current_len = 0
    for w in words:
        if len(truncated_words) >= 8:
            break
        added_len = len(w) + (1 if truncated_words else 0)
        if current_len + added_len > 48:
            if not truncated_words:
                truncated_words.append(w[:48])
            break
        truncated_words.append(w)
        current_len += added_len
    title = ' '.join(truncated_words)
    title = title.rstrip('.,;:?!- ')
    if title:
        title = title[0].upper() + title[1:]
    return title or "New session"


def save_transcript(state_dir: str, session_id: str, messages: Any) -> None:
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


def load_transcript(state_dir: str, session_id: str) -> Any:
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
