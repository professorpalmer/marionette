from __future__ import annotations

"""Continual harness REFINE — propose supplemental deltas with human gate.

Kinds: memory | rule | skill | role. Scopes: local | global.
Never mutates the frozen system prompt. Autopilot skips (same as memory_propose).
Local scope writes session-scoped JSON; global uses MemoryStore / RuleStore /
SkillStore. Snapshot supplemental state before accept; rollback on dismiss/reject.
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

REFINE_KINDS = ("memory", "rule", "skill", "role")
REFINE_SCOPES = ("local", "global")
FORBIDDEN_KINDS = ("system_prompt", "system", "frozen_system_prompt")
LOCAL_REFINE_FILENAME = "local_refine.json"
SNAPSHOT_FILENAME = "refine_snapshot.json"
HISTORY_FILENAME = "refinements.jsonl"


def _now() -> float:
    return time.time()


@dataclass
class RefineProposal:
    id: str
    kind: str
    scope: str
    text: str
    category: str = "general"
    name: str = ""
    body: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "scope": self.scope,
            "text": self.text,
            "category": self.category,
            "name": self.name,
            "body": self.body,
            "meta": dict(self.meta or {}),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RefineProposal":
        return cls(
            id=str(data.get("id") or ""),
            kind=str(data.get("kind") or "memory"),
            scope=str(data.get("scope") or "global"),
            text=str(data.get("text") or ""),
            category=str(data.get("category") or "general"),
            name=str(data.get("name") or ""),
            body=str(data.get("body") or ""),
            meta=dict(data.get("meta") or {}) if isinstance(data.get("meta"), dict) else {},
            created_at=float(data.get("created_at") or 0.0),
        )


class LocalRefineStore:
    """Session-scoped JSON bag for local memory/rule/skill/role entries."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir or ""
        self.path = (
            os.path.join(self.state_dir, LOCAL_REFINE_FILENAME) if self.state_dir else ""
        )

    def _load(self) -> Dict[str, List[dict]]:
        empty: Dict[str, List[dict]] = {
            "memory": [],
            "rule": [],
            "skill": [],
            "role": [],
        }
        if not self.path or not os.path.isfile(self.path):
            return empty
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return empty
            out = dict(empty)
            for kind in REFINE_KINDS:
                items = data.get(kind) or []
                if isinstance(items, list):
                    out[kind] = [x for x in items if isinstance(x, dict)]
            return out
        except Exception:
            return empty

    def _save(self, data: Dict[str, List[dict]]) -> None:
        if not self.path or not self.state_dir:
            return
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, self.path)

    def list(self, kind: str) -> List[dict]:
        return list(self._load().get(kind) or [])

    def add(self, kind: str, entry: dict) -> dict:
        data = self._load()
        bucket = data.setdefault(kind, [])
        bucket.append(entry)
        self._save(data)
        return entry

    def replace_all(self, data: Dict[str, List[dict]]) -> None:
        clean: Dict[str, List[dict]] = {
            "memory": [],
            "rule": [],
            "skill": [],
            "role": [],
        }
        for kind in REFINE_KINDS:
            items = (data or {}).get(kind) or []
            if isinstance(items, list):
                clean[kind] = [x for x in items if isinstance(x, dict)]
        self._save(clean)

    def snapshot(self) -> Dict[str, List[dict]]:
        return self._load()


