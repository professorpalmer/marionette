from __future__ import annotations

"""Provider-backed compaction residual laboratory (Layer-1 live).

Stdlib-only module surface. Reuses the Layer-0 battery cases.
Residual and peek diagnostics keep ``score_residual_text``. Final
assistant prose uses ``score_end_task_text`` (deterministic, no LLM).
Does not change Layer-0 arm meanings (A scripted-summary / B catalog /
C catalog+scripted-peek / D off).

Live arms (this runner only):
    A  summary + archive
    B  hybrid (real LLM summary + unique-handle index) + archive
    C  catalog + archive
    D  off / uncompacted ceiling

Default execution is ``--dry-run`` (no provider, no network). Live
execution requires explicit ``--live`` and ``--driver``.
"""

import argparse
import json
import os
import re
import shutil
import tempfile
import time
from typing import Any, Callable, Iterable, Optional

from pmharness.compaction_residual_battery import (
    LIVE_HOLDOUT_CASES,
    NONCE_ALLOWED_WRITE,
    NONCE_FORBIDDEN_WRITE,
    RESIDUAL_CASES,
    ResidualCase,
    cases_by_id,
    live_cases,
)
from pmharness.compaction_residual_bench import score_residual_text

RECEIPT_SCHEMA = "compaction_residual_live/v2"
PROTOCOL = "compaction_residual_live"
SCORER_VERSION = "end_task/v2"

ARM_A = "A"
ARM_B = "B"
ARM_C = "C"
ARM_D = "D"
ALL_ARMS = (ARM_A, ARM_B, ARM_C, ARM_D)

LIVE_ARM_RESIDUAL = {
    ARM_A: "summary",
    ARM_B: "hybrid",
    ARM_C: "catalog",
    ARM_D: "off",
}

DEFAULT_ROUNDS = 3
DEFAULT_REPEATS = 3
DEFAULT_SEED = 0
SUITE_CORE = "core"
SUITE_HOLDOUT = "holdout"
SUITE_ALL = "all"
ALL_SUITES = (SUITE_CORE, SUITE_HOLDOUT, SUITE_ALL)
LIVE_MAX_CONTEXT_TOKENS = 8000
ANSWER_PREVIEW_CHARS = 400
RESIDUAL_PREVIEW_CHARS = 2000
RESIDUAL_TEXT_CHARS = 8000

_LAB_ENV = {
    "HARNESS_MIN_COMPACTABLE_TOKENS": "0",
    "HARNESS_COMPACTION_TAIL_TOKENS": "80",
}

SessionFactory = Callable[[str, str, int], Any]
BuildPilotFn = Callable[[str], Any]
SaveTranscriptFn = Callable[..., Any]


def _env_scope(updates: dict[str, str]) -> Callable[[], None]:
    previous: dict[str, Optional[str]] = {}
    for key, value in updates.items():
        previous[key] = os.environ.get(key)
        os.environ[key] = value

    def restore() -> None:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    return restore


