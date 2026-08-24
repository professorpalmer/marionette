from __future__ import annotations

"""Persistent sticky session GOAL — distinct from Job.goal / Schedule.objective.

JSON under the session state_dir so the goal survives turns and compaction
(transcript summarization must not clear it). Supplemental context only; never
mutates the frozen system prompt.
"""

import json
import os
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
        return cls(
            text=str(data.get("text") or ""),
            status=status,
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            token_count=int(data.get("token_count") or 0),
            elapsed_seconds=float(data.get("elapsed_seconds") or 0.0),
            continuation_count=int(data.get("continuation_count") or 0),
            token_budget=token_budget,
            budget_exceeded=bool(data.get("budget_exceeded")),
            _active_since=float(data.get("_active_since") or 0.0),
        )

    def is_active(self) -> bool:
        return self.status == "active" and bool((self.text or "").strip())

    def set(self, text: str, *, token_budget: Optional[int] = None) -> "SessionGoal":
        now = time.time()
        cleaned = (text or "").strip()
        if not cleaned:
            return self.clear()
        self.text = cleaned
        self.status = "active"
        if not self.created_at:
            self.created_at = now
        self.updated_at = now
        self._active_since = now
        self.budget_exceeded = False
        if token_budget is not None:
            try:
                self.token_budget = int(token_budget)
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
        return "\n".join(lines)

    def _fold_elapsed(self) -> None:
        if self.status == "active" and self._active_since:
            self.elapsed_seconds = float(self.elapsed_seconds) + max(
                0.0, time.time() - float(self._active_since)
            )
            self._active_since = 0.0


class SessionGoalStore:
    """Load/save SessionGoal JSON under a session state_dir."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir or ""
        self.path = (
            os.path.join(self.state_dir, GOAL_FILENAME) if self.state_dir else ""
        )

    def load(self) -> SessionGoal:
        if not self.path or not os.path.isfile(self.path):
            return SessionGoal()
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return SessionGoal.from_dict(data if isinstance(data, dict) else None)
        except Exception:
            return SessionGoal()

    def save(self, goal: SessionGoal) -> None:
        if not self.path or not self.state_dir:
            return
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            payload = goal.to_dict()
            # Persist active_since so elapsed can resume across process restarts.
            payload["_active_since"] = float(getattr(goal, "_active_since", 0.0) or 0.0)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(payload, indent=2, ensure_ascii=False))
            os.replace(tmp, self.path)
        except Exception:
            pass