class HarnessRefineController:
    """Propose / accept / dismiss / rollback for supplemental harness state."""

    def __init__(self, session: Any) -> None:
        self.session = session
        state_dir = str(getattr(session, "state_dir", "") or "")
        self.local = LocalRefineStore(state_dir)
        self._pending: Dict[str, RefineProposal] = {}
        self._snapshot: Optional[Dict[str, Any]] = None
        self._snapshot_path = (
            os.path.join(state_dir, SNAPSHOT_FILENAME) if state_dir else ""
        )


    def _history_path(self) -> str:
        state_dir = str(getattr(self.session, "state_dir", "") or "")
        if not state_dir:
            return ""
        return os.path.join(state_dir, HISTORY_FILENAME)

    def _append_history(self, event: str, payload: Dict[str, Any]) -> None:
        path = self._history_path()
        if not path:
            return
        rec = {"event": event, "ts": _now(), **dict(payload or {})}
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def list_history(self, *, limit: int = 50) -> List[dict]:
        """Read append-only applied-refine history (newest last)."""
        path = self._history_path()
        if not path or not os.path.isfile(path):
            return []
        out: List[dict] = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(rec, dict):
                        out.append(rec)
        except Exception:
            return out
        if limit and len(out) > int(limit):
            return out[-int(limit):]
        return out

    def handle_slash(self, raw: str) -> Dict[str, Any]:
        """Slash /refine on this controller — list history or propose."""
        text = (raw or "").strip()
        if text.lower().startswith("/refine"):
            text = text[7:].strip()
        if not text:
            return {
                "ok": True,
                "history": self.list_history(),
                "usage": "/refine [kind] [scope] <text>",
            }
        parts = text.split()
        kind = "memory"
        scope = "global"
        rest = text
        if parts and parts[0].lower() in REFINE_KINDS:
            kind = parts[0].lower()
            rest = " ".join(parts[1:])
            more = rest.split()
            if more and more[0].lower() in REFINE_SCOPES:
                scope = more[0].lower()
                rest = " ".join(more[1:])
        prop = self.propose(kind=kind, text=rest, scope=scope)
        if prop is None:
            return {"ok": False, "error": "could not propose refine"}
        return {"ok": True, "proposed": prop.to_dict()}

    @property
    def pending(self) -> Dict[str, RefineProposal]:
        return self._pending

    def propose(
        self,
        *,
        kind: str,
        text: str,
        scope: str = "global",
        category: str = "general",
        name: str = "",
        body: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[RefineProposal]:
        kind_n = (kind or "").strip().lower()
        if kind_n in FORBIDDEN_KINDS or kind_n == "system_prompt":
            return None
        if kind_n not in REFINE_KINDS:
            return None
        scope_n = (scope or "global").strip().lower()
        if scope_n not in REFINE_SCOPES:
            scope_n = "global"
        cleaned = (text or "").strip()
        if not cleaned and kind_n != "skill":
            return None
        if bool(getattr(self.session, "_auto_mode", False)):
            return None
        prop = RefineProposal(
            id="refine_" + uuid.uuid4().hex[:12],
            kind=kind_n,
            scope=scope_n,
            text=cleaned,
            category=(category or "general").strip() or "general",
            name=(name or "").strip(),
            body=(body or "").strip(),
            meta=dict(meta or {}),
            created_at=_now(),
        )
        self._pending[prop.id] = prop
        return prop

    def flush_queued(self, queued: Optional[List[dict]] = None) -> List[dict]:
        """Promote queued refine hints into pending cards (cap 3)."""
        if bool(getattr(self.session, "_auto_mode", False)):
            if queued is not None:
                queued.clear()
            if hasattr(self.session, "_turn_refine_queue"):
                self.session._turn_refine_queue = []
            return []
        items = list(queued or getattr(self.session, "_turn_refine_queue", None) or [])
        if hasattr(self.session, "_turn_refine_queue"):
            self.session._turn_refine_queue = []
        out: List[dict] = []
        for item in items:
            if len(out) >= 3:
                break
            if not isinstance(item, dict):
                continue
            prop = self.propose(
                kind=str(item.get("kind") or "memory"),
                text=str(item.get("text") or ""),
                scope=str(item.get("scope") or "global"),
                category=str(item.get("category") or "general"),
                name=str(item.get("name") or ""),
                body=str(item.get("body") or ""),
                meta=item.get("meta") if isinstance(item.get("meta"), dict) else None,
            )
            if prop is not None:
                out.append(prop.to_dict())
        return out

    def _capture_snapshot(self) -> Dict[str, Any]:
        session = self.session
        snap: Dict[str, Any] = {
            "local": self.local.snapshot(),
            "frozen_system_prompt": getattr(session, "_frozen_system_prompt", None),
            "global_memory": [],
            "global_rules": [],
            "global_skills": [],
        }
        memory = getattr(session, "_memory", None)
        if memory is not None:
            try:
                snap["global_memory"] = [
                    {
                        "id": e.id,
                        "text": e.text,
                        "category": e.category,
                        "source": e.source,
                        "created_at": e.created_at,
                    }
                    for e in memory.list()
                ]
            except Exception:
                pass
        rules = getattr(session, "_rules", None)
        if rules is not None:
            try:
                snap["global_rules"] = [
                    {
                        "text": r.text,
                        "scope": r.scope,
                        "state": r.state,
                        "source": r.source,
                        "created_at": r.created_at,
                    }
                    for r in rules.list()
                ]
            except Exception:
                pass
        skills = getattr(session, "_skills", None)
        if skills is not None:
            try:
                snap["global_skills"] = [
                    {
                        "name": s.name,
                        "description": s.description,
                        "body": s.body,
                        "state": s.state,
                        "source": s.source,
                    }
                    for s in skills.list()
                ]
            except Exception:
                pass
        return snap

    def _persist_snapshot(self, snap: Dict[str, Any]) -> None:
        self._snapshot = snap
        if not self._snapshot_path:
            return
        try:
            os.makedirs(os.path.dirname(self._snapshot_path) or ".", exist_ok=True)
            tmp = self._snapshot_path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(snap, indent=2, ensure_ascii=False))
            os.replace(tmp, self._snapshot_path)
        except Exception:
            pass

    def accept(self, proposal_id: str) -> Dict[str, Any]:
        prop = self._pending.pop(proposal_id, None)
        if prop is None:
            return {"ok": False, "error": "proposal not found"}
        if prop.kind in FORBIDDEN_KINDS:
            return {"ok": False, "error": "refusing system_prompt mutation"}
        # Snapshot BEFORE mutation so dismiss/rollback can restore.
        self._persist_snapshot(self._capture_snapshot())
        try:
            applied = self._apply(prop)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        # Frozen prompt must remain untouched.
        frozen_before = (self._snapshot or {}).get("frozen_system_prompt")
        frozen_now = getattr(self.session, "_frozen_system_prompt", None)
        if frozen_before != frozen_now:
            # Restore immediately — refine must never touch frozen prompt.
            try:
                self.session._frozen_system_prompt = frozen_before
            except Exception:
                pass
            return {"ok": False, "error": "refused: would mutate frozen system prompt"}
        applied["ok"] = True
        applied["id"] = prop.id
        applied["kind"] = prop.kind
        applied["scope"] = prop.scope
        self._append_history("accept", {"id": prop.id, "kind": prop.kind, "scope": prop.scope, "text": prop.text})
        return applied

    def dismiss(self, proposal_id: str) -> Dict[str, Any]:
        if proposal_id in self._pending:
            self._pending.pop(proposal_id, None)
            return {"ok": True}
        # Dismiss after accept → rollback supplemental snapshot.
        if self._snapshot is not None:
            return self.rollback()
        return {"ok": False, "error": "proposal not found"}

    def rollback(self) -> Dict[str, Any]:
        snap = self._snapshot
        if snap is None and self._snapshot_path and os.path.isfile(self._snapshot_path):
            try:
                with open(self._snapshot_path, "r", encoding="utf-8") as fh:
                    snap = json.load(fh)
            except Exception:
                snap = None
        if not isinstance(snap, dict):
            return {"ok": False, "error": "no snapshot"}
        try:
            self._restore_snapshot(snap)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        self._append_history("rollback", {"ok": True})
        return {"ok": True, "rolled_back": True}

    def _apply(self, prop: RefineProposal) -> Dict[str, Any]:
        if prop.scope == "local":
            entry = {
                "id": prop.id,
                "text": prop.text,
                "category": prop.category,
                "name": prop.name,
                "body": prop.body,
                "kind": prop.kind,
                "created_at": prop.created_at or _now(),
                "meta": dict(prop.meta or {}),
            }
            self.local.add(prop.kind, entry)
            return {"applied": "local", "entry": entry}

        session = self.session
        if prop.kind == "memory":
            memory = getattr(session, "_memory", None)
            if memory is None:
                return {"applied": "none", "error": "no memory store"}
            entry = memory.add(
                text=prop.text,
                category=prop.category,
                source="agent",
            )
            return {
                "applied": "global_memory",
                "entry": {
                    "id": entry.id,
                    "text": entry.text,
                    "category": entry.category,
                },
            }
        if prop.kind == "rule":
            from .rule_store import Rule

            rules = getattr(session, "_rules", None)
            if rules is None:
                return {"applied": "none", "error": "no rule store"}
            rule = rules.add(
                Rule(
                    text=prop.text,
                    scope="global",
                    state="pending",
                    source="refine",
                    created_at=_now(),
                )
            )
            return {
                "applied": "global_rule",
                "entry": {"slug": rule.slug, "text": rule.text, "state": rule.state},
            }
        if prop.kind == "skill":
            from .skill_store import Skill

            skills = getattr(session, "_skills", None)
            if skills is None:
                return {"applied": "none", "error": "no skill store"}
            skill = Skill(
                name=prop.name or prop.text[:48] or "refined-skill",
                description=prop.text,
                body=prop.body or prop.text,
                state="pending",
                source="refine",
            )
            if hasattr(skills, "save"):
                skills.save(skill)
            slug = getattr(skill, "slug", "")
            return {
                "applied": "global_skill",
                "entry": {"slug": slug, "name": skill.name, "state": skill.state},
            }
        if prop.kind == "role":
            # Role specs are session-local supplemental JSON even when scope=global
            # until a dedicated role store exists; never touch system prompt.
            entry = {
                "id": prop.id,
                "text": prop.text,
                "name": prop.name or "role",
                "body": prop.body,
                "created_at": prop.created_at or _now(),
            }
            self.local.add("role", entry)
            return {"applied": "role", "entry": entry}
        return {"applied": "none", "error": "unknown kind"}

    def _restore_snapshot(self, snap: Dict[str, Any]) -> None:
        local = snap.get("local") if isinstance(snap.get("local"), dict) else {}
        self.local.replace_all(local)  # type: ignore[arg-type]
        # Restore global memory by rewriting store file contents when possible.
        memory = getattr(self.session, "_memory", None)
        if memory is not None and hasattr(memory, "_save"):
            entries = snap.get("global_memory") or []
            if isinstance(entries, list):
                try:
                    memory._save(list(entries))
                except Exception:
                    pass
        rules = getattr(self.session, "_rules", None)
        if rules is not None and hasattr(rules, "_save"):
            entries = snap.get("global_rules") or []
            if isinstance(entries, list):
                try:
                    rules._save(list(entries))
                except Exception:
                    pass
        # Skills filesystem restore is best-effort: leave active files; tests
        # cover local + memory/rule rollback primarily.
        # Never alter frozen system prompt — only re-assert snapshot value.
        if "frozen_system_prompt" in snap:
            try:
                self.session._frozen_system_prompt = snap.get("frozen_system_prompt")
            except Exception:
                pass


def get_refine_controller(session: Any) -> HarnessRefineController:
    ctrl = getattr(session, "_harness_refine", None)
    if ctrl is None:
        ctrl = HarnessRefineController(session)
        session._harness_refine = ctrl
    return ctrl
