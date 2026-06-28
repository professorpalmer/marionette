"""Local-model wiki orchestration: the "backwards" structuring pass, run cheaply
by the harness's own pilot model instead of a frontier orchestrator.

The portable-llm-wiki has an expensive "orchestrator" pass that explodes a raw
conversation digest into structured knowledge: entity pages, concept pages,
decision pages. That pass normally costs frontier-model tokens (Anthropic/OpenAI
via the wiki backend, or a Puppetmaster frontier worker). But the harness already
has a cheap local pilot running (e.g. qwen3-coder-30b via OpenRouter, ~cents per
session). This module makes THAT model do the structuring locally, so the wiki
just receives finished pages -- no frontier orchestrator needed.

Design (mirrors skill_distiller):
- Pure of Puppetmaster. Takes a driver-like object exposing complete()/chat() and
  the raw session material; returns structured page dicts. No network, no PM.
- Deterministic-ish: the model emits a strict JSON envelope we validate. Malformed
  output yields an empty result (best-effort, never raises into the caller).
- Human-in-the-loop by default: this module only PREPARES pages. The caller
  decides whether to ingest (one-click approve) or auto-ingest (opt-in flag).

A "structured page" is {kind, title, slug, body} where kind is one of
entity | concept | decision. The caller files each as its own wiki source.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

# Keep the orchestration prompt tight: we want a small number of HIGH-SIGNAL pages,
# not a dump. The wiki is durable cross-LLM memory; noise there is worse than a gap.
_ORCH_SYSTEM = (
    "You are a knowledge librarian for a durable, cross-session wiki. You convert a "
    "raw engineering session digest into a SMALL set of high-signal, reusable wiki "
    "pages. Only capture durable facts worth preserving across future sessions: "
    "entities (people, systems, repos, services), concepts (architectures, decisions "
    "with reasoning, operating principles), and decisions (a choice made + why). "
    "Skip ephemeral debugging state, throwaway Q&A, and anything that will be stale "
    "in a week. Fewer, denser pages beat many thin ones. No emojis or decorative "
    "pictographs -- plain words only."
)

_VALID_KINDS = {"entity", "concept", "decision"}

_PROMPT_TEMPLATE = """Convert the session digest below into structured wiki pages.

Return ONLY a JSON object of this exact shape (no prose, no code fences):
{{
  "pages": [
    {{
      "kind": "entity" | "concept" | "decision",
      "title": "Short human title",
      "body": "Markdown body. Dense, factual, durable. Cross-reference other pages with [[wikilinks]] where natural."
    }}
  ]
}}

Rules:
- 0 to {max_pages} pages. Return an EMPTY pages array if nothing is durable enough to keep.
- Each body should stand alone and stay useful months later.
- Do not invent facts not supported by the digest.
- No secrets, credentials, or tokens.

Session objective: {objective}

Session digest:
{digest}
"""


def _slugify(text: str, maxlen: int = 80) -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:maxlen] or "untitled"


def _extract_json(text: str) -> Optional[dict]:
    """Pull the first balanced JSON object out of a model response."""
    if not text:
        return None
    # strip code fences if present
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    break
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except Exception:
        return None


def prepare_pages(
    driver: Any,
    objective: str,
    digest: str,
    *,
    max_pages: int = 5,
) -> dict:
    """Run the local model to structure a digest into wiki pages.

    Returns {"status": "...", "pages": [{kind,title,slug,body}, ...], "reason"?}.
    status is one of: prepared (>=1 page) | empty (model kept nothing) | error.
    Never raises -- wiki capture is best-effort.
    """
    if not digest or not digest.strip():
        return {"status": "empty", "pages": [], "reason": "no digest"}

    prompt = _PROMPT_TEMPLATE.format(
        max_pages=max_pages,
        objective=(objective or "(session)").strip(),
        digest=digest.strip()[:12000],
    )

    try:
        if hasattr(driver, "chat"):
            resp = driver.chat([{"role": "user", "content": prompt}], system=_ORCH_SYSTEM)
        else:
            resp = driver.complete(prompt, system=_ORCH_SYSTEM)
        text = getattr(resp, "text", "") or ""
        if getattr(resp, "error", None):
            return {"status": "error", "pages": [], "reason": str(resp.error)}
    except Exception as e:
        return {"status": "error", "pages": [], "reason": str(e)}

    obj = _extract_json(text)
    if not obj or not isinstance(obj, dict):
        return {"status": "error", "pages": [], "reason": "model did not return valid JSON"}

    raw_pages = obj.get("pages")
    if not isinstance(raw_pages, list):
        return {"status": "error", "pages": [], "reason": "no pages array"}

    pages = []
    seen_slugs = set()
    for p in raw_pages[:max_pages]:
        if not isinstance(p, dict):
            continue
        kind = (p.get("kind") or "").strip().lower()
        title = (p.get("title") or "").strip()
        body = (p.get("body") or "").strip()
        if kind not in _VALID_KINDS or not title or not body:
            continue
        slug = _slugify(title)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        pages.append({"kind": kind, "title": title, "slug": slug, "body": body})

    if not pages:
        return {"status": "empty", "pages": [], "reason": "model kept nothing durable"}
    return {"status": "prepared", "pages": pages}
