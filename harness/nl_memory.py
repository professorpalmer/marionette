from __future__ import annotations

"""Natural-language memory/wiki query synthesis (pure, hermetic core).

Given a natural-language question ("what did we decide about the default
driver?") plus a set of already-retrieved candidate memory/wiki entries, build a
grounded prompt and have a small cheap model synthesize a direct answer that
cites the entries it used -- rather than dumping raw keyword/graph hits.

This module is the INTENT/pure layer: it does NO retrieval, NO network, and NO
LLM configuration. The single side-effecting dependency -- the model call -- is
injected as a `complete` callable so the core logic unit-tests hermetically with
a fake `complete` (no network, no API keys), per AGENTS.md.

Contract:
    answer_from_memory(question, entries, *, complete=None) -> {
        "answer": str,             # grounded prose, or a not-found sentinel
        "citations": [int, ...],   # 1-based entry numbers the model cited
        "used_entry_ids": [str, ...],  # resolved ids/titles of cited entries
    }

Each entry is a dict: {"title": str, "body": str, "source": str}. An optional
"id" is honored when present; otherwise the title (or a synthetic "entry-N") is
used as the stable id.

This module is intentionally NOT wired into the live pilot tool loop yet.
"""

import re
from typing import Callable, Optional

# Sentinel the model is instructed to return when the entries do not support an
# answer. We match it case-insensitively so a fake/real model can be lenient.
NOT_FOUND = "not found in memory"


def _entry_id(entry: dict, index: int) -> str:
    """Stable id for an entry: explicit id, else title, else synthetic."""
    if not isinstance(entry, dict):
        return f"entry-{index + 1}"
    for key in ("id", "title"):
        val = entry.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return f"entry-{index + 1}"


def _clean(val: object) -> str:
    return str(val).strip() if val is not None else ""


def build_prompt(question: str, entries: list[dict]) -> str:
    """Build a grounded prompt: numbered context + strict citation instructions.

    The model is told to answer ONLY from the provided entries, cite the entries
    it used by their number as [n], and return the not-found sentinel when the
    entries do not support an answer.
    """
    lines: list[str] = []
    lines.append(
        "You are a memory assistant. Answer the question using ONLY the numbered "
        "context entries below. Do not use outside knowledge."
    )
    lines.append(
        "Cite every entry you rely on by its number in square brackets, like "
        "[1] or [2]. You may cite more than one."
    )
    lines.append(
        f'If the entries do not contain the answer, reply exactly: "{NOT_FOUND}" '
        "and cite nothing."
    )
    lines.append("")
    lines.append("Context entries:")
    for i, entry in enumerate(entries):
        num = i + 1
        title = _clean(entry.get("title") if isinstance(entry, dict) else "")
        body = _clean(entry.get("body") if isinstance(entry, dict) else "")
        source = _clean(entry.get("source") if isinstance(entry, dict) else "")
        header = f"[{num}] {title}" if title else f"[{num}]"
        if source:
            header += f" (source: {source})"
        lines.append(header)
        lines.append(body if body else "(no body)")
        lines.append("")
    lines.append(f"Question: {_clean(question)}")
    lines.append("")
    lines.append("Answer:")
    return "\n".join(lines)


def parse_citations(text: str, num_entries: int) -> list[int]:
    """Extract 1-based citation numbers ([n]) from model output, in first-seen
    order, keeping only those in range [1, num_entries]."""
    found: list[int] = []
    for match in re.findall(r"\[(\d+)\]", text or ""):
        try:
            n = int(match)
        except ValueError:
            continue
        if 1 <= n <= num_entries and n not in found:
            found.append(n)
    return found


def _is_not_found(text: str) -> bool:
    return NOT_FOUND in (text or "").strip().lower()


def answer_from_memory(
    question: str,
    entries: list[dict],
    *,
    complete: Optional[Callable[[str], str]] = None,
) -> dict:
    """Synthesize a grounded answer to `question` from candidate `entries`.

    Pure orchestration: builds a grounded prompt, calls the injected `complete`
    model callable, and maps the returned citations back to entry ids. Raises
    ValueError if no `complete` is provided (the core stays PM/network-free).
    """
    entries = list(entries or [])

    # Empty-entries guard: nothing to ground on -> deterministic not-found,
    # without spending a model call.
    if not entries:
        return {"answer": NOT_FOUND, "citations": [], "used_entry_ids": []}

    if complete is None:
        raise ValueError(
            "answer_from_memory requires an injected `complete` callable "
            "(the model call is a dependency; the core stays hermetic)"
        )

    prompt = build_prompt(question, entries)
    raw = complete(prompt)
    text = _clean(raw)

    if not text or _is_not_found(text):
        return {"answer": NOT_FOUND, "citations": [], "used_entry_ids": []}

    citations = parse_citations(text, len(entries))
    used_entry_ids = [_entry_id(entries[n - 1], n - 1) for n in citations]

    return {
        "answer": text,
        "citations": citations,
        "used_entry_ids": used_entry_ids,
    }
