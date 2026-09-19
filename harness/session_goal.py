from __future__ import annotations

"""Persistent sticky session GOAL — distinct from Job.goal / Schedule.objective.

JSON under the session state_dir so the goal survives turns and compaction
(transcript summarization must not clear it). Supplemental context only; never
mutates the frozen system prompt.
"""

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

GOAL_STATUSES = ("active", "paused", "complete", "cleared")
GOAL_FILENAME = "session_goal.json"


@dataclass
class SessionGoal:
    text: str = ""
    status: str = "cleared"
    created_at: float = 0.0
    updated_at: float = 0.0
    token_count: int = 0
    elapsed_seconds: float = 0.0
    continuation_count: int = 0
    token_budget: Optional[int] = None
    output_token_cap: Optional[int] = None
    budget_exceeded: bool = False
    # Wall-clock anchor for elapsed_seconds while status is active.
    _active_since: float = field(default=0.0, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "token_count": int(self.token_count),
            "elapsed_seconds": float(self.elapsed_seconds),
            "continuation_count": int(self.continuation_count),
            "token_budget": self.token_budget,
            "output_token_cap": self.output_token_cap,
            "budget_exceeded": bool(self.budget_exceeded),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "SessionGoal":
        if not isinstance(data, dict):
            return cls()
        status = str(data.get("status") or "cleared").strip().lower()
        if status not in GOAL_STATUSES:
            status = "cleared"
        budget = data.get("token_budget")
        token_budget: Optional[int]
        if budget in ("", None):
            token_budget = None
        else:
            try:
                token_budget = int(budget)
            except (TypeError, ValueError):
                token_budget = None
        raw_cap = data.get("output_token_cap")
        output_token_cap: Optional[int]
        if raw_cap in ("", None):
            output_token_cap = None
        else:
            try:
                parsed_cap = int(raw_cap)
                output_token_cap = parsed_cap if parsed_cap > 0 else None
            except (TypeError, ValueError):
                output_token_cap = None
        return cls(
            text=str(data.get("text") or ""),
            status=status,
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            token_count=int(data.get("token_count") or 0),
            elapsed_seconds=float(data.get("elapsed_seconds") or 0.0),
            continuation_count=int(data.get("continuation_count") or 0),
            token_budget=token_budget,
            output_token_cap=output_token_cap,
            budget_exceeded=bool(data.get("budget_exceeded")),
            _active_since=float(data.get("_active_since") or 0.0),
        )

    def is_active(self) -> bool:
        return self.status == "active" and bool((self.text or "").strip())

    def set(
        self,
        text: str,
        *,
        token_budget: Optional[int] = None,
        output_token_cap: Optional[int] = None,
    ) -> "SessionGoal":
        now = time.time()
        cleaned = (text or "").strip()
        if not cleaned:
            return self.clear()
        self.text = cleaned
        self.status = "active"
        self.created_at = now
        self.token_count = 0
        self.continuation_count = 0
        self.elapsed_seconds = 0.0
        self.updated_at = now
        self._active_since = now
        self.budget_exceeded = False
        if token_budget is not None:
            try:
                self.token_budget = int(token_budget)
            except (TypeError, ValueError):
                pass
        if output_token_cap is not None:
            try:
                parsed_cap = int(output_token_cap)
                self.output_token_cap = parsed_cap if parsed_cap > 0 else None
            except (TypeError, ValueError):
                pass
        return self

    def pause(self) -> "SessionGoal":
        if self.status == "active":
            self._fold_elapsed()
            self.status = "paused"
            self.updated_at = time.time()
            self._active_since = 0.0
        return self

    def resume(self) -> "SessionGoal":
        if self.status == "paused" and (self.text or "").strip():
            self.status = "active"
            self.updated_at = time.time()
            self._active_since = time.time()
        return self

    def complete(self) -> "SessionGoal":
        if self.status in ("active", "paused"):
            self._fold_elapsed()
            self.status = "complete"
            self.updated_at = time.time()
            self._active_since = 0.0
        return self

    def clear(self) -> "SessionGoal":
        self._fold_elapsed()
        self.text = ""
        self.status = "cleared"
        self.updated_at = time.time()
        self._active_since = 0.0
        self.output_token_cap = None
        # Counters retained for audit until a new set() replaces the goal.
        return self

    def record_turn_usage(
        self,
        *,
        tokens: int = 0,
        elapsed_seconds: float = 0.0,
        continuation: bool = False,
    ) -> "SessionGoal":
        if self.status != "active":
            return self
        try:
            self.token_count = int(self.token_count) + max(0, int(tokens or 0))
        except (TypeError, ValueError):
            pass
        try:
            self.elapsed_seconds = float(self.elapsed_seconds) + max(
                0.0, float(elapsed_seconds or 0.0)
            )
        except (TypeError, ValueError):
            pass
        if continuation:
            self.continuation_count = int(self.continuation_count) + 1
        self.updated_at = time.time()
        if self.token_budget is not None:
            try:
                if int(self.token_count) >= int(self.token_budget):
                    self.pause()
                    self.budget_exceeded = True
            except (TypeError, ValueError):
                pass
        return self

    def continuation_prompt(self) -> str:
        """Cheap one-sentence reminder for the host to enqueue after a turn."""
        text = (self.text or "").strip()
        if not text:
            return ""
        return (
            "Continue working toward the session goal "
            "(one focused step; do not restart from scratch): " + text
        )

    def context_block(self) -> str:
        """Supplemental turn context (not part of the frozen system prompt)."""
        if not self.is_active():
            return ""
        lines = [
            "SESSION GOAL (sticky until complete/pause/clear — work toward this):",
            self.text.strip(),
        ]
        if self.token_budget is not None:
            lines.append(
                "Goal token usage: %d / %d"
                % (int(self.token_count), int(self.token_budget))
            )
        else:
            lines.append("Goal token usage: %d" % int(self.token_count))
        if self.output_token_cap is not None:
            lines.append("Output token cap: %d" % int(self.output_token_cap))
        return "\n".join(lines)

    def _fold_elapsed(self) -> None:
        if self.status == "active" and self._active_since:
            self.elapsed_seconds = float(self.elapsed_seconds) + max(
                0.0, time.time() - float(self._active_since)
            )
            self._active_since = 0.0


class SessionGoalStore:
    """Load/save SessionGoal JSON under a session state_dir."""

    def __init__(self, state_dir: str, *, session_id: str = "") -> None:
        self.state_dir = state_dir or ""
        self.session_id = session_id or ""
        filename = GOAL_FILENAME
        if self.session_id:
            key = hashlib.sha256(self.session_id.encode("utf-8")).hexdigest()
            filename = os.path.join("session_goals", key + ".json")
        self.path = os.path.join(self.state_dir, filename) if self.state_dir else ""

    @classmethod
    def migrate_legacy(cls, state_dir: str, sessions: Any) -> None:
        """Claim the legacy goal at boot, before active-view promotion or builds.

        The owner is stamped first so an interrupted copy can only retry for
        that same owner. Keep every legacy field and never replace scoped data.
        Callers must surface write errors instead of continuing an unclaimed boot.
        """
        if not state_dir:
            return
        legacy_path = os.path.join(state_dir, GOAL_FILENAME)
        try:
            with open(legacy_path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (FileNotFoundError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        owner = payload.get("session_id")
        if not owner:
            owner = sessions.active
            if not isinstance(owner, str) or not owner.strip():
                return
            if sum(row.get("id") == owner for row in sessions.rows()) != 1:
                return
            payload["session_id"] = owner
            cls._write_payload(legacy_path, payload)
        if not isinstance(owner, str):
            return
        target = cls(state_dir, session_id=owner)
        cls._write_payload(target.path, payload, replace=False)

    @staticmethod
    def _write_payload(path: str, payload: dict, *, replace: bool = True) -> None:
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".session-goal-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            if replace:
                os.replace(temporary, path)
            else:
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def load(self) -> SessionGoal:
        if not self.path:
            return SessionGoal()
        path = self.path
        migrate = self.session_id and not os.path.isfile(path)
        if migrate:
            path = os.path.join(self.state_dir, GOAL_FILENAME)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return SessionGoal()
            # An old unscoped file has no knowable owner. Preserve it for
            # unbound callers; migration requires an explicit session_id.
            owner = data.get("session_id") or ""
            if (migrate and owner != self.session_id) or (not self.session_id and owner):
                return SessionGoal()
            goal = SessionGoal.from_dict(data)
            if migrate:
                self._write_payload(self.path, data, replace=False)
            return goal
        except Exception:
            return SessionGoal()

    def save(self, goal: SessionGoal) -> None:
        if not self.path or not self.state_dir:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            payload = goal.to_dict()
            if self.session_id:
                payload["session_id"] = self.session_id
            # Persist active_since so elapsed can resume across process restarts.
            payload["_active_since"] = float(getattr(goal, "_active_since", 0.0) or 0.0)
            self._write_payload(self.path, payload)
        except Exception:
            pass
