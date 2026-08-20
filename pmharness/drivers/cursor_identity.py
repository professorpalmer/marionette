"""Cursor CLI requested-vs-served model identity.

The ``--print`` path reports the init ``model`` display label. ACP never
does. Compare family + effort after stripping Cursor chrome (context
sizes, ``No Thinking``) so picker receipts cannot stamp a requested id
as fact when Cursor actually served a different line (Fable vs Luna).
"""

from __future__ import annotations

import re
from typing import FrozenSet, Optional, Tuple

UNPINNED_CURSOR_MODELS = frozenset({"", "auto"})

IDENTITY_AUTO = "auto"
IDENTITY_UNREPORTED = "unreported"
IDENTITY_VERIFIED = "verified"
IDENTITY_MISMATCH = "mismatch"

IDENTITY_STATUSES = frozenset({
    IDENTITY_AUTO,
    IDENTITY_UNREPORTED,
    IDENTITY_VERIFIED,
    IDENTITY_MISMATCH,
})

_EFFORT = {
    "high": "high",
    "medium": "medium",
    "med": "medium",
    "low": "low",
    "xhigh": "xhigh",
    "extra-high": "xhigh",
    "extrahigh": "xhigh",
}

_SPEED = frozenset({"fast", "slow"})

_BRANDS = frozenset({
    "fable",
    "luna",
    "sol",
    "composer",
    "grok",
    "opus",
    "sonnet",
    "haiku",
    "gpt",
    "claude",
    "gemini",
    "codex",
})

# Distinctive Cursor lines: version display may disagree with the slug
# (``claude-fable-5-high`` vs ``Claude 4.5 Fable High``) without a swap.
_VERSIONLESS_LINES = frozenset({"fable", "luna", "sol"})

# Cursor chrome: 200K / 272k context / (200K) / No Thinking / thinking
_DECORATION_RE = re.compile(
    r"(?:"
    r"\d+(?:\.\d+)?\s*k(?:b)?(?:\s*context)?"
    r"|no\s*thinking"
    r"|thinking"
    r"|context"
    r")",
    re.IGNORECASE,
)

_SEP_RE = re.compile(r"[\s_/:+·•,;|()[\]{}]+")
_NUMERIC_RE = re.compile(r"\d+(?:\.\d+)?")


def is_unpinned_cursor_model(model: str) -> bool:
    """True when the picker left model selection to Cursor (``auto`` / empty)."""
    return (model or "").strip().lower() in UNPINNED_CURSOR_MODELS


def normalize_cursor_model_label(label: str) -> str:
    """Lowercase hyphen slug with Cursor display chrome removed."""
    text = (label or "").strip().lower()
    if not text:
        return ""
    text = _DECORATION_RE.sub(" ", text)
    text = _SEP_RE.sub("-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")


def _tokens(label: str) -> Tuple[str, ...]:
    slug = normalize_cursor_model_label(label)
    if not slug:
        return ()
    return tuple(part for part in slug.split("-") if part and part not in _SPEED)


def _effort(tokens: Tuple[str, ...]) -> Optional[str]:
    found = None
    for tok in tokens:
        mapped = _EFFORT.get(tok)
        if mapped:
            found = mapped
    return found


def _family_tokens(tokens: Tuple[str, ...]) -> FrozenSet[str]:
    return frozenset(tok for tok in tokens if tok not in _EFFORT)


def _brands(tokens: FrozenSet[str]) -> FrozenSet[str]:
    return tokens & _BRANDS


def _numeric(tokens: FrozenSet[str]) -> FrozenSet[str]:
    return frozenset(tok for tok in tokens if _NUMERIC_RE.fullmatch(tok))


def cursor_models_compatible(requested: str, served: str) -> bool:
    """True when served is the requested family + effort (decorations ignored)."""
    req = _tokens(requested)
    srv = _tokens(served)
    if not req or not srv:
        return True
    req_fam = _family_tokens(req)
    srv_fam = _family_tokens(srv)
    req_brands = _brands(req_fam)
    srv_brands = _brands(srv_fam)
    if req_brands and srv_brands and req_brands.isdisjoint(srv_brands):
        return False
    req_effort = _effort(req)
    srv_effort = _effort(srv)
    if req_effort and srv_effort and req_effort != srv_effort:
        return False
    shared_line = (req_brands & srv_brands) & _VERSIONLESS_LINES
    if not shared_line:
        req_nums = _numeric(req_fam)
        srv_nums = _numeric(srv_fam)
        if req_nums and srv_nums and req_nums.isdisjoint(srv_nums):
            return False
    if not req_brands and not srv_brands:
        req_slug = "-".join(tok for tok in req if tok not in _EFFORT)
        srv_slug = "-".join(tok for tok in srv if tok not in _EFFORT)
        if req_slug == srv_slug:
            return True
        return (
            srv_slug.startswith(req_slug + "-")
            or req_slug.startswith(srv_slug + "-")
        )
    req_core = req_fam - _numeric(req_fam)
    srv_core = srv_fam - _numeric(srv_fam)
    if req_core and srv_core:
        return bool(req_core & srv_core)
    return True


def cursor_identity_status(requested: str, served: str) -> str:
    """Classify requested vs provider-reported served identity."""
    if is_unpinned_cursor_model(requested):
        return IDENTITY_AUTO
    if not (served or "").strip():
        return IDENTITY_UNREPORTED
    if cursor_models_compatible(requested, served):
        return IDENTITY_VERIFIED
    return IDENTITY_MISMATCH


def cursor_identity_mismatch_message(requested: str, served: str) -> Optional[str]:
    """Closed-fail text when served is a different family or effort."""
    if cursor_identity_status(requested, served) != IDENTITY_MISMATCH:
        return None
    return (
        "Cursor served {0!r} instead of requested {1!r} "
        "(refusing to stamp the picker label as fact)"
    ).format(served, requested)
