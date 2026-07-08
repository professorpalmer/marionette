from __future__ import annotations

"""Rules store: standing project CONVENTIONS the pilot must always honor
(distinct from skills, which are task procedures). A rule is a terse constraint
-- "always X", "never Y" -- like the AGENTS.md "no emojis" rule.

Rules load into the pilot context as an always-on block (not task-triggered like
skills). Same human-in-loop gate: auto-extracted rules are PENDING until
approved. JSON-backed (rules are short; no need for markdown files).
"""

import json
import os
import re
import time
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

from .skill_store import _slug as _filename_slug

RULES_PATH = Path(os.path.expanduser("~/.pmharness/rules.json"))
STATES = ("pending", "active", "archived")


def _slug(text: str) -> str:
    return _filename_slug(text, fallback="rule")


@dataclass
class Rule:
    text: str                 # the constraint, terse and imperative
    scope: str = "global"     # global | repo path | language | etc.
    state: str = "pending"
    source: str = ""
    created_at: float = 0.0

    @property
    def slug(self) -> str:
        return _slug(self.text)


class RuleStore:
    def __init__(self, path: Optional[str] = None):
        self.path = Path(path) if path else RULES_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _load(self) -> List[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or []
        except Exception:
            return []

    def _save(self, rules: List[dict]) -> None:
        # atomic: temp + os.replace so a concurrent reader never sees a truncated
        # file (which _load would swallow and return [], dropping all rules).
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rules, indent=2, ensure_ascii=False),
                       encoding="utf-8", newline="\n")
        os.replace(tmp, self.path)

    def list(self, state: Optional[str] = None) -> List[Rule]:
        out = [Rule(**r) for r in self._load()]
        return [r for r in out if (state is None or r.state == state)]

    def add(self, rule: Rule) -> Rule:
        with self._lock:
            return self._add_locked(rule)

    def _add_locked(self, rule: Rule) -> Rule:
        rules = self._load()
        if not rule.created_at:
            rule.created_at = time.time()
        # replace by slug if present
        rules = [r for r in rules if _slug(r.get("text", "")) != rule.slug]
        rules.append(asdict(rule))
        self._save(rules)
        return rule

    def set_state(self, slug: str, state: str) -> bool:
        if state not in STATES:
            raise ValueError(f"bad state: {state}")
        with self._lock:
            return self._set_state_locked(slug, state)

    def _set_state_locked(self, slug: str, state: str) -> bool:
        rules = self._load()
        hit = False
        for r in rules:
            if _slug(r.get("text", "")) == slug:
                r["state"] = state
                hit = True
        if hit:
            self._save(rules)
        return hit

    def exists_similar(self, text: str, threshold: float = 0.6) -> Optional[str]:
        ctoks = set(re.findall(r"[a-z0-9]{3,}", text.lower()))
        if not ctoks:
            return None
        for r in self._load():
            stoks = set(re.findall(r"[a-z0-9]{3,}", r.get("text", "").lower()))
            if not stoks:
                continue
            inter = len(ctoks & stoks); union = len(ctoks | stoks)
            if union and inter / union >= threshold:
                return _slug(r.get("text", ""))
        return None

    def remove(self, slug: str) -> bool:
        with self._lock:
            rules = self._load()
            kept = [r for r in rules if _slug(r.get("text", "")) != slug]
            if len(kept) == len(rules):
                return False
            self._save(kept)
            return True

    def update(self, slug: str, *, text: Optional[str] = None,
               scope: Optional[str] = None) -> Optional[Rule]:
        with self._lock:
            rules = self._load()
            hit = None
            for r in rules:
                if _slug(r.get("text", "")) == slug:
                    if text is not None:
                        r["text"] = text.strip() or r.get("text", "")
                    if scope is not None:
                        r["scope"] = scope.strip() or r.get("scope", "global")
                    hit = Rule(**r)
                    break
            if not hit:
                return None
            self._save(rules)
            return hit
