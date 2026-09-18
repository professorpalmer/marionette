from __future__ import annotations

"""Skill-like roster rows for Jev. Host Cursor skills are hook-only."""

import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional


@dataclass
class RosterSkill:
    name: str
    description: str = ""
    body: str = ""


def skill_name(skill: Any) -> str:
    return str(getattr(skill, "name", "") or "").strip()


def skill_description(skill: Any) -> str:
    return str(getattr(skill, "description", "") or "").strip()


def skill_body(skill: Any) -> str:
    return str(getattr(skill, "body", "") or "").strip()


def as_roster(skills: Iterable[Any]) -> List[RosterSkill]:
    rows = []
    for skill in skills or ():
        name = skill_name(skill)
        if not name:
            continue
        rows.append(
            RosterSkill(
                name=name,
                description=skill_description(skill),
                body=skill_body(skill),
            )
        )
    return rows


def _parse_frontmatter(text: str):
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    fm = text[3:end]
    body = text[end + 4 :]
    data = {}
    key = None
    buf = []
    for line in fm.splitlines():
        if re.match(r"^[A-Za-z0-9_-]+:", line):
            if key:
                data[key] = " ".join(x.strip() for x in buf).strip()
            key, rest = line.split(":", 1)
            rest = rest.strip()
            if rest in (">", ">-", "|"):
                buf = []
            else:
                buf = [rest.strip("'\"")]
        elif key:
            buf.append(line.strip())
    if key:
        data[key] = " ".join(x.strip() for x in buf).strip()
    return data, body


def load_cursor_skills(root: Optional[str] = None) -> List[RosterSkill]:
    """Read ~/.cursor/skills/*/SKILL.md. Empty on any error."""
    try:
        base = root or os.path.join(os.path.expanduser("~"), ".cursor", "skills")
        rows = []
        if not os.path.isdir(base):
            return []
        names = sorted(os.listdir(base))
        for name in names:
            path = os.path.join(base, name, "SKILL.md")
            if not os.path.isfile(path):
                continue
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            fm, body = _parse_frontmatter(text)
            skill_id = (fm.get("name") or name).strip()
            desc = re.sub(r"\s+", " ", fm.get("description") or "").strip()
            desc = desc.lstrip(">-").strip()
            rows.append(RosterSkill(name=skill_id, description=desc, body=body.strip()))
        return rows
    except Exception:
        return []
