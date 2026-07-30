"""Canonical model identity for local-job envelopes and price/display boundaries.

Registry ids for agentic models already carry an ``agentic/`` namespace
(``agentic/<provider>/<slug>``). Local jobs historically always prepended
``{engine}/`` again, producing ``agentic/agentic/...``. One helper owns
idempotent strip/prefix so register, finish, price lookup, and ROUTING
invariants share the same identity space.

Legacy bare slugs (``z-ai/glm-5.2``) and already-canonical registry ids are
both accepted; double-prefixed and mixed forms collapse to one engine segment.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

ENGINE_LABELS = frozenset({"agentic", "native"})


def strip_engine_prefixes(model_id: str) -> str:
    """Strip every leading ``agentic/`` or ``native/`` segment (idempotent)."""
    mid = (model_id or "").strip()
    while "/" in mid:
        head, rest = mid.split("/", 1)
        if head.lower() in ENGINE_LABELS and rest:
            mid = rest
            continue
        break
    return mid


def leading_engine(model_id: str) -> str:
    """First engine label on ``model_id``, or empty when none is present."""
    mid = (model_id or "").strip()
    if "/" not in mid:
        return ""
    head = mid.split("/", 1)[0].lower()
    return head if head in ENGINE_LABELS else ""


def collapse_engine_prefixes(model_id: str) -> str:
    """Collapse repeated leading engine segments to a single canonical prefix.

    ``agentic/agentic/deepseek/x`` -> ``agentic/deepseek/x``
    ``native/foo`` stays ``native/foo``
    bare ``deepseek/x`` stays bare (no engine invented).
    """
    mid = (model_id or "").strip()
    if not mid:
        return ""
    eng = leading_engine(mid)
    body = strip_engine_prefixes(mid)
    if not body:
        return eng or mid
    if eng:
        return f"{eng}/{body}"
    return body


def envelope_model_id(engine: str, model_id: str) -> str:
    """Canonical ``job.model`` stamp: one engine prefix, never ``agentic/agentic/...``.

    When ``model_id`` already carries a matching (or any) engine prefix, that
    collapsed form is kept. Otherwise ``engine`` is prepended once.
    """
    eng = (engine or "").strip().lower()
    if eng not in ENGINE_LABELS:
        eng = ""
    mid = (model_id or "").strip()
    if not mid:
        return eng
    collapsed = collapse_engine_prefixes(mid)
    existing = leading_engine(collapsed)
    if existing:
        # Prefer the caller's engine when both are known and disagree.
        if eng and existing != eng:
            body = strip_engine_prefixes(collapsed)
            return f"{eng}/{body}" if body else eng
        return collapsed
    if eng:
        return f"{eng}/{mid}"
    return collapsed


def price_lookup_id(model_id: str) -> str:
    """Id suitable for registry / catalog rate lookup (engine prefixes removed)."""
    return strip_engine_prefixes(model_id)


def model_ids_equal(left: str, right: str) -> bool:
    """True when two ids name the same model under identity normalization."""
    a = strip_engine_prefixes(left).strip().lower()
    b = strip_engine_prefixes(right).strip().lower()
    if not a or not b:
        return False
    return a == b


def filter_rejected_excluding_selected(
    rejected: Any,
    selected_id: str,
    *,
    also_exclude: Optional[Iterable[str]] = None,
) -> List[Dict[str, str]]:
    """Drop rejected ledger rows that identity-match the selected model.

    Tolerates ``{"model"}`` / ``{"id"}`` shapes and non-dict entries.
    """
    exclude = [selected_id]
    if also_exclude:
        exclude.extend(str(x) for x in also_exclude if x)
    out: List[Dict[str, str]] = []
    if not isinstance(rejected, list):
        return out
    for row in rejected:
        if isinstance(row, dict):
            mid = str(row.get("model") or row.get("id") or "").strip()
            reason = str(row.get("reason") or "")
        else:
            mid = str(row).strip()
            reason = ""
        if not mid:
            continue
        if any(model_ids_equal(mid, cand) for cand in exclude if cand):
            continue
        out.append({"model": mid, "reason": reason})
    return out


def format_model_ref(engine: str, model_id: str) -> Dict[str, str]:
    """Structured view of a model stamp for envelope / display / price paths."""
    eng = (engine or "").strip().lower()
    if eng not in ENGINE_LABELS:
        eng = leading_engine(model_id) or ""
    display = envelope_model_id(eng, model_id)
    return {
        "engine": eng or leading_engine(display),
        "registry_id": collapse_engine_prefixes(model_id) or display,
        "display_id": display,
        "price_id": price_lookup_id(display or model_id),
    }
