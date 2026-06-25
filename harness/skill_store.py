from __future__ import annotations

"""Harness skill store: the durable procedural memory the self-learning loop
writes to. Skills are markdown files with a YAML-ish frontmatter block, stored
under ~/.pmharness/skills/<state>/<name>.md.

States (lifted from Hermes's curator pattern, but with a hard human-in-loop gate):
  - pending:  AUTO-GENERATED candidate, NOT yet used by the pilot. Requires
              explicit approval. (A bad auto-skill is worse than none -- this gate
              is the whole point.)
  - active:   approved; loaded into the pilot's context.
  - archived: retired (recoverable); never auto-deleted.

Frontmatter is parsed without PyYAML (stdlib only): simple key: value lines.
"""

import os
import re
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

SKILLS_DIR = Path(os.path.expanduser("~/.pmharness/skills"))
STATES = ("pending", "active", "archived")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "skill"


@dataclass
class Skill:
    name: str
    description: str = ""
    body: str = ""
    state: str = "pending"
    source: str = ""          # where it came from (e.g. "distilled:session")
    created_at: float = field(default_factory=time.time)
    used_count: int = 0
    last_used: float = 0.0

    @property
    def slug(self) -> str:
        return _slug(self.name)

    def to_markdown(self) -> str:
        fm = [
            "---",
            f"name: {self.name}",
            f"description: {self.description}",
            f"state: {self.state}",
            f"source: {self.source}",
            f"created_at: {self.created_at:.0f}",
            f"used_count: {self.used_count}",
            f"last_used: {self.last_used:.0f}",
            "---",
            "",
        ]
        return "\n".join(fm) + self.body.strip() + "\n"


def _parse(text: str) -> Skill:
    meta: Dict[str, str] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            block = text[3:end].strip()
            body = text[end + 4:].lstrip("\n")
            for line in block.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()

    def _f(k, d=0.0):
        try:
            return float(meta.get(k, d))
        except (TypeError, ValueError):
            return d

    def _i(k, d=0):
        try:
            return int(float(meta.get(k, d)))
        except (TypeError, ValueError):
            return d

    return Skill(
        name=meta.get("name", "untitled"),
        description=meta.get("description", ""),
        body=body.strip(),
        state=meta.get("state", "pending"),
        source=meta.get("source", ""),
        created_at=_f("created_at", time.time()),
        used_count=_i("used_count", 0),
        last_used=_f("last_used", 0.0),
    )


class SkillStore:
    def __init__(self, root: Optional[str] = None):
        self.root = Path(root) if root else SKILLS_DIR
        for st in STATES:
            (self.root / st).mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, state: str, slug: str) -> Path:
        # SECURITY: sanitize on the lookup path, not just on create. The server
        # passes user-supplied slugs straight here; without this, "../../x" would
        # escape the skills dir for read/write.
        safe = _slug(slug)
        return self.root / state / f"{safe}.md"

    def _find(self, slug: str) -> Optional[Path]:
        for st in STATES:
            p = self._path(st, slug)
            if p.exists():
                return p
        return None

    def save(self, skill: Skill) -> Path:
        with self._lock:
            # ensure a skill lives in exactly one state dir
            existing = self._find(skill.slug)
            if existing and existing.parent.name != skill.state:
                existing.unlink()
            p = self._path(skill.state, skill.slug)
            # atomic: write temp in the same dir, then os.replace (no torn reads)
            tmp = p.with_suffix(".md.tmp")
            tmp.write_text(skill.to_markdown())
            os.replace(tmp, p)
            return p

    def get(self, slug: str) -> Optional[Skill]:
        p = self._find(slug)
        return _parse(p.read_text()) if p else None

    def list(self, state: Optional[str] = None) -> List[Skill]:
        out = []
        states = [state] if state else STATES
        for st in states:
            d = self.root / st
            if not d.exists():
                continue
            for f in sorted(d.glob("*.md")):
                out.append(_parse(f.read_text()))
        return out

    def set_state(self, slug: str, state: str) -> Optional[Skill]:
        if state not in STATES:
            raise ValueError(f"bad state: {state}")
        sk = self.get(slug)
        if not sk:
            return None
        sk.state = state
        self.save(sk)
        return sk

    def mark_used(self, slug: str) -> None:
        sk = self.get(slug)
        if sk:
            sk.used_count += 1
            sk.last_used = time.time()
            self.save(sk)

    def exists(self, slug: str) -> bool:
        return self._find(slug) is not None
