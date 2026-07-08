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

# Windows rejects CON/PRN/AUX/NUL/COM1-9/LPT1-9 as filenames regardless of extension.
_WIN_RESERVED = frozenset(
    {*(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
     "CON", "PRN", "AUX", "NUL"}
)


def _slug(name: str, *, fallback: str = "skill") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    s = (s[:48] or fallback).rstrip(". ")
    stem = s.split(".", 1)[0].upper()
    if stem in _WIN_RESERVED:
        s = f"{s}-item" if s else fallback
    return s or fallback


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
    supersedes: str = ""

    @property
    def slug(self) -> str:
        if getattr(self, "supersedes", ""):
            return f"{self.supersedes}-patch"
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
        ]
        if getattr(self, "supersedes", ""):
            fm.append(f"supersedes: {self.supersedes}")
        fm.extend([
            "---",
            "",
        ])
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
        supersedes=meta.get("supersedes", ""),
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
            tmp.write_text(skill.to_markdown(), encoding="utf-8", newline="\n")
            os.replace(tmp, p)
            return p

    def get(self, slug: str) -> Optional[Skill]:
        p = self._find(slug)
        return _parse(p.read_text(encoding="utf-8", errors="replace")) if p else None

    def list(self, state: Optional[str] = None) -> List[Skill]:
        out = []
        states = [state] if state else STATES
        for st in states:
            d = self.root / st
            if not d.exists():
                continue
            for f in sorted(d.glob("*.md")):
                out.append(_parse(f.read_text(encoding="utf-8", errors="replace")))
        return out

    def set_state(self, slug: str, state: str) -> Optional[Skill]:
        if state not in STATES:
            raise ValueError(f"bad state: {state}")
        sk = self.get(slug)
        if not sk:
            return None

        if state == "active" and getattr(sk, "supersedes", ""):
            orig_slug = sk.supersedes
            orig_sk = self.get(orig_slug)
            if orig_sk:
                orig_sk.body = sk.body
                orig_sk.description = sk.description
                orig_sk.name = sk.name
                orig_sk.source = sk.source
                self.save(orig_sk)
                p_patch = self._find(slug)
                if p_patch:
                    p_patch.unlink()
                return orig_sk

        sk.state = state
        self.save(sk)
        return sk

    def propose_update(self, slug: str, new_body: str, new_name: str = "", new_description: str = "", source: str = "") -> Skill:
        existing = self.get(slug)
        if not existing:
            raise ValueError(f"Skill not found: {slug}")
        # Always supersede the ROOT skill, never chain patch-of-a-patch (which
        # would grow the slug unboundedly: foo-patch-patch-patch...). If the
        # target is itself a pending patch, redirect to the skill it supersedes.
        root_slug = getattr(existing, "supersedes", "") or slug
        patch_skill = Skill(
            name=new_name or existing.name,
            description=new_description or existing.description,
            body=new_body,
            state="pending",
            source=source or existing.source,
            supersedes=root_slug
        )
        # Stable slug ({root}-patch): re-proposing overwrites the same pending
        # patch rather than creating a new one each time.
        self.save(patch_skill)
        return patch_skill

    def mark_used(self, slug: str) -> None:
        sk = self.get(slug)
        if sk:
            sk.used_count += 1
            sk.last_used = time.time()
            self.save(sk)

    def exists(self, slug: str) -> bool:
        return self._find(slug) is not None

    def remove(self, slug: str) -> bool:
        with self._lock:
            p = self._find(slug)
            if not p:
                return False
            p.unlink()
            return True

    def update(self, slug: str, *, name: Optional[str] = None,
               description: Optional[str] = None, body: Optional[str] = None) -> Optional[Skill]:
        old_path = self._find(slug)
        sk = self.get(slug)
        if not sk:
            return None
        old_slug = sk.slug
        if name is not None:
            sk.name = name.strip() or sk.name
        if description is not None:
            sk.description = description.strip()
        if body is not None:
            sk.body = body.strip()
        self.save(sk)
        if old_path and sk.slug != old_slug and old_path.exists():
            old_path.unlink()
        return sk
