"""Skills / rules / memory HTTP route bodies (peeled from ``harness.server``).

Handlers take a :class:`SkillsServices` so this module never imports
``harness.server`` at top level. ``server.Handler`` keeps auth/token gates
and thin path delegates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Union


@dataclass
class SkillsServices:
    """Explicit deps for skills/rules/memory HTTP handlers."""

    skills: Any
    rules: Any
    memory: Any
    get_pilot: Callable[[], Any]
    memory_char_limit: int


JsonPayload = Union[dict, list]


def post_skills_distill(svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/skills/distill."""
    return 200, svc.get_pilot().distill()


def post_skills_approve(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/skills/approve."""
    sk = svc.skills.set_state(body.get("slug", ""), "active")
    return 200, {"ok": bool(sk)}


def post_skills_add(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/skills/add."""
    name = (body.get("name") or "").strip()
    if not name:
        return 400, {"error": "name is required"}
    from ..skill_store import Skill
    sk = Skill(
        name=name,
        description=(body.get("description") or "").strip(),
        body=(body.get("body") or "").strip(),
        state="active",
        source="manual",
    )
    svc.skills.save(sk)
    return 200, {
        "ok": True,
        "slug": sk.slug,
        "name": sk.name,
        "state": sk.state,
        "source": sk.source,
    }


def post_skills_update(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/skills/update."""
    slug = (body.get("slug") or "").strip()
    if not slug:
        return 400, {"error": "slug is required"}
    sk = svc.skills.update(
        slug,
        name=body.get("name"),
        description=body.get("description"),
        body=body.get("body"),
    )
    if not sk:
        return 404, {"error": "skill not found"}
    return 200, {
        "ok": True,
        "slug": sk.slug,
        "name": sk.name,
        "description": sk.description,
        "state": sk.state,
    }


def post_skills_remove(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/skills/remove."""
    ok = svc.skills.remove(body.get("slug", ""))
    return 200, {"ok": ok}


def post_skills_reject(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/skills/reject."""
    svc.skills.set_state(body.get("slug", ""), "archived")
    return 200, {"ok": True}


def post_skills_archive(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/skills/archive."""
    svc.skills.set_state(body.get("slug", ""), "archived")
    return 200, {"ok": True}


def post_rules_approve(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/rules/approve."""
    ok = svc.rules.set_state(body.get("slug", ""), "active")
    return 200, {"ok": ok}


def post_rules_add(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/rules/add."""
    text = (body.get("text") or "").strip()
    if not text:
        return 400, {"error": "text is required"}
    from ..rule_store import Rule
    rule = Rule(
        text=text,
        scope=(body.get("scope") or "global").strip() or "global",
        state="active",
        source="manual",
    )
    svc.rules.add(rule)
    return 200, {
        "ok": True,
        "slug": rule.slug,
        "text": rule.text,
        "scope": rule.scope,
        "state": rule.state,
        "source": rule.source,
    }


def post_rules_update(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/rules/update."""
    slug = (body.get("slug") or "").strip()
    if not slug:
        return 400, {"error": "slug is required"}
    rule = svc.rules.update(slug, text=body.get("text"), scope=body.get("scope"))
    if not rule:
        return 404, {"error": "rule not found"}
    return 200, {
        "ok": True,
        "slug": rule.slug,
        "text": rule.text,
        "scope": rule.scope,
        "state": rule.state,
    }


def post_rules_remove(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/rules/remove."""
    ok = svc.rules.remove(body.get("slug", ""))
    return 200, {"ok": ok}


def post_rules_reject(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/rules/reject."""
    svc.rules.set_state(body.get("slug", ""), "archived")
    return 200, {"ok": True}


def post_memory_add(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/memory/add."""
    text = body.get("text", "")
    category = body.get("category", "general")
    entry = svc.memory.add(text, category=category, source="user")
    return 200, {
        "id": entry.id,
        "text": entry.text,
        "category": entry.category,
        "created_at": entry.created_at,
        "source": entry.source,
    }


def post_memory_remove(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/memory/remove."""
    entry_id = body.get("id", "")
    ok = svc.memory.remove(entry_id)
    return 200, {"ok": ok}


def post_memory_propose_accept(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/memory/propose/accept."""
    proposal_id = (body.get("id") or "").strip()
    if not proposal_id:
        return 400, {"ok": False, "error": "missing id"}
    result = svc.get_pilot().accept_memory_proposal(proposal_id)
    code = 200 if result.get("ok") else 404
    return code, result


def post_memory_propose_dismiss(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/memory/propose/dismiss."""
    proposal_id = (body.get("id") or "").strip()
    if not proposal_id:
        return 400, {"ok": False, "error": "missing id"}
    result = svc.get_pilot().dismiss_memory_proposal(proposal_id)
    code = 200 if result.get("ok") else 404
    return code, result


def get_skills(svc: SkillsServices) -> tuple[int, JsonPayload]:
    """GET /api/skills."""
    return 200, [
        {
            "slug": sk.slug,
            "name": sk.name,
            "description": sk.description,
            "state": sk.state,
            "source": sk.source,
            "used_count": sk.used_count,
            "body": sk.body,
            "supersedes": getattr(sk, "supersedes", ""),
        }
        for sk in svc.skills.list()
    ]


def get_rules(svc: SkillsServices) -> tuple[int, JsonPayload]:
    """GET /api/rules."""
    return 200, [
        {
            "slug": r.slug,
            "text": r.text,
            "scope": r.scope,
            "state": r.state,
            "source": r.source,
        }
        for r in svc.rules.list()
    ]


def get_memory(svc: SkillsServices) -> tuple[int, JsonPayload]:
    """GET /api/memory."""
    entries = svc.memory.list()
    return 200, {
        "memory": [
            {
                "id": e.id,
                "text": e.text,
                "category": e.category,
                "created_at": e.created_at,
                "source": e.source,
            }
            for e in entries
        ],
        "total_chars": svc.memory.total_chars(),
        "limit": svc.memory_char_limit,
    }
