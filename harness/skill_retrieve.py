from __future__ import annotations

"""Retrieve skill bodies by token overlap. Frozen prefix stays catalog-only.

Dest conversation used to concatenate every active/plugin skill body into
history[0]. That is OAE (bodies present whether the turn needs them) and it
bloats the prompt-cache prefix. This module is the RAE hatch: the prefix
lists name + description + slug; bodies are selected per query.

Stdlib-only. Python 3.9 floor. Never imports Puppetmaster (chat hot path).
"""

import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Sequence

from .skill_store import _slug

# Same chars→tokens heuristic as ConversationalSession context estimates.
CHARS_PER_TOKEN = 4
DEFAULT_MAX_COUNT = 4
DEFAULT_TOKEN_BUDGET = 1200

_TOKEN = re.compile(r"[A-Za-z0-9]{2,}", re.UNICODE)
_STOP = frozenset({
    "the", "and", "for", "what", "was", "were", "this", "that", "with",
    "from", "only", "when", "how", "why", "who", "are", "you", "your",
    "to", "of", "in", "on", "or", "as", "at", "be", "by", "it", "an",
})

SKILLS_METHOD_FRAMING = (
    "# Learned skills (METHOD ONLY -- how to "
    "approach work, distilled from earlier sessions). These "
    "are never evidence about the current code and never "
    "current findings: re-verify anything you intend to "
    "claim against this session's own tool results, and "
    "report what you could not check as not verified.\n"
)


@dataclass(frozen=True)
class SkillCatalogLine:
    """Frozen-prefix row: identity only. Body is retrieve-time."""

    name: str
    description: str
    slug: str


def _tokens(text: str) -> set:
    found = set()
    for raw in _TOKEN.findall(text or ""):
        token = raw.lower()
        if token not in _STOP:
            found.add(token)
    return found


def _estimate_tokens(text: str) -> int:
    return max(0, len(text or "") // CHARS_PER_TOKEN)


def skill_slug(skill: Any) -> str:
    """Prefer an explicit slug; otherwise derive one from the name."""
    explicit = getattr(skill, "slug", None)
    if explicit:
        return str(explicit)
    return _slug(str(getattr(skill, "name", "") or "skill"))


def catalog_line(skill: Any) -> SkillCatalogLine:
    return SkillCatalogLine(
        name=str(getattr(skill, "name", "") or ""),
        description=str(getattr(skill, "description", "") or ""),
        slug=skill_slug(skill),
    )


def format_skill_catalog(skills: Sequence[Any]) -> str:
    """METHOD ONLY catalog: name + description + slug. No bodies."""
    parts = []
    for skill in skills or ():
        line = catalog_line(skill)
        parts.append(
            "## Skill: {name}\n{description}\nslug: {slug}".format(
                name=line.name,
                description=line.description,
                slug=line.slug,
            )
        )
    if not parts:
        return ""
    return SKILLS_METHOD_FRAMING + "\n\n".join(parts)


def format_retrieved_skill_bodies(skills: Sequence[Any]) -> str:
    """METHOD ONLY bodies for the turn trailer. Empty if nothing to inject."""
    parts = []
    for skill in skills or ():
        body = str(getattr(skill, "body", "") or "").strip()
        if not body:
            continue
        name = str(getattr(skill, "name", "") or "")
        parts.append("## Skill: {name}\n{body}".format(name=name, body=body))
    if not parts:
        return ""
    return SKILLS_METHOD_FRAMING + "\n\n".join(parts)


def select_skill_bodies(
    query: str,
    skills: Iterable[Any],
    *,
    max_count: int = DEFAULT_MAX_COUNT,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> List[Any]:
    """Greedy retrieve by nonzero token overlap on name+description.

    Fail-closed: empty query, empty skills, or zero overlap returns [].
    Ranking is overlap size, then original order. A hit whose body would
    exceed the remaining token budget is skipped (not truncated).
    Never raises.
    """
    try:
        query_tokens = _tokens(query or "")
        if not query_tokens:
            return []
        try:
            limit = int(max_count)
            budget = int(token_budget)
        except (TypeError, ValueError):
            return []
        if limit <= 0 or budget <= 0:
            return []

        ranked = []
        for index, skill in enumerate(skills or ()):
            name = str(getattr(skill, "name", "") or "")
            description = str(getattr(skill, "description", "") or "")
            overlap = query_tokens & _tokens(name + " " + description)
            if not overlap:
                continue
            ranked.append((len(overlap), index, skill))
        ranked.sort(key=lambda item: (-item[0], item[1]))

        selected = []
        used = 0
        for _score, _index, skill in ranked:
            if len(selected) >= limit:
                break
            body = str(getattr(skill, "body", "") or "")
            if not body.strip():
                continue
            cost = _estimate_tokens(body)
            if cost <= 0:
                cost = 1
            if used + cost > budget:
                continue
            selected.append(skill)
            used += cost
        return selected
    except Exception:
        return []
