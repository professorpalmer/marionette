from __future__ import annotations

"""Sessions: lightweight named chat sessions persisted to a JSON sidecar so the
UI can list/create/switch them (the Cursor/Hermes sidebar pattern). Each session
has its own ConversationalSession transcript in memory; this module persists the
LIST + which is active. Transcript bodies live with the live session objects.

Workspace scoping (``session_visible_for_workspace``):
- New rows store ``workspace_root`` (the open repo cwd) at creation.
- ``/api/sessions`` lists only rows visible for the active workspace.
- Legacy rows with no stored root: infer from the first transcript display entry
  that carries a ``cwd``; if none, show in every workspace (never hide silently).
"""

import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Optional, Any

from .job_scoping import cwd_under_repo, _norm_path


@dataclass
class SessionMeta:
    id: str
    title: str
    created: float
    active: bool = False
    archived: bool = False
    repo: str = ""
    branch: str = ""
    workspace_root: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    estimated_cost_usd: float = 0.0


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
            self._prune_ephemeral_rows()

    def _prune_ephemeral_rows(self) -> None:
        """Drop non-active sessions rooted in temp dirs (worker worktrees,
        pilot self-test opens). They pollute the store, and worse, ``delete``
        used to pick one as the next active session -- yanking the whole
        workspace to a random temp dir."""
        kept = [
            s for s in self._sessions
            if s.get("id") == self._active
            or not _is_ephemeral_root(session_stored_root(s))
        ]
        if len(kept) != len(self._sessions):
            self._sessions = kept
            try:
                self._save()
            except Exception:
                pass

    def _save(self) -> None:
        with self._lock:
            # Atomic write (temp + os.replace) so a crash or concurrent reader never
            # sees a truncated session file -- matches memory_store/rule_store/keys.
            target_dir = os.path.dirname(self.path) or "."
            os.makedirs(target_dir, exist_ok=True)
            payload = {"sessions": self._sessions, "active": self._active}
            tmp_fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".sessions_")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="\n") as f:
                    json.dump(payload, f)
                os.replace(tmp_path, self.path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

    def list(self, workspace_root: str = "", state_dir: str = "") -> list[dict]:
        rows = [{
            **s,
            "active": s["id"] == self._active,
            "archived": s.get("archived", False),
            "repo": s.get("repo", ""),
            "branch": s.get("branch", ""),
            "workspace_root": session_stored_root(s),
            "input_tokens": int(s.get("input_tokens", 0) or 0),
            "output_tokens": int(s.get("output_tokens", 0) or 0),
            "cache_read_tokens": int(s.get("cache_read_tokens", 0) or 0),
            "estimated_cost_usd": float(s.get("estimated_cost_usd", 0.0) or 0.0),
        } for s in self._sessions]
        if not workspace_root:
            return rows
        return [
            row for row in rows
            if session_visible_for_workspace(row, workspace_root, state_dir)
        ]

    def accumulate_meters(
        self,
        sid: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        """Persist cumulative token/cost meters on a chat session."""
        if not sid:
            return
        tin = int(input_tokens or 0)
        tout = int(output_tokens or 0)
        tcached = int(cache_read_tokens or 0)
        cost = float(estimated_cost_usd or 0.0)
        if not (tin or tout or tcached or cost):
            return
        with self._lock:
            for s in self._sessions:
                if s["id"] != sid:
                    continue
                s["input_tokens"] = int(s.get("input_tokens", 0) or 0) + tin
                s["output_tokens"] = int(s.get("output_tokens", 0) or 0) + tout
                s["cache_read_tokens"] = int(s.get("cache_read_tokens", 0) or 0) + tcached
                s["estimated_cost_usd"] = round(
                    float(s.get("estimated_cost_usd", 0.0) or 0.0) + cost, 6
                )
                self._save()
                break

    def create(
        self,
        title: Optional[str] = None,
        repo: str = "",
        branch: str = "",
        workspace_root: str = "",
    ) -> dict:
        with self._lock:
            sid = uuid.uuid4().hex[:12]
            ws_root = (workspace_root or repo or "").strip()
            meta = asdict(SessionMeta(
                id=sid,
                title=title or "New session",
                created=time.time(),
                repo=repo,
                branch=branch,
                workspace_root=ws_root,
            ))
            self._sessions.append(meta)
            self._active = sid
            self._save()
            return {**meta, "active": True, "workspace_root": ws_root}

    def switch(self, sid: str) -> dict:
        with self._lock:
            if not any(s["id"] == sid for s in self._sessions):
                return {"ok": False, "error": "unknown session"}
            self._active = sid
            self._save()
            return {"ok": True, "active": sid}

    def delete(self, sid: str) -> Optional[str]:
        with self._lock:
            deleted_root = ""
            for s in self._sessions:
                if s["id"] == sid:
                    deleted_root = session_stored_root(s)
                    break
            self._sessions = [s for s in self._sessions if s["id"] != sid]
            if self._active == sid:
                self._active = self._pick_next_active(deleted_root)
            self._save()
            return self._active

    def rows(self) -> list[dict]:
        """Snapshot of the raw session rows (copies; safe to inspect freely)."""
        with self._lock:
            return [dict(s) for s in self._sessions]

    def remove_rows(self, sids: list) -> list[str]:
        """Delete several session rows in one pass (boot-migration path).

        If the active session is removed, promote a same-workspace sibling via
        ``_pick_next_active`` -- never a session from another workspace.
        Callers own transcript-file cleanup for the returned ids.
        """
        with self._lock:
            doomed = {sid for sid in sids if sid}
            removed = [s["id"] for s in self._sessions if s["id"] in doomed]
            if not removed:
                return []
            active_root = ""
            if self._active in doomed:
                for s in self._sessions:
                    if s["id"] == self._active:
                        active_root = session_stored_root(s)
                        break
            self._sessions = [s for s in self._sessions if s["id"] not in doomed]
            if self._active in doomed:
                self._active = self._pick_next_active(active_root)
            self._save()
            return removed

    def activate_newest_for_root(self, workspace_root: str) -> Optional[str]:
        """Point the active session at the newest one under ``workspace_root``.

        Boot same-workspace promotion: when the persisted active session would
        yank the workspace elsewhere (e.g. a stale app-checkout row), activate
        the newest session that actually belongs to the restored workspace --
        or nothing, never a session from a third workspace.
        """
        with self._lock:
            candidate = self._pick_next_active(workspace_root)
            if candidate != self._active:
                self._active = candidate
                self._save()
            return candidate

    def _pick_next_active(self, preferred_root: str) -> Optional[str]:
        """Most recent session in the same workspace as the one just deleted,
        or None. Never promotes a session from another workspace: doing so made
        the frontend auto-switch dirs (often to a leaked temp worktree), so
        closing the last session in a dir yanked the user somewhere else and
        the dir dropped out of the projects list. Staying put with no active
        session keeps the workspace and lets the user start fresh in place."""
        same_root = [
            s for s in self._sessions
            if _same_root(session_stored_root(s), preferred_root)
        ]
        if not same_root:
            return None
        return max(same_root, key=lambda s: s.get("created", 0))["id"]

    def clear_for_workspace(self, workspace_root: str, state_dir: str = "") -> tuple[list[str], Optional[str]]:
        """Drop session metadata rows for ``workspace_root`` only (not job store)."""
        with self._lock:
            deleted: list[str] = []
            kept: list[dict] = []
            for s in self._sessions:
                if session_visible_for_workspace(s, workspace_root, state_dir):
                    deleted.append(s["id"])
                else:
                    kept.append(s)
            self._sessions = kept
            if self._active in deleted:
                # Everything visible in this workspace is gone; do not promote
                # a session from another workspace (see _pick_next_active).
                self._active = self._pick_next_active(workspace_root)
            self._save()
            return deleted, self._active

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

    def relocate(
        self,
        sid: str,
        workspace_root: str,
        *,
        repo: str = "",
        branch: str = "",
        title: Optional[str] = None,
        make_active: bool = True,
    ) -> Optional[dict]:
        """Move a session into ``workspace_root`` without creating a new session.

        Preserves the same session id (and therefore the transcript file under
        ``state/transcripts/<sid>.json``). When ``make_active`` is True, the
        relocated session becomes the active view.
        """
        ws_root = (workspace_root or "").strip()
        if not sid or not ws_root:
            return None
        with self._lock:
            for s in self._sessions:
                if s["id"] != sid:
                    continue
                s["workspace_root"] = ws_root
                s["repo"] = (repo or ws_root).strip()
                if branch:
                    s["branch"] = branch
                if title is not None and str(title).strip():
                    s["title"] = str(title).strip()
                if make_active:
                    self._active = sid
                self._save()
                return {
                    **s,
                    "active": s["id"] == self._active,
                    "workspace_root": session_stored_root(s),
                    "input_tokens": int(s.get("input_tokens", 0) or 0),
                    "output_tokens": int(s.get("output_tokens", 0) or 0),
                    "cache_read_tokens": int(s.get("cache_read_tokens", 0) or 0),
                    "estimated_cost_usd": float(s.get("estimated_cost_usd", 0.0) or 0.0),
                }
            return None

    def list_bank(
        self,
        query: str = "",
        limit: int = 50,
        state_dir: str = "",
    ) -> list[dict]:
        """Chronological listing of ALL sessions (cross-workspace transcript bank).

        Optional ``query`` matches title, id, or workspace_root (case-insensitive).
        """
        rows = self.list(workspace_root="", state_dir=state_dir)
        rows.sort(key=lambda s: float(s.get("created", 0) or 0), reverse=True)
        q = (query or "").strip().lower()
        if q:
            filtered = []
            for row in rows:
                hay = " ".join([
                    str(row.get("title") or ""),
                    str(row.get("id") or ""),
                    str(row.get("workspace_root") or ""),
                    str(row.get("repo") or ""),
                ]).lower()
                if q in hay:
                    filtered.append(row)
            rows = filtered
        cap = max(1, min(int(limit or 50), 500))
        return rows[:cap]

    def migrate_empty_roots(self, workspace_root: str) -> list[str]:
        """Bind rootless / empty-root sessions to ``workspace_root`` (boot hygiene).

        Does not change the active session. Returns relocated session ids.
        """
        ws_root = (workspace_root or "").strip()
        if not ws_root:
            return []
        moved: list[str] = []
        with self._lock:
            for s in self._sessions:
                if session_stored_root(s):
                    continue
                s["workspace_root"] = ws_root
                if not (s.get("repo") or "").strip():
                    s["repo"] = ws_root
                moved.append(s["id"])
            if moved:
                self._save()
        return moved

    @property
    def active(self) -> Optional[str]:
        return self._active


def session_stored_root(session: dict) -> str:
    """Persisted workspace root on a session row (``workspace_root`` or legacy ``repo``)."""
    return (session.get("workspace_root") or session.get("repo") or "").strip()


def _same_root(a: str, b: str) -> bool:
    """Path-normalized equality; two rootless sessions also count as peers."""
    if not a and not b:
        return True
    if not a or not b:
        return False
    return _norm_path(a) == _norm_path(b)


def _is_ephemeral_root(root: str) -> bool:
    """True for roots under the OS temp dir (worker worktrees, test repos).

    Skipped under pytest so the suite's own tmp_path fixtures keep working.
    """
    if not root or "PYTEST_CURRENT_TEST" in os.environ:
        return False
    try:
        real = os.path.realpath(root)
        tmp = os.path.realpath(tempfile.gettempdir())
        return real.startswith(tmp) or "/var/folders/" in real
    except Exception:
        return False


def infer_legacy_session_root(session: dict, state_dir: str) -> str:
    """First ``cwd`` on the session transcript display stream, if any."""
    sid = session.get("id") or ""
    if not sid or not state_dir:
        return ""
    data = load_transcript(state_dir, sid)
    if not isinstance(data, dict):
        return ""
    for entry in data.get("display") or []:
        if not isinstance(entry, dict):
            continue
        cwd = (entry.get("cwd") or "").strip()
        if cwd:
            return cwd
    return ""


def session_visible_for_workspace(session: dict, workspace_root: str, state_dir: str = "") -> bool:
    """True when ``session`` belongs in the sidebar for the active workspace."""
    if not workspace_root:
        return True
    stored = session_stored_root(session)
    if stored:
        if _norm_path(stored) == _norm_path(workspace_root):
            return True
        return cwd_under_repo(stored, workspace_root) or cwd_under_repo(workspace_root, stored)
    inferred = infer_legacy_session_root(session, state_dir)
    if inferred:
        if _norm_path(inferred) == _norm_path(workspace_root):
            return True
        return cwd_under_repo(inferred, workspace_root)
    return True


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
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
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
