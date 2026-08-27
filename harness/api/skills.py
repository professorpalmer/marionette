"""Skills / rules / memory HTTP route bodies (peeled from ``harness.server``).

Handlers take a :class:`SkillsServices` so this module never imports
``harness.server`` at top level. ``server.Handler`` keeps auth/token gates
and thin path delegates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Union

from .redaction import redact_api_secrets


@dataclass
class SkillsServices:
    """Explicit deps for skills/rules/memory HTTP handlers."""

    skills: Any
    rules: Any
    memory: Any
    get_pilot: Callable[[], Any]
    memory_char_limit: int
    memory_graph: Any = None


JsonPayload = Union[dict, list]


def post_skills_distill(svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/skills/distill."""
    return 200, svc.get_pilot().distill()


def post_skills_approve(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/skills/approve."""
    slug = (body.get("slug") or "").strip()
    if not slug:
        return 400, {"error": "slug is required"}
    approver = (body.get("session_id") or "").strip()
    if not approver:
        pilot = svc.get_pilot()
        approver = getattr(pilot, "harness_session_id", "") or ""
    result = svc.skills.admit(slug, approver)
    code = 200 if result.get("ok") else 404
    return code, result


def post_skills_rollback(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/skills/rollback — restore a prior skill version snapshot."""
    slug = (body.get("slug") or "").strip()
    if not slug:
        return 400, {"ok": False, "error": "slug is required"}
    version_raw = body.get("version")
    version = None
    if version_raw not in (None, ""):
        try:
            version = int(version_raw)
        except (TypeError, ValueError):
            return 400, {"ok": False, "error": "version must be an int"}
    sk = svc.skills.rollback(slug, version=version)
    if not sk:
        return 404, {"ok": False, "error": "no version to rollback"}
    return 200, {
        "ok": True,
        "slug": sk.slug,
        "version": sk.version,
        "state": sk.state,
    }


def get_skill_versions(slug: str, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """GET /api/skills/versions?slug="""
    cleaned = (slug or "").strip()
    if not cleaned:
        return 400, {"ok": False, "error": "slug is required"}
    versions = svc.skills.list_versions(cleaned)
    sk = svc.skills.get(cleaned)
    return 200, {
        "ok": True,
        "slug": cleaned,
        "current_version": getattr(sk, "version", 1) if sk else 0,
        "versions": versions,
    }


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
    text = (body.get("text") or "").strip()
    if not text:
        return 400, {"error": "text is required"}
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


def post_refine(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/refine — slash /refine or propose on HarnessRefineController."""
    pilot = svc.get_pilot()
    if not pilot or not hasattr(pilot, "handle_refine_slash"):
        return 404, {"ok": False, "error": "no active session"}
    command = (body.get("command") or "").strip()
    text = (body.get("text") or "").strip()
    if command or text.startswith("/refine"):
        return 200, pilot.handle_refine_slash(command or text)
    kind = (body.get("kind") or "memory").strip() or "memory"
    scope = (body.get("scope") or "global").strip() or "global"
    from ..harness_refine import get_refine_controller

    prop = get_refine_controller(pilot).propose(kind=kind, text=text, scope=scope)
    if prop is None:
        return 400, {"ok": False, "error": "could not propose refine"}
    return 200, {"ok": True, "proposed": prop.to_dict()}


def get_refine_history(svc: SkillsServices) -> tuple[int, JsonPayload]:
    """GET /api/refine/history."""
    pilot = svc.get_pilot()
    if not pilot or not hasattr(pilot, "refine_history"):
        return 404, {"ok": False, "error": "no active session"}
    return 200, {"ok": True, "history": pilot.refine_history()}


def post_refine_propose(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/refine/propose — create a pending card on HarnessRefineController."""
    text = (body.get("text") or "").strip()
    if not text:
        return 400, {"ok": False, "error": "missing text"}
    kind = (body.get("kind") or "memory").strip().lower() or "memory"
    # Allow "/refine rule Prefer X" style: leading kind token peels off.
    parts = text.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in ("memory", "rule", "skill", "role"):
        kind = parts[0].lower()
        text = parts[1].strip()
    if not text:
        return 400, {"ok": False, "error": "missing text"}
    scope = (body.get("scope") or "global").strip().lower() or "global"
    pilot = svc.get_pilot()
    if not pilot or not hasattr(pilot, "propose_refine"):
        return 404, {"ok": False, "error": "no active session"}
    result = pilot.propose_refine(
        kind=kind,
        text=text,
        scope=scope,
        category=str(body.get("category") or "general"),
        name=str(body.get("name") or ""),
        body=str(body.get("body") or ""),
    )
    code = 200 if result.get("ok") else 400
    return code, result


def post_refine_propose_accept(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/refine/propose/accept."""
    proposal_id = (body.get("id") or "").strip()
    if not proposal_id:
        return 400, {"ok": False, "error": "missing id"}
    pilot = svc.get_pilot()
    if not pilot or not hasattr(pilot, "accept_refine_proposal"):
        return 404, {"ok": False, "error": "no active session"}
    result = pilot.accept_refine_proposal(proposal_id)
    code = 200 if result.get("ok") else 404
    return code, result


def post_refine_propose_dismiss(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/refine/propose/dismiss."""
    proposal_id = (body.get("id") or "").strip()
    if not proposal_id:
        return 400, {"ok": False, "error": "missing id"}
    pilot = svc.get_pilot()
    if not pilot or not hasattr(pilot, "dismiss_refine_proposal"):
        return 404, {"ok": False, "error": "no active session"}
    result = pilot.dismiss_refine_proposal(proposal_id)
    code = 200 if result.get("ok") else 404
    return code, result


def post_refine_propose_rollback(svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/refine/propose/rollback."""
    pilot = svc.get_pilot()
    if not pilot or not hasattr(pilot, "rollback_refine"):
        return 404, {"ok": False, "error": "no active session"}
    result = pilot.rollback_refine()
    code = 200 if result.get("ok") else 400
    return code, result


def get_skills(svc: SkillsServices) -> tuple[int, JsonPayload]:
    """GET /api/skills."""
    return 200, redact_api_secrets([
        {
            "slug": sk.slug,
            "name": sk.name,
            "description": sk.description,
            "state": sk.state,
            "source": sk.source,
            "used_count": sk.used_count,
            "body": sk.body,
            "supersedes": getattr(sk, "supersedes", ""),
            "version": getattr(sk, "version", 1),
            "admit_support": getattr(sk, "admit_support", 0),
            "admit_sessions": getattr(sk, "admit_sessions", ""),
            "provenance_session": getattr(sk, "provenance_session", ""),
            "provenance_job": getattr(sk, "provenance_job", ""),
        }
        for sk in svc.skills.list()
    ])


def get_rules(svc: SkillsServices) -> tuple[int, JsonPayload]:
    """GET /api/rules."""
    return 200, redact_api_secrets([
        {
            "slug": r.slug,
            "text": r.text,
            "scope": r.scope,
            "state": r.state,
            "source": r.source,
        }
        for r in svc.rules.list()
    ])


def get_memory(svc: SkillsServices) -> tuple[int, JsonPayload]:
    """GET /api/memory."""
    entries = svc.memory.list()
    return 200, redact_api_secrets({
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
    })


def get_memory_graph(q: str, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """GET /api/memory/graph — nodes from MemoryStore, edges from MemoryGraph."""
    graph = svc.memory_graph
    if graph is None:
        return 200, redact_api_secrets({"nodes": [], "edges": []})
    query = (q or "").strip()
    if query:
        payload = graph.search(query)
    else:
        payload = graph.graph()
    return 200, redact_api_secrets({
        "nodes": list(payload.get("nodes") or []),
        "edges": list(payload.get("edges") or []),
    })


def post_memory_graph_edge(body: dict, svc: SkillsServices) -> tuple[int, JsonPayload]:
    """POST /api/memory/graph/edge — add a relation between memory ids."""
    graph = svc.memory_graph
    if graph is None:
        return 503, {"error": "memory graph unavailable"}
    source = (body.get("source") or "").strip()
    target = (body.get("target") or "").strip()
    rel = (body.get("rel") or "").strip()
    if not source or not target or not rel:
        return 400, {"error": "source, target, and rel are required"}
    try:
        edge = graph.add_edge(source, target, rel)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    return 200, redact_api_secrets(edge)