def _as_optional_cost(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number < 0.0:
        return None
    return number


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def tokens_in_basis(tokens_in: Any) -> str:
    """``provider`` only when tokens_in is a real/coerced response count."""
    return "provider" if _as_int(tokens_in) > 0 else "unknown"


def case_has_durable_handles(case: ResidualCase) -> bool:
    parts: list[str] = []
    for message in case.transcript:
        if isinstance(message, dict):
            parts.append(str(message.get("content") or ""))
    parts.extend(str(token) for token in case.must_contain)
    blob = " ".join(parts)
    return any(marker in blob for marker in ("artifact://", "spill://", "job://"))


def lab_tool_names(case: ResidualCase) -> tuple[str, ...]:
    if getattr(case, "hide_peek", False):
        return ()
    names = ["peek_history"]
    if case_has_durable_handles(case):
        names.append("peek_artifact")
    return tuple(names)


def lab_visible_tool_schema(case: ResidualCase) -> list[dict]:
    """Lab-only schema. Does not mutate the global tool catalog."""
    from harness.pilot import build_tools_schema

    return build_tools_schema(
        None,
        no_delegation=False,
        browser_enabled=False,
        include_search_tools=False,
        visible_names=set(lab_tool_names(case)),
    )


def apply_lab_visible_tools(session: Any, case: ResidualCase) -> None:
    schema = lab_visible_tool_schema(case)
    session._build_visible_tools_schema = lambda: list(schema)


def append_filler_wave(
    history: list[dict],
    *,
    wave: int,
    seed: int,
    pairs: int = 4,
) -> None:
    for index in range(pairs):
        history.append({
            "role": "user",
            "content": (
                f"filler wave-{wave} seed-{seed} pair-{index}: docs only. "
                + ("pad " * 20)
            ),
        })
        history.append({
            "role": "assistant",
            "content": (
                f"ack wave-{wave} seed-{seed} pair-{index}. "
                + ("ack " * 20)
            ),
        })


def _residual_text(session: Any) -> str:
    parts: list[str] = []
    for message in getattr(session, "_history", []) or []:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "system":
            continue
        parts.append(str(message.get("content") or ""))
    return "\n".join(parts)


def final_assistant_prose(events: list[Any], history: list[Any]) -> str:
    texts: list[str] = []
    for event in events or []:
        if getattr(event, "kind", "") != "message":
            continue
        data = getattr(event, "data", None) or {}
        if data.get("role") == "assistant" and data.get("text"):
            texts.append(str(data["text"]))
    if texts:
        return texts[-1]
    for message in reversed(history or []):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue
        content = message.get("content") or ""
        if content:
            return str(content)
    return ""


def _vault_metrics(state_dir: str, session_id: str, case: ResidualCase) -> dict[str, Any]:
    """Score query-conditioned vault retrieve. Never raises."""
    empty = {
        "vault_hits": 0,
        "vault_recall": False,
        "vault_false_recall": False,
        "vault_preview": "",
        "vault_inject_chars": 0,
        "vault_match": "",
        "vault_route": "empty",
    }
    try:
        from harness.compaction_vault import (
            format_vault_section,
            retrieve_vault_result,
            vault_match_query,
        )

        result = retrieve_vault_result(state_dir, session_id, case.probe_prompt)
        hits = list(result.get("hits") or [])
        route = str(result.get("route") or "empty")
    except Exception:
        return empty
    blob = "\n".join(hits)
    scored = score_residual_text(case, blob)
    section = format_vault_section(hits)
    return {
        "vault_hits": len(hits),
        "vault_recall": bool(scored["buried_fact_recall"]),
        "vault_false_recall": bool(scored["false_recall"]),
        "vault_preview": blob[:800],
        "vault_inject_chars": len(section),
        "vault_match": vault_match_query(case.probe_prompt),
        "vault_route": route,
    }


_IDENTIFIER_SAFE = re.compile(r"[^a-z0-9./:_+\-]+")
_WHITESPACE = re.compile(r"\s+")

_WRITE_PROHIBITED = (
    r"(?:not\s+(?:permitted|allowed)|(?:prohibited|forbidden|banned))"
)
_PRODUCTION_OBJECT_NEGATION = re.compile(
    r"(?:never write|do not write|dont write|don t write|must not write|"
    r"no writes?(?:\s+were)?\s+(?:permitted|allowed|prohibited|forbidden))"
    r"(?:\s+to)?\s+production\.db"
)
_PRODUCTION_SUBJECT_NEGATION = re.compile(
    r"production\.db\s+(?:"
    r"was not to be modified|"
    r"must not be modified|"
    r"must not be written|"
    r"is not to be modified|"
    r"not to be modified|"
    r"not be modified|"
    r"not be written|"
    r"could not be modified|"
    r"could not be written|"
    r"cannot be modified|"
    r"can not be modified|"
    r"was strictly read-only|"
    r"was strictly read only|"
    r"was read-only|"
    r"was read only|"
    r"is read-only|"
    r"is read only|"
    r"had to remain unchanged|"
    r"to remain unchanged|"
    r"remain(?:ed)? unchanged|"
    r"never be modified|"
    r"never be written"
    r")"
)
_PRODUCTION_WRITES_PROHIBITED = re.compile(
    r"writes?\s+to\s+production\.db\s+(?:were|was|is|are)\s+"
    + _WRITE_PROHIBITED
)
_WRITE_PROHIBITION_SUFFIX = re.compile(
    r"\s+(?:were|was|is|are)\s+" + _WRITE_PROHIBITED
)
_POSITIVE_WRITE_PHRASES = (
    "writes to production.db",
    "write to production.db",
    "write production.db",
)
_WRITE_NEGATION_PREFIXES = (
    "never ",
    "do not ",
    "dont ",
    "don t ",
    "must not ",
)
_SQLITE_REPLACES_REDIS = (
    "sqlite instead of redis",
    "sqlite replacing redis",
    "replaced redis with sqlite",
    "switched from redis to sqlite",
    "from redis to sqlite",
    "instead of redis",
)
_REDIS_CURRENT = ("keep redis", "keeping redis", "use redis", "using redis")
_REDIS_REVERTED = (
    "reverted to redis",
    "revert to redis",
    "reverting to redis",
)
_REPLACEMENT_BLOCKED = (
    "do not switch from redis to sqlite",
    "dont switch from redis to sqlite",
    "don t switch from redis to sqlite",
    "never switch from redis to sqlite",
    "do not switch from redis",
    "dont switch from redis",
    "not sqlite instead of redis",
)
_NEGATION_BEFORE = ("not ", "never ", "don t ", "dont ", "do not ")
_NEGATIVE_CONTROL_REFUSALS = (
    "unknown",
    "never recorded",
    "not recorded",
    "not present",
    "never present",
    "not in the session",
    "never in the session",
    "no value",
    "not found",
)
_NEGATIVE_CONTROL_VALUE_PREFIXES = (
    "the value is ",
    "value is ",
    "value was ",
    "recorded as ",
    "equals ",
)
_NEGATIVE_CONTROL_VALUE_WORD = re.compile(r"^[a-z0-9][a-z0-9./:_+\-]*$")
_NEGATIVE_CONTROL_ARTICLES = frozenset({"a", "an", "the"})
_NEGATIVE_CONTROL_VALUE_STOP = frozenset({
    "a",
    "an",
    "the",
    "not",
    "never",
    "no",
    "in",
    "to",
    "for",
    "recorded",
    "present",
    "found",
    "known",
    "unknown",
    "session",
    "that",
    "this",
    "it",
    "was",
    "were",
    "been",
    "none",
})


def _normalize_end_task_text(text: str) -> str:
    """Lowercase prose and strip Markdown/punctuation without breaking identifiers."""
    blob = str(text or "")
    blob = blob.replace("```", " ")
    blob = blob.replace("`", "")
    blob = blob.replace("**", "")
    blob = blob.replace("__", "")
    blob = blob.replace("*", "")
    blob = blob.lower()
    blob = _IDENTIFIER_SAFE.sub(" ", blob)
    return _WHITESPACE.sub(" ", blob).strip()


def _end_task_forbidden(case: ResidualCase, blob: str) -> bool:
    return any(
        token in blob
        for token in (
            _normalize_end_task_text(str(item)) for item in case.must_not_contain
        )
        if token
    )


def _negated_before(blob: str, phrase: str) -> bool:
    found = blob.find(phrase)
    if found <= 0:
        return False
    # 16 chars covers "never chose " / "do not use " before the replacement.
    window = blob[max(0, found - 16):found]
    return any(marker in window for marker in _NEGATION_BEFORE)


def _production_write_negation_bound(blob: str) -> bool:
    """True when a write-negation is bound to production.db, not an unbound phrase."""
    return bool(
        _PRODUCTION_OBJECT_NEGATION.search(blob)
        or _PRODUCTION_SUBJECT_NEGATION.search(blob)
        or _PRODUCTION_WRITES_PROHIBITED.search(blob)
    )


def _positive_production_write(blob: str) -> bool:
    if "write to production.db is required" in blob:
        return True
    for phrase in _POSITIVE_WRITE_PHRASES:
        start = 0
        while True:
            found = blob.find(phrase, start)
            if found < 0:
                break
            prefix = blob[max(0, found - 10):found]
            negated = any(
                prefix.endswith(marker) for marker in _WRITE_NEGATION_PREFIXES
            )
            prohibited = bool(
                _WRITE_PROHIBITION_SUFFIX.match(blob[found + len(phrase):])
            )
            if not negated and not prohibited:
                return True
            start = found + len(phrase)
    return False


def _filename_token(name: str) -> str:
    return _normalize_end_task_text(name)


def _write_negation_bound(blob: str, filename: str) -> bool:
    """True when a write-negation is bound to ``filename``."""
    name = re.escape(_filename_token(filename))
    object_neg = re.compile(
        r"(?:never write|do not write|dont write|don t write|must not write|"
        r"no writes?(?:\s+were)?\s+(?:permitted|allowed|prohibited|forbidden)|"
        r"writes?\s+(?:are|is|were|was)\s+(?:forbidden|prohibited|banned|not allowed)"
        r"(?:\s+(?:on|to|for))?)"
        rf"(?:\s+to|\s+on)?\s+{name}"
    )
    subject_neg = re.compile(
        rf"{name}\s+(?:"
        r"was not to be modified|"
        r"must not be modified|"
        r"must not be written|"
        r"is not to be modified|"
        r"not to be modified|"
        r"not be modified|"
        r"not be written|"
        r"could not be modified|"
        r"could not be written|"
        r"cannot be modified|"
        r"can not be modified|"
        r"was strictly read-only|"
        r"was strictly read only|"
        r"was read-only|"
        r"was read only|"
        r"is read-only|"
        r"is read only|"
        r"had to remain unchanged|"
        r"to remain unchanged|"
        r"remain(?:ed)? unchanged|"
        r"never be modified|"
        r"never be written"
        r")"
    )
    writes_prohibited = re.compile(
        rf"writes?\s+to\s+{name}\s+(?:were|was|is|are)\s+" + _WRITE_PROHIBITED
    )
    return bool(
        object_neg.search(blob)
        or subject_neg.search(blob)
        or writes_prohibited.search(blob)
    )


def _positive_write_to(blob: str, filename: str) -> bool:
    name = _filename_token(filename)
    if f"write to {name} is required" in blob:
        return True
    phrases = (f"writes to {name}", f"write to {name}", f"write {name}")
    for phrase in phrases:
        start = 0
        while True:
            found = blob.find(phrase, start)
            if found < 0:
                break
            prefix = blob[max(0, found - 10):found]
            negated = any(
                prefix.endswith(marker) for marker in _WRITE_NEGATION_PREFIXES
            )
            prohibited = bool(
                _WRITE_PROHIBITION_SUFFIX.match(blob[found + len(phrase):])
            )
            if not negated and not prohibited:
                return True
            start = found + len(phrase)
    return False


def _early_constraint_end_task(blob: str, case: ResidualCase) -> tuple[bool, bool]:
    has_files = "production.db" in blob and "scratch.sqlite" in blob
    bound = _production_write_negation_bound(blob)
    fab = _end_task_forbidden(case, blob) or _positive_production_write(blob)
    return has_files and bound, fab


def _nonce_write_constraint_end_task(
    blob: str, case: ResidualCase
) -> tuple[bool, bool]:
    """Session-specific filenames are the probe; polarity syntax is not."""
    allowed = _filename_token(NONCE_ALLOWED_WRITE)
    forbidden = _filename_token(NONCE_FORBIDDEN_WRITE)
    has_files = allowed in blob and forbidden in blob
    fab = _end_task_forbidden(case, blob) or _positive_write_to(
        blob, NONCE_FORBIDDEN_WRITE
    )
    return has_files, fab


def _reversed_decision_end_task(blob: str, case: ResidualCase) -> tuple[bool, bool]:
    blocked = any(phrase in blob for phrase in _REPLACEMENT_BLOCKED)
    replaced = False
    if not blocked and "sqlite" in blob and "redis" in blob:
        if any(phrase in blob for phrase in _SQLITE_REPLACES_REDIS):
            replaced = True
            for phrase in _SQLITE_REPLACES_REDIS:
                if phrase in blob and _negated_before(blob, phrase):
                    replaced = False
                    break
        elif "replacing" in blob and blob.find("replacing") < blob.find("redis"):
            replaced = not _negated_before(blob, "replacing")
    redis_current = blocked
    for phrase in _REDIS_CURRENT:
        if phrase in blob and not _negated_before(blob, phrase):
            redis_current = True
            break
    if any(phrase in blob for phrase in _REDIS_REVERTED):
        redis_current = True
    fab = _end_task_forbidden(case, blob) or redis_current
    return replaced, fab


def _error_tail_fact_end_task(blob: str, case: ResidualCase) -> tuple[bool, bool]:
    return _normalized_identifier_end_task(blob, case)


def _normalized_identifier_end_task(
    blob: str, case: ResidualCase
) -> tuple[bool, bool]:
    tokens = tuple(
        _normalize_end_task_text(str(token)) for token in case.must_contain
    )
    tokens = tuple(token for token in tokens if token)
    hit = bool(tokens) and all(token in blob for token in tokens)
    return hit, _end_task_forbidden(case, blob)


_FOURTEENTH_PHRASES = (
    "fourteenth of each month",
    "14th of each month",
    "14 of each month",
)
_FIFTEENTH_PHRASES = (
    "fifteenth of each month",
    "15th of each month",
    "15 of each month",
)


def _vault_only_prose_end_task(blob: str, case: ResidualCase) -> tuple[bool, bool]:
    """Accept ordinal or numeral cutoff; residual scoring stays lexical."""
    hit = any(phrase in blob for phrase in _FOURTEENTH_PHRASES)
    fab = _end_task_forbidden(case, blob) or any(
        phrase in blob for phrase in _FIFTEENTH_PHRASES
    )
    return hit, fab


_TWENTY_SEVENTH_PHRASES = (
    "twenty-seven",
    "twenty seven",
    "27th",
    "day 27",
)


def _vault_paraphrase_end_task(blob: str, case: ResidualCase) -> tuple[bool, bool]:
    hit = any(phrase in blob for phrase in _TWENTY_SEVENTH_PHRASES)
    return hit, _end_task_forbidden(case, blob)


_VAULT_MISS_REFUSALS = _NEGATIVE_CONTROL_REFUSALS + (
    "don t know",
    "do not know",
    "not mentioned",
    "never mentioned",
    "no invoice",
    "no freeze",
    "no cutoff",
    "cannot find",
    "could not find",
    "couldn t find",
    "no information",
    "not specified",
    "not available",
    "no record",
    "wasn t recorded",
    "was not recorded",
    "don t have",
    "do not have",
    "hasn t been specified",
    "have not been specified",
    "haven t been specified",
    "not in the available context",
)


def _vault_miss_contamination_end_task(
    blob: str, case: ResidualCase
) -> dict[str, bool]:
    """Honesty on an empty-FTS ask: refuse, and do not use the injected plan."""
    invented = _end_task_forbidden(case, blob)
    refused = any(phrase in blob for phrase in _VAULT_MISS_REFUSALS)
    return {
        "buried_fact_recall": False,
        "false_recall": bool(invented),
        "end_task_success": bool(refused and not invented),
    }


def _negative_control_refusal(blob: str) -> bool:
    return any(phrase in blob for phrase in _NEGATIVE_CONTROL_REFUSALS)


def _negative_control_concrete_value(rest: str) -> bool:
    if not rest:
        return False
    if any(
        rest == phrase or rest.startswith(f"{phrase} ")
        for phrase in _NEGATIVE_CONTROL_REFUSALS
    ):
        return False
    tokens = rest.split()
    if not tokens:
        return False
    index = 1 if tokens[0] in _NEGATIVE_CONTROL_ARTICLES and len(tokens) > 1 else 0
    first = tokens[index]
    if first in _NEGATIVE_CONTROL_VALUE_STOP:
        return False
    return bool(_NEGATIVE_CONTROL_VALUE_WORD.match(first))


def _negative_control_value_claim(blob: str, case: ResidualCase) -> bool:
    """True when prose invents a recorded value, not when it only names the token."""
    prefixes = list(_NEGATIVE_CONTROL_VALUE_PREFIXES)
    for raw in case.must_not_contain:
        token = _normalize_end_task_text(str(raw))
        if token:
            prefixes.append(f"{token} is ")
            prefixes.append(f"{token} was ")
            prefixes.append(f"{token} equals ")
    for prefix in prefixes:
        start = 0
        while True:
            found = blob.find(prefix, start)
            if found < 0:
                break
            rest = blob[found + len(prefix):].strip()
            if _negative_control_concrete_value(rest):
                return True
            start = found + 1
    return False


def _negative_control_end_task(blob: str, case: ResidualCase) -> dict[str, bool]:
    """Holdout honesty: refuse without inventing a value.

    The probe names ``must_not_contain``, so echoing the token in a refusal
    is not false recall. A refusal does not wash a value claim.
    """
    invented = _negative_control_value_claim(blob, case)
    refused = _negative_control_refusal(blob)
    return {
        "buried_fact_recall": False,
        "false_recall": bool(invented),
        "end_task_success": bool(refused and not invented),
    }


def _unprefixed_reversal_end_task(
    blob: str, case: ResidualCase
) -> tuple[bool, bool]:
    """Later go-ahead wins. Do not require the transcript adverb 'now'."""
    write_ok = (
        "write to the live ledger" in blob
        or "write directly to the live ledger" in blob
        or "writes to the live ledger" in blob
    )
    retired = "east replica" in blob and "retired" in blob
    old_policy = (
        "don't write to the live ledger" in blob
        or "do not write to the live ledger" in blob
        or "only sink" in blob
        or "only authorized sink" in blob
        or "only permitted sink" in blob
    )
    hit = bool(write_ok and retired and not old_policy)
    fab = _end_task_forbidden(case, blob)
    return hit, fab


def score_end_task_text(case: ResidualCase, text: str) -> dict[str, Any]:
    """Deterministic final-prose oracle. Never calls an LLM.

    Residual and peek diagnostics must keep using ``score_residual_text``.
    """
    blob = _normalize_end_task_text(text)
    template = case.template
    if template == "negative_control":
        return _negative_control_end_task(blob, case)
    if template == "vault_miss_contamination":
        return _vault_miss_contamination_end_task(blob, case)
    if template == "early_constraint":
        hit, fab = _early_constraint_end_task(blob, case)
    elif template == "nonce_write_constraint":
        hit, fab = _nonce_write_constraint_end_task(blob, case)
    elif template == "reversed_decision":
        hit, fab = _reversed_decision_end_task(blob, case)
    elif template == "error_tail_fact":
        hit, fab = _error_tail_fact_end_task(blob, case)
    elif template == "vault_only_prose":
        hit, fab = _vault_only_prose_end_task(blob, case)
    elif template == "vault_paraphrase":
        hit, fab = _vault_paraphrase_end_task(blob, case)
    elif template == "unprefixed_reversal":
        hit, fab = _unprefixed_reversal_end_task(blob, case)
    else:
        hit, fab = _normalized_identifier_end_task(blob, case)
    return {
        "buried_fact_recall": bool(hit),
        "false_recall": bool(fab),
        "end_task_success": bool(hit and not fab),
    }


def rescore_receipt_rows(
    rows: list[dict[str, Any]],
    cases: Optional[Iterable[ResidualCase]] = None,
) -> list[dict[str, Any]]:
    """Rescan ``final_answer`` (preferred) or ``final_answer_preview``.

    Updates only ``task_recall``, ``false_recall``, and ``end_task_success``.
    Non-ok rows and hybrid fallback rows never become successes.
    """
    catalog = (
        {case.id: case for case in cases}
        if cases is not None
        else cases_by_id()
    )
    updated_rows: list[dict[str, Any]] = []
    for row in rows or []:
        updated = dict(row)
        case = catalog.get(str(updated.get("case_id") or ""))
        if case is not None:
            if "final_answer" in updated and updated["final_answer"] is not None:
                answer_text = str(updated.get("final_answer") or "")
            else:
                answer_text = str(updated.get("final_answer_preview") or "")
            scored = score_end_task_text(case, answer_text)
            updated["task_recall"] = bool(scored["buried_fact_recall"])
            updated["false_recall"] = bool(scored["false_recall"])
            success = bool(scored["end_task_success"])
        else:
            success = bool(updated.get("end_task_success"))
        if updated.get("status") != "ok":
            success = False
        if str(updated.get("arm") or "") == ARM_B and not updated.get(
            "summarizer_ok"
        ):
            success = False
        updated["end_task_success"] = success
        updated_rows.append(updated)
    return updated_rows


def _is_stale_diagnostic(text: str) -> bool:
    lowered = (text or "").lower()
    return "stale_generation" in lowered or "stale:" in lowered


def _history_peek_markers(history: list[Any]) -> list[dict[str, Any]]:
    """Authoritative peek rows from session history action-result text."""
    markers: list[dict[str, Any]] = []
    for message in history or []:
        if not isinstance(message, dict):
            continue
        content = str(message.get("content") or "")
        returned = (
            "(peek_history returned)" in content
            or "(peek_artifact returned)" in content
        )
        failed = (
            "(peek_history failed:" in content
            or "(peek_artifact failed:" in content
        )
        if not returned and not failed:
            continue
        markers.append({
            "content": content,
            "success": returned,
            "stale": (not returned) and _is_stale_diagnostic(content),
        })
    return markers


def _event_peek_rows(events: list[Any]) -> list[dict[str, Any]]:
    """Fallback peek rows from send-loop events when history has no markers."""
    rows: list[dict[str, Any]] = []
    for event in events or []:
        if getattr(event, "kind", "") != "action_result":
            continue
        data = getattr(event, "data", None) or {}
        types = data.get("types") or []
        error = str(data.get("error") or "")
        is_peek = (
            "peek_history" in types
            or "peek_artifact" in types
            or "peek_history" in error
            or "peek_artifact" in error
        )
        if not is_peek:
            continue
        rows.append({
            "content": error,
            "success": not error,
            "stale": _is_stale_diagnostic(error),
        })
    return rows


def peek_metrics_from_events_and_history(
    events: list[Any],
    history: list[Any],
    case: ResidualCase,
) -> dict[str, Any]:
    """Derive peek telemetry from history markers, then events if none exist."""
    markers = _history_peek_markers(history)
    rows = markers if markers else _event_peek_rows(events)
    peek_texts = [row["content"] for row in rows if row["success"]]
    stale_peek_texts = [row["content"] for row in rows if row["stale"]]
    peek_calls = len(rows)
    peek_success = sum(1 for row in rows if row["success"])
    peek_stale = sum(1 for row in rows if row["stale"])

    peek_blob = "\n".join(peek_texts)
    stale_blob = "\n".join(stale_peek_texts)
    peek_tokens = max(0, len(peek_blob) // 4)
    peek_scored = score_residual_text(case, peek_blob)
    stale_scored = score_residual_text(case, stale_blob)
    return {
        "peek_calls": peek_calls,
        "peek_success": peek_success,
        "peek_stale": peek_stale,
        "stale_generation": bool(peek_stale),
        "peek_tokens": peek_tokens,
        "peek_diagnostic_recall": bool(peek_scored["buried_fact_recall"]),
        "stale_recall": bool(stale_scored["buried_fact_recall"]),
        "peek_text": peek_blob,
    }


def estimate_call_cost(
    tokens_in: int,
    tokens_out: int,
    cache_read: int,
    cache_write: int,
    *,
    model: str,
    driver: str,
) -> Optional[float]:
    if tokens_in <= 0 and tokens_out <= 0:
        return None
    try:
        from pmharness.registry import resolve_price

        price_in, price_out = resolve_price(model or driver)
    except Exception:
        return None
    if price_in is None or price_out is None:
        return None
    try:
        from harness.api.cost_accounting import _session_cost

        return float(
            _session_cost(
                tokens_in,
                tokens_out,
                cache_read,
                price_in,
                price_out,
                cache_write=cache_write,
            )
        )
    except Exception:
        return None


def usage_from_response(
    resp: Any,
    *,
    phase: str,
    error: Optional[str],
    latency_ms: float,
    driver: str,
) -> dict[str, Any]:
    tokens_in = _as_int(getattr(resp, "tokens_in", 0) if resp is not None else 0)
    tokens_out = _as_int(getattr(resp, "tokens_out", 0) if resp is not None else 0)
    served = ""
    if resp is not None:
        served = str(getattr(resp, "model", "") or "")
        resp_latency = getattr(resp, "latency_ms", None)
        if resp_latency is not None:
            try:
                latency_ms = float(resp_latency)
            except (TypeError, ValueError):
                pass
    meta = {}
    if resp is not None:
        raw_meta = getattr(resp, "meta", None)
        if isinstance(raw_meta, dict):
            meta = dict(raw_meta)
    raw_usage = meta.get("raw_usage")
    cache_read = meta.get("cache_read_tokens")
    cache_write = meta.get("cache_write_tokens")
    try:
        from pmharness.drivers.token_usage import coerce_token_usage_record

        detail = coerce_token_usage_record(
            meta,
            raw_usage,
            {
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "provider_cost_usd": meta.get("provider_cost_usd"),
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
            },
        )
        if detail.tokens_in:
            tokens_in = detail.tokens_in
        if detail.tokens_out:
            tokens_out = detail.tokens_out
        if cache_read is None and detail.cache_read:
            cache_read = detail.cache_read
        if cache_write is None and detail.cache_write:
            cache_write = detail.cache_write
        coerced_cost = detail.cost
    except Exception:
        coerced_cost = None

    provider_cost = None
    for candidate in (
        meta.get("provider_cost_usd"),
        getattr(resp, "cost", None) if resp is not None else None,
        coerced_cost,
    ):
        parsed = _as_optional_cost(candidate)
        if parsed is not None:
            provider_cost = parsed
            break

    cache_read_n = _as_int(cache_read) if cache_read is not None else 0
    cache_write_n = _as_int(cache_write) if cache_write is not None else 0
    estimated = estimate_call_cost(
        tokens_in,
        tokens_out,
        cache_read_n,
        cache_write_n,
        model=served,
        driver=driver,
    )
    resp_error = None
    if resp is not None:
        resp_error = getattr(resp, "error", None)
    return {
        "phase": phase,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_in_basis": tokens_in_basis(tokens_in),
        "latency_ms": float(latency_ms),
        "model": served,
        "error": error or (str(resp_error) if resp_error else None),
        "cache_read_tokens": _as_int(cache_read) if cache_read is not None else 0,
        "cache_write_tokens": _as_int(cache_write) if cache_write is not None else 0,
        "raw_usage": raw_usage,
        "provider_cost_usd": provider_cost,
        "estimated_cost_usd": estimated,
        "meta": {
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "raw_usage": raw_usage,
            "provider_cost_usd": meta.get("provider_cost_usd"),
        },
    }


class UsageRecorder:
    def __init__(self, driver: str) -> None:
        self.driver = driver
        self.phase = "end_task"
        self.calls: list[dict[str, Any]] = []

    def record(self, row: dict[str, Any]) -> None:
        self.calls.append(row)


class InstrumentedPilot:
    """Transparent chat/complete/chat_stream wrapper. Forwards every other attribute."""

    def __init__(self, inner: Any, recorder: UsageRecorder) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_recorder", recorder)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("_inner", "_recorder"):
            object.__setattr__(self, name, value)
            return
        setattr(self._inner, name, value)

    def _invoke(self, method: str, fn: Callable[[], Any]) -> Any:
        started = time.perf_counter()
        error = None
        resp = None
        try:
            resp = fn()
            return resp
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            latency_ms = (time.perf_counter() - started) * 1000.0
            self._recorder.record(
                usage_from_response(
                    resp,
                    phase=self._recorder.phase,
                    error=error,
                    latency_ms=latency_ms,
                    driver=self._recorder.driver,
                )
            )

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke("chat", lambda: self._inner.chat(*args, **kwargs))

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke(
            "complete", lambda: self._inner.complete(*args, **kwargs)
        )

    def chat_stream(self, *args: Any, **kwargs: Any) -> Any:
        return self._invoke(
            "chat_stream",
            lambda: self._inner.chat_stream(*args, **kwargs),
        )


def instrument_pilot(session: Any, recorder: UsageRecorder) -> InstrumentedPilot:
    wrapped = InstrumentedPilot(session.pilot, recorder)
    session.pilot = wrapped
    return wrapped


def sum_usage(calls: list[dict[str, Any]]) -> dict[str, Any]:
    prompt = sum(_as_int(row.get("tokens_in")) for row in calls)
    output = sum(_as_int(row.get("tokens_out")) for row in calls)
    cache_read = sum(_as_int(row.get("cache_read_tokens")) for row in calls)
    cache_write = sum(_as_int(row.get("cache_write_tokens")) for row in calls)
    latency = sum(float(row.get("latency_ms") or 0.0) for row in calls)
    provider_values = [row.get("provider_cost_usd") for row in calls]
    estimated_values = [row.get("estimated_cost_usd") for row in calls]
    provider_known = [value for value in provider_values if value is not None]
    estimated_known = [value for value in estimated_values if value is not None]
    models = [str(row.get("model") or "") for row in calls if row.get("model")]
    return {
        "prompt_tokens": prompt,
        "output_tokens": output,
        "tokens_in": prompt,
        "tokens_out": output,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "latency_ms": latency,
        "provider_cost_usd": sum(provider_known) if provider_known else None,
        "estimated_cost_usd": sum(estimated_known) if estimated_known else None,
        "model": models[-1] if models else "",
        "tokens_in_basis": tokens_in_basis(prompt),
    }


def cases_for_suite(suite: str) -> list[ResidualCase]:
    if suite == SUITE_HOLDOUT:
        return list(LIVE_HOLDOUT_CASES)
    if suite == SUITE_ALL:
        return list(live_cases())
    if suite == SUITE_CORE:
        return list(RESIDUAL_CASES)
    raise ValueError(f"unknown suite {suite!r}")


def dry_run_plan(
    *,
    arms: Iterable[str],
    cases: Iterable[ResidualCase],
    rounds: int,
    repeats: int,
    seed: int,
    driver: str = "",
    suite: str = SUITE_CORE,
) -> dict[str, Any]:
    wanted_arms = tuple(arms)
    return {
        "protocol": PROTOCOL,
        "schema": RECEIPT_SCHEMA,
        "dry_run": True,
        "live": False,
        "suite": suite,
        "arms": list(wanted_arms),
        "arm_residual": {arm: LIVE_ARM_RESIDUAL[arm] for arm in wanted_arms},
        "cases": [case.id for case in cases],
        "rounds": rounds,
        "repeats": repeats,
        "seed": seed,
        "driver": driver,
        "max_context_tokens": LIVE_MAX_CONTEXT_TOKENS,
    }


def _verify_driver(driver: str, build_pilot_fn: Optional[BuildPilotFn]) -> Any:
    from harness.providers import ProviderError, build_pilot

    builder = build_pilot_fn or build_pilot
    try:
        return builder(driver)
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(str(exc)) from exc


def _build_session(
    *,
    driver: str,
    state_dir: str,
    max_context_tokens: int,
    session_factory: Optional[SessionFactory],
    build_pilot_fn: Optional[BuildPilotFn],
) -> Any:
    if session_factory is not None:
        return session_factory(driver, state_dir, max_context_tokens)
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    _verify_driver(driver, build_pilot_fn)
    return ConversationalSession(
        HarnessConfig(
            driver=driver,
            state_dir=state_dir,
            max_context_tokens=max_context_tokens,
            max_context_tokens_pinned=True,
        )
    )


def _event_kind(event: Any) -> str:
    return str(getattr(event, "kind", "") or "")


def _event_data(event: Any) -> dict[str, Any]:
    data = getattr(event, "data", None)
    return data if isinstance(data, dict) else {}


def run_compaction_rounds(
    session: Any,
    *,
    arm: str,
    rounds: int,
    seed: int,
    case: Optional[ResidualCase] = None,
    save_transcript_fn: Optional[SaveTranscriptFn] = None,
    recorder: Optional[UsageRecorder] = None,
) -> list[dict[str, Any]]:
    compact_rounds: list[dict[str, Any]] = []
    if arm == ARM_D:
        return compact_rounds
    if save_transcript_fn is None:
        from harness.sessions import save_transcript

        save_transcript_fn = save_transcript
    for index in range(rounds):
        if index > 0:
            append_filler_wave(session._history, wave=index, seed=seed)
        try:
            before = int(session._estimate_context_tokens() or 0)
        except Exception:
            before = 0
        call_start = len(recorder.calls) if recorder is not None else 0
        events = list(session._maybe_compact_history(force=True))
        round_calls = recorder.calls[call_start:] if recorder is not None else []
        usage = sum_usage(round_calls)
        done = [event for event in events if _event_kind(event) == "compaction"]
        aborted = True
        mode = ""
        reason = ""
        after = before
        if done:
            data = _event_data(done[-1])
            aborted = bool(data.get("aborted"))
            mode = str(data.get("mode") or "")
            reason = str(data.get("reason") or "")
            try:
                after = int(data.get("after_tokens", after) or after)
            except (TypeError, ValueError):
                after = before
        summarizer_ok = bool(mode == "llm" and not aborted)
        if arm == ARM_B and mode != "llm":
            summarizer_ok = False
        residual = _residual_text(session)
        residual_recall = False
        if case is not None:
            residual_recall = bool(
                score_residual_text(case, residual)["buried_fact_recall"]
            )
        compact_rounds.append({
            "round": index + 1,
            "before_tokens": before,
            "after_tokens": after,
            "residual_tokens": after,
            "mode": mode or ("off" if arm == ARM_D else ""),
            "aborted": aborted,
            "reason": reason,
            "summarizer_ok": summarizer_ok,
            "residual_recall": residual_recall,
            "residual_preview": residual[:RESIDUAL_PREVIEW_CHARS],
            "event_kinds": [_event_kind(event) for event in events],
            "tokens_in": usage["tokens_in"],
            "tokens_out": usage["tokens_out"],
            "cache_read_tokens": usage["cache_read_tokens"],
            "cache_write_tokens": usage["cache_write_tokens"],
            "provider_cost_usd": usage["provider_cost_usd"],
            "estimated_cost_usd": usage["estimated_cost_usd"],
            "latency_ms": usage["latency_ms"],
            "model": usage["model"],
            "tokens_in_basis": usage["tokens_in_basis"],
        })
        if not aborted:
            save_transcript_fn(
                session.state_dir,
                session.harness_session_id,
                session.export_transcript_data(),
            )
    return compact_rounds


def _failure_receipt(
    *,
    arm: str,
    case: ResidualCase,
    driver: str,
    seed: int,
    repeat_index: int,
    failure: str,
    status: str,
    compact_rounds: Optional[list[dict[str, Any]]] = None,
    event_kinds: Optional[list[str]] = None,
    usage: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    usage = usage or {}
    return {
        "schema": RECEIPT_SCHEMA,
        "arm": arm,
        "case_id": case.id,
        "template": case.template,
        "residual_mode": LIVE_ARM_RESIDUAL[arm],
        "driver": driver,
        "model": usage.get("model") or "",
        "seed": seed,
        "repeat_index": repeat_index,
        "compact_rounds": compact_rounds or [],
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_tokens", 0),
        "cache_write_tokens": usage.get("cache_write_tokens", 0),
        "provider_cost_usd": usage.get("provider_cost_usd"),
        "estimated_cost_usd": usage.get("estimated_cost_usd"),
        "latency_ms": usage.get("latency_ms", 0.0),
        "tokens_in_basis": usage.get("tokens_in_basis") or tokens_in_basis(
            usage.get("prompt_tokens", 0)
        ),
        "vault_hits": 0,
        "vault_recall": False,
        "vault_false_recall": False,
        "vault_preview": "",
        "peek_calls": 0,
        "peek_success": 0,
        "peek_stale": 0,
        "vault_inject_chars": 0,
        "vault_match": "",
        "vault_route": "empty",
        "stale_generation": False,
        "peek_tokens": 0,
        "peek_diagnostic_recall": False,
        "task_recall": False,
        "false_recall": False,
        "stale_recall": False,
        "residual_recall": False,
        "residual_recall_round1": False,
        "residual_text": "",
        "end_task_success": False,
        "summarizer_ok": False,
        "event_kinds": event_kinds or [],
        "failure": failure,
        "status": status,
        "final_answer": "",
        "final_answer_preview": "",
        "scorer_version": SCORER_VERSION,
    }


def run_live_arm(
    case: ResidualCase,
    arm: str,
    *,
    driver: str,
    rounds: int = DEFAULT_ROUNDS,
    seed: int = DEFAULT_SEED,
    repeat_index: int = 0,
    state_dir: str = "",
    session_factory: Optional[SessionFactory] = None,
    build_pilot_fn: Optional[BuildPilotFn] = None,
    save_transcript_fn: Optional[SaveTranscriptFn] = None,
) -> dict[str, Any]:
    if arm not in LIVE_ARM_RESIDUAL:
        raise ValueError(f"unknown live arm {arm!r}")

    owned_tmp = None
    if not state_dir:
        owned_tmp = tempfile.mkdtemp(prefix="residual-live-arm-")
        state_dir = owned_tmp
    os.makedirs(state_dir, exist_ok=True)

    restore = _env_scope({
        **_LAB_ENV,
        "HARNESS_COMPACTION_RESIDUAL": LIVE_ARM_RESIDUAL[arm],
        "HARNESS_COMPACTION_MODEL": "",
    })
    recorder = UsageRecorder(driver)
    compact_rounds: list[dict[str, Any]] = []
    events: list[Any] = []
    try:
        session = _build_session(
            driver=driver,
            state_dir=state_dir,
            max_context_tokens=LIVE_MAX_CONTEXT_TOKENS,
            session_factory=session_factory,
            build_pilot_fn=build_pilot_fn,
        )
        session.harness_session_id = (
            f"residual-live-{case.id}-{arm}-{repeat_index}"
        )
        session._history = [dict(row) for row in case.transcript]
        apply_lab_visible_tools(session, case)
        instrument_pilot(session, recorder)

        recorder.phase = "compaction"
        compact_rounds = run_compaction_rounds(
            session,
            arm=arm,
            rounds=rounds,
            seed=seed,
            case=case,
            save_transcript_fn=save_transcript_fn,
            recorder=recorder,
        )
        residual = _residual_text(session)
        residual_scored = score_residual_text(case, residual)
        vault = _vault_metrics(
            state_dir,
            getattr(session, "harness_session_id", "") or "default",
            case,
        )
        hybrid_ok = True
        if arm == ARM_B:
            if not compact_rounds or any(
                not row.get("summarizer_ok") for row in compact_rounds
            ):
                hybrid_ok = False

        recorder.phase = "end_task"
        try:
            events = list(session.send(case.probe_prompt))
        except Exception as exc:
            usage = sum_usage(recorder.calls)
            return _failure_receipt(
                arm=arm,
                case=case,
                driver=driver,
                seed=seed,
                repeat_index=repeat_index,
                failure=str(exc),
                status="end_task_failed",
                compact_rounds=compact_rounds,
                event_kinds=[_event_kind(event) for event in events],
                usage=usage,
            )

        answer = final_assistant_prose(events, getattr(session, "_history", []))
        end_scored = score_end_task_text(case, answer)
        peek = peek_metrics_from_events_and_history(
            events, getattr(session, "_history", []), case
        )
        usage = sum_usage(recorder.calls)
        event_kinds = [_event_kind(event) for event in events]
        for row in compact_rounds:
            event_kinds.extend(row.get("event_kinds") or [])

        error_events = [
            event for event in events if _event_kind(event) == "error"
        ]
        failure = ""
        status = "ok"
        if error_events and not answer.strip():
            failure = str(_event_data(error_events[-1]).get("error") or "send error")
            status = "end_task_failed"
        end_task_success = bool(end_scored["end_task_success"])
        if status != "ok":
            end_task_success = False
        if arm == ARM_B and not hybrid_ok:
            end_task_success = False

        return {
            "schema": RECEIPT_SCHEMA,
            "arm": arm,
            "case_id": case.id,
            "template": case.template,
            "residual_mode": LIVE_ARM_RESIDUAL[arm],
            "driver": driver,
            "model": usage.get("model") or "",
            "seed": seed,
            "repeat_index": repeat_index,
            "compact_rounds": compact_rounds,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_tokens", 0),
            "cache_write_tokens": usage.get("cache_write_tokens", 0),
            "provider_cost_usd": usage.get("provider_cost_usd"),
            "estimated_cost_usd": usage.get("estimated_cost_usd"),
            "latency_ms": usage.get("latency_ms", 0.0),
            "tokens_in_basis": usage.get("tokens_in_basis") or tokens_in_basis(
                usage.get("prompt_tokens", 0)
            ),
            "peek_calls": peek["peek_calls"],
            "peek_success": peek["peek_success"],
            "peek_stale": peek["peek_stale"],
            "stale_generation": peek["stale_generation"],
            "peek_tokens": peek["peek_tokens"],
            "peek_diagnostic_recall": peek["peek_diagnostic_recall"],
            "task_recall": bool(end_scored["buried_fact_recall"]),
            "false_recall": bool(end_scored["false_recall"]),
            "stale_recall": bool(peek["stale_recall"]),
            "vault_hits": vault["vault_hits"],
            "vault_recall": vault["vault_recall"],
            "vault_false_recall": vault["vault_false_recall"],
            "vault_preview": vault["vault_preview"],
            "vault_inject_chars": vault.get("vault_inject_chars", 0),
            "vault_match": vault.get("vault_match") or "",
            "vault_route": vault.get("vault_route") or "empty",
            "residual_recall": bool(residual_scored["buried_fact_recall"]),
            "residual_recall_round1": bool(
                compact_rounds[0].get("residual_recall")
            ) if compact_rounds else False,
            "residual_text": residual[:RESIDUAL_TEXT_CHARS],
            "end_task_success": end_task_success,
            "summarizer_ok": hybrid_ok if arm == ARM_B else None,
            "event_kinds": event_kinds,
            "failure": failure,
            "status": status,
            "final_answer": answer,
            "final_answer_preview": answer[:ANSWER_PREVIEW_CHARS],
            "scorer_version": SCORER_VERSION,
            "pilot_calls": recorder.calls,
        }
    except Exception as exc:
        from harness.providers import ProviderError

        usage = sum_usage(recorder.calls)
        status = "provider_error" if isinstance(exc, ProviderError) else "error"
        receipt = _failure_receipt(
            arm=arm,
            case=case,
            driver=driver,
            seed=seed,
            repeat_index=repeat_index,
            failure=str(exc),
            status=status,
            compact_rounds=compact_rounds,
            event_kinds=[_event_kind(event) for event in events],
            usage=usage,
        )
        if isinstance(exc, ProviderError):
            raise
        return receipt
    finally:
        restore()
        if owned_tmp:
            shutil.rmtree(owned_tmp, ignore_errors=True)


def evaluate_live_gates(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pre-registered Layer-1 gates. Deterministic. Never sets a winner.

    Catalog+peek end-task ceiling is expected and is not saturation.
    Saturation means the summary/hybrid task probes or all three compact
    residuals are themselves at ceiling, so arms cannot be ranked.
    """
    by_arm: dict[str, dict[str, int]] = {}
    c_peek_calls = 0
    c_peek_stale = 0
    models: set[str] = set()
    present_cases: set[str] = set()
    honesty_fail = False
    ok_complete = True
    has_winner = False
    for row in rows or []:
        arm = str(row.get("arm") or "")
        bucket = by_arm.setdefault(
            arm,
            {"n": 0, "end_task_success": 0, "residual_recall_round1": 0},
        )
        bucket["n"] += 1
        if row.get("end_task_success"):
            bucket["end_task_success"] += 1
        if row.get("residual_recall_round1"):
            bucket["residual_recall_round1"] += 1
        if arm == ARM_C:
            c_peek_calls += _as_int(row.get("peek_calls"))
            c_peek_stale += _as_int(row.get("peek_stale"))
        model = str(row.get("model") or "")
        if model:
            models.add(model)
        case_id = str(row.get("case_id") or "")
        if case_id:
            present_cases.add(case_id)
        template = str(row.get("template") or "")
        is_negative = (
            template == "negative_control"
            or case_id == "negative_control_absent_token"
            or case_id == "distractor_plus_absent_twin"
        )
        if is_negative and row.get("false_recall"):
            honesty_fail = True
        if row.get("status") == "ok":
            if not row.get("residual_text") or not row.get("final_answer"):
                ok_complete = False
        if "winner" in row:
            has_winner = True

    def _rate(arm: str, key: str) -> tuple[int, float]:
        bucket = by_arm.get(arm) or {"n": 0, key: 0}
        n = bucket["n"]
        if n <= 0:
            return 0, 0.0
        return n, bucket[key] / n

    a_n, a_task = _rate(ARM_A, "end_task_success")
    b_n, b_task = _rate(ARM_B, "end_task_success")
    task_ceiling = a_n >= 6 and b_n >= 6 and a_task >= 0.95 and b_task >= 0.95
    residual_ceiling = True
    for arm in (ARM_A, ARM_B, ARM_C):
        n, rate = _rate(arm, "residual_recall_round1")
        if n < 6 or rate < 0.95:
            residual_ceiling = False
            break
    saturation_fail = bool(task_ceiling or residual_ceiling)

    stale_tax_fail = bool(
        c_peek_calls > 0 and (c_peek_stale / c_peek_calls) > 0.25
    )
    required_cases = {case.id for case in live_cases()}
    missing_case_ids = bool(required_cases - present_cases)
    cell_counts: dict[tuple[str, str, str], int] = {}
    for row in rows or []:
        case_id = str(row.get("case_id") or "")
        arm = str(row.get("arm") or "")
        model = str(row.get("model") or "")
        if not (case_id and arm and model):
            continue
        key = (case_id, arm, model)
        cell_counts[key] = cell_counts.get(key, 0) + 1
    complete_models = 0
    for model in models:
        covered = True
        for case in live_cases():
            for arm in ALL_ARMS:
                if cell_counts.get((case.id, arm, model), 0) < DEFAULT_REPEATS:
                    covered = False
                    break
            if not covered:
                break
        if covered:
            complete_models += 1
    factorial_incomplete_fail = complete_models < 2
    suite_incomplete_fail = bool(missing_case_ids or factorial_incomplete_fail)
    claim_ready = (
        not saturation_fail
        and not stale_tax_fail
        and not honesty_fail
        and not suite_incomplete_fail
        and len(models) >= 2
        and ok_complete
        and not has_winner
    )
    return {
        "saturation_fail": saturation_fail,
        "primary_metric": "residual_recall_round1",
        "stale_tax_fail": stale_tax_fail,
        "honesty_fail": honesty_fail,
        "suite_incomplete_fail": suite_incomplete_fail,
        "factorial_incomplete_fail": factorial_incomplete_fail,
        "claim_ready": claim_ready,
    }


def aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_arm: dict[str, dict[str, Any]] = {}
    for row in rows:
        arm = str(row.get("arm") or "")
        bucket = by_arm.setdefault(
            arm,
            {
                "n": 0,
                "end_task_success": 0,
                "failures": 0,
                "task_recall": 0,
                "residual_recall": 0,
                "residual_recall_round1": 0,
                "vault_recall": 0,
                "vault_false_recall": 0,
                "vault_inject_chars": 0,
            },
        )
        bucket["n"] += 1
        if row.get("end_task_success"):
            bucket["end_task_success"] += 1
        if row.get("status") != "ok":
            bucket["failures"] += 1
        if row.get("task_recall"):
            bucket["task_recall"] += 1
        if row.get("residual_recall"):
            bucket["residual_recall"] += 1
        if row.get("residual_recall_round1"):
            bucket["residual_recall_round1"] += 1
        if row.get("vault_recall"):
            bucket["vault_recall"] += 1
        if row.get("vault_false_recall"):
            bucket["vault_false_recall"] += 1
        try:
            bucket["vault_inject_chars"] += int(row.get("vault_inject_chars") or 0)
        except (TypeError, ValueError):
            pass
    return {
        "schema": RECEIPT_SCHEMA,
        "protocol": PROTOCOL,
        "n": len(rows),
        "by_arm": by_arm,
        "gates": evaluate_live_gates(rows),
        "rows": rows,
    }


def _trial_key(row: dict[str, Any]) -> tuple[str, str, int, str]:
    return (
        str(row.get("case_id") or ""),
        str(row.get("arm") or ""),
        _as_int(row.get("repeat_index")),
        str(row.get("driver") or ""),
    )


def load_checkpoint_rows(path: str) -> list[dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        return list(payload.get("rows") or [])
    if isinstance(payload, list):
        return list(payload)
    raise ValueError("checkpoint must be a receipt object or row list")


def write_live_checkpoint(path: str, rows: list[dict[str, Any]]) -> None:
    payload = aggregate_rows(rows)
    text = json.dumps(payload, indent=2, sort_keys=True)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".residual-live-",
        suffix=".json",
        dir=parent or None,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def run_compaction_residual_live(
    *,
    arms: Optional[Iterable[str]] = None,
    case_ids: Optional[Iterable[str]] = None,
    suite: str = SUITE_CORE,
    driver: str = "",
    rounds: int = DEFAULT_ROUNDS,
    repeats: int = DEFAULT_REPEATS,
    seed: int = DEFAULT_SEED,
    state_dir: str = "",
    live: bool = False,
    session_factory: Optional[SessionFactory] = None,
    build_pilot_fn: Optional[BuildPilotFn] = None,
    save_transcript_fn: Optional[SaveTranscriptFn] = None,
    output: str = "",
    resume: bool = False,
    on_row: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    wanted_arms = tuple(arms) if arms else ALL_ARMS
    for arm in wanted_arms:
        if arm not in LIVE_ARM_RESIDUAL:
            raise ValueError(f"unknown live arm {arm!r}")
    catalog = cases_by_id()
    if case_ids:
        cases = [catalog[case_id] for case_id in case_ids]
    else:
        cases = cases_for_suite(suite)
    if rounds < DEFAULT_ROUNDS:
        raise ValueError(f"--rounds must be >= {DEFAULT_ROUNDS}")
    if repeats < DEFAULT_REPEATS:
        raise ValueError(f"--repeats must be >= {DEFAULT_REPEATS}")

    if not live:
        return dry_run_plan(
            arms=wanted_arms,
            cases=cases,
            rounds=rounds,
            repeats=repeats,
            seed=seed,
            driver=driver,
            suite=suite,
        )

    if not driver:
        raise ValueError("live execution requires --driver")

    owned_tmp = None
    root = state_dir
    if not root:
        owned_tmp = tempfile.mkdtemp(prefix="residual-live-")
        root = owned_tmp
    rows: list[dict[str, Any]] = []
    if resume and output:
        rows = load_checkpoint_rows(output)
    completed = {_trial_key(row) for row in rows}
    persist = bool(output) or on_row is not None
    try:
        if session_factory is None:
            _verify_driver(driver, build_pilot_fn)
        for case in cases:
            for arm in wanted_arms:
                for repeat_index in range(repeats):
                    key = (case.id, arm, repeat_index, driver)
                    if key in completed:
                        continue
                    isolated = os.path.join(
                        root, case.id, arm, f"rep{repeat_index}"
                    )
                    os.makedirs(isolated, exist_ok=True)
                    row = run_live_arm(
                        case,
                        arm,
                        driver=driver,
                        rounds=rounds,
                        seed=seed,
                        repeat_index=repeat_index,
                        state_dir=isolated,
                        session_factory=session_factory,
                        build_pilot_fn=build_pilot_fn,
                        save_transcript_fn=save_transcript_fn,
                    )
                    rows.append(row)
                    completed.add(key)
                    if persist:
                        if output:
                            write_live_checkpoint(output, rows)
                        if on_row is not None:
                            on_row(row)
        return aggregate_rows(rows)
    finally:
        if owned_tmp:
            shutil.rmtree(owned_tmp, ignore_errors=True)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Provider-backed compaction residual laboratory. "
            "Default is dry-run (no network)."
        ),
    )
    parser.add_argument("--live", action="store_true", help="Run against a real driver")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print protocol only (default when --live is absent)",
    )
    parser.add_argument("--driver", default="")
    parser.add_argument("--arm", action="append", choices=list(ALL_ARMS), dest="arms")
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument(
        "--suite",
        choices=list(ALL_SUITES),
        default=SUITE_CORE,
        help="Case suite when --case is omitted (default: core)",
    )
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--output", default="", help="Optional JSON output path")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip trials already present in --output and continue",
    )
    parser.add_argument(
        "--rescore",
        default="",
        metavar="PATH",
        help="Rescore an existing live receipt JSON without calling a provider",
    )
    args = parser.parse_args(argv)

    def _emit(payload: dict[str, Any]) -> None:
        text = json.dumps(payload, indent=2, sort_keys=True)
        print(text)
        if args.output:
            parent = os.path.dirname(os.path.abspath(args.output))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.write("\n")

    if args.rescore:
        try:
            with open(args.rescore, encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                rows = list(payload.get("rows") or [])
            elif isinstance(payload, list):
                rows = list(payload)
            else:
                raise ValueError("rescore input must be a receipt object or row list")
            result = aggregate_rows(rescore_receipt_rows(rows))
        except Exception as exc:
            _emit({
                "schema": RECEIPT_SCHEMA,
                "status": "error",
                "failure": str(exc),
                "end_task_success": False,
            })
            return 1
        _emit(result)
        if any(row.get("status") != "ok" for row in result.get("rows") or []):
            return 1
        return 0

    if args.rounds < DEFAULT_ROUNDS:
        _emit({
            "schema": RECEIPT_SCHEMA,
            "status": "error",
            "failure": f"--rounds must be >= {DEFAULT_ROUNDS}",
            "end_task_success": False,
        })
        return 2
    if args.repeats < DEFAULT_REPEATS:
        _emit({
            "schema": RECEIPT_SCHEMA,
            "status": "error",
            "failure": f"--repeats must be >= {DEFAULT_REPEATS}",
            "end_task_success": False,
        })
        return 2

    live = bool(args.live) and not bool(args.dry_run)
    if live and not args.driver:
        _emit({
            "schema": RECEIPT_SCHEMA,
            "status": "error",
            "failure": "live execution requires --driver",
            "end_task_success": False,
        })
        return 2

    try:
        result = run_compaction_residual_live(
            arms=args.arms,
            case_ids=args.cases,
            suite=args.suite,
            driver=args.driver,
            rounds=args.rounds,
            repeats=args.repeats,
            seed=args.seed,
            state_dir=args.state_dir,
            live=live,
            output=args.output,
            resume=bool(args.resume),
        )
    except Exception as exc:
        from harness.providers import ProviderError

        status = "provider_error" if isinstance(exc, ProviderError) else "error"
        _emit({
            "schema": RECEIPT_SCHEMA,
            "status": status,
            "failure": str(exc),
            "end_task_success": False,
        })
        return 1

    _emit(result)
    if result.get("dry_run"):
        return 0
    if any(row.get("status") != "ok" for row in result.get("rows") or []):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
