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
VERSIONS_DIRNAME = "versions"
MIN_ADMIT_SUPPORT_MANUAL = 1
MIN_ADMIT_SUPPORT_DISTILLED = 2

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
    version: int = 1
    admit_support: int = 0
    admit_sessions: str = ""
    provenance_session: str = ""
    provenance_job: str = ""

    @property
    def slug(self) -> str:
        if getattr(self, "supersedes", ""):
            # Keep the -patch suffix inside the 48-char filename cap: a long
            # base slug + "-patch" re-truncated by _slug loses the suffix and
            # COLLIDES with the base skill's file (overwriting it on save).
            return _slug(self.supersedes)[:42].rstrip("-") + "-patch"
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
        if getattr(self, "version", 1) > 1:
            fm.append(f"version: {self.version}")
        if getattr(self, "admit_support", 0):
            fm.append(f"admit_support: {self.admit_support}")
        if getattr(self, "admit_sessions", ""):
            fm.append(f"admit_sessions: {self.admit_sessions}")
        if getattr(self, "provenance_session", ""):
            fm.append(f"provenance_session: {self.provenance_session}")
        if getattr(self, "provenance_job", ""):
            fm.append(f"provenance_job: {self.provenance_job}")
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
        version=_i("version", 1),
        admit_support=_i("admit_support", 0),
        admit_sessions=meta.get("admit_sessions", ""),
        provenance_session=meta.get("provenance_session", ""),
        provenance_job=meta.get("provenance_job", ""),
    )


class SkillStore:
    def __init__(self, root: Optional[str] = None):
        self.root = Path(root) if root else SKILLS_DIR
        for st in STATES:
            (self.root / st).mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

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
        # Legacy fallback: older writers (and direct file drops) saved skills
        # under un-truncated or otherwise non-canonical filenames, while API
        # slugs come from the parsed skill. Without this scan those skills
        # parse and list fine but can never be approved/rejected -- get()
        # misses the file. Match by filename slug first, then by the slug the
        # file's content actually produces (covers -patch files whose name
        # diverges from the filename).
        safe = _slug(slug)
        for st in STATES:
            d = self.root / st
            if not d.exists():
                continue
            for f in d.glob("*.md"):
                if _slug(f.stem) == safe:
                    return f
        for st in STATES:
            d = self.root / st
            if not d.exists():
                continue
            for f in d.glob("*.md"):
                try:
                    parsed = _parse(f.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    continue
                if parsed.slug == slug:
                    return f
        return None

    def _versions_dir(self, slug: str) -> Path:
        return self.root / VERSIONS_DIRNAME / _slug(slug)

    def snapshot_version(self, skill: Skill) -> Optional[Path]:
        """Append-only version snapshot before mutating an active skill."""
        with self._lock:
            if not skill or not skill.slug:
                return None
            vdir = self._versions_dir(skill.slug)
            vdir.mkdir(parents=True, exist_ok=True)
            version = max(1, int(getattr(skill, "version", 1) or 1))
            dest = vdir / f"v{version}.md"
            if dest.exists():
                return dest
            tmp = dest.with_suffix(".md.tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(skill.to_markdown())
            os.replace(tmp, dest)
            return dest

    def list_versions(self, slug: str) -> List[int]:
        with self._lock:
            vdir = self._versions_dir(slug)
            if not vdir.is_dir():
                return []
            out: List[int] = []
            for f in sorted(vdir.glob("v*.md")):
                stem = f.stem
                if stem.startswith("v") and stem[1:].isdigit():
                    out.append(int(stem[1:]))
            return out

    def _needed_admit_support(self, skill: Skill) -> int:
        source = (skill.source or "").strip().lower()
        if source.startswith("distilled") or source == "refine":
            return MIN_ADMIT_SUPPORT_DISTILLED
        return MIN_ADMIT_SUPPORT_MANUAL

    def admit(self, slug: str, approver_session_id: str = "") -> dict:
        """Human gate with independent session support (not worker self-rating)."""
        with self._lock:
            sk = self.get(slug)
            if not sk:
                return {"ok": False, "error": "skill not found"}
            sessions = [
                s.strip()
                for s in (getattr(sk, "admit_sessions", "") or "").split(",")
                if s.strip()
            ]
            approver = (approver_session_id or "").strip()
            if approver and approver not in sessions:
                sessions.append(approver)
            sk.admit_sessions = ",".join(sessions)
            sk.admit_support = len(sessions)
            needed = self._needed_admit_support(sk)
            if sk.admit_support < needed:
                self.save(sk)
                return {
                    "ok": True,
                    "active": False,
                    "pending_admit": True,
                    "slug": sk.slug,
                    "support": sk.admit_support,
                    "needed": needed,
                }
            self.snapshot_version(sk)
            promoted = self.set_state(slug, "active")
            if not promoted:
                return {"ok": False, "error": "could not activate skill"}
            promoted.admit_support = sk.admit_support
            promoted.admit_sessions = sk.admit_sessions
            self.save(promoted)
            return {
                "ok": True,
                "active": True,
                "slug": promoted.slug,
                "support": sk.admit_support,
                "needed": needed,
                "version": promoted.version,
            }

    def rollback(self, slug: str, version: Optional[int] = None) -> Optional[Skill]:
        """Restore a prior snapshot (default: latest archived version)."""
        with self._lock:
            versions = self.list_versions(slug)
            if not versions:
                return None
            target = int(version) if version is not None else (
                versions[-2] if len(versions) >= 2 else versions[-1]
            )
            snap_path = self._versions_dir(slug) / f"v{target}.md"
            if not snap_path.is_file():
                return None
            restored = _parse(snap_path.read_text(encoding="utf-8", errors="replace"))
            current = self.get(slug)
            if current:
                self.snapshot_version(current)
            restored.version = max(versions) + 1
            restored.state = current.state if current else restored.state
            existing = self._find(restored.slug)
            p = self._path(restored.state, restored.slug)
            if existing and existing != p:
                existing.unlink()
            tmp = p.with_suffix(".md.tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(restored.to_markdown())
            os.replace(tmp, p)
            return restored

    def save(self, skill: Skill) -> Path:
        with self._lock:
            # ensure a skill lives in exactly one file: state moves change the
            # dir, and legacy un-truncated filenames get renormalized to the
            # canonical slug path (found via the _find fallback scan).
            existing = self._find(skill.slug)
            if existing:
                try:
                    prior = _parse(existing.read_text(encoding="utf-8", errors="replace"))
                    body_changed = (
                        prior.body != skill.body
                        or prior.name != skill.name
                        or prior.description != skill.description
                    )
                    if body_changed and prior.state == "active" and skill.state == "active":
                        self.snapshot_version(prior)
                        skill.version = max(int(getattr(prior, "version", 1) or 1), 1) + 1
                except Exception:
                    pass
            p = self._path(skill.state, skill.slug)
            if existing and existing != p:
                existing.unlink()
            # atomic: write temp in the same dir, then os.replace (no torn reads)
            tmp = p.with_suffix(".md.tmp")
            # open() rather than Path.write_text: newline= lands there in
            # 3.10+, and we still support the 3.9 floor.
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(skill.to_markdown())
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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
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
