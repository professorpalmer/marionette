from __future__ import annotations

"""Shared CodeGraph auto-inject wrap for the chat turn.

Keyed off working context (user ask plus files already touched this turn),
not the raw user message alone. Softens Puppetmaster's "authoritative
starting points" line so a thin or stale slice cannot be treated as
verbatim on-disk text.
"""

import re
from typing import Any

_PM_AUTHORITATIVE = (
    "Use these symbols and files as authoritative starting points. "
    "Confirm with the live repo before relying on them, but do not "
    "re-scan the whole codebase if CodeGraph already located the "
    "relevant area."
)

_PM_SOURCE_GUIDANCE = (
    "> The code below is the **verbatim, current on-disk source** of these "
    "files — re-read from disk on this call and line-numbered, byte-for-byte "
    "identical to what the Read tool returns. It is NOT a summary, outline, "
    "or stale cache. Treat each block as a Read you have already performed: "
    "do not Read a file shown here."
)
_RANKED_GUIDANCE = (
    "Treat these as ranked starting points, not a verbatim on-disk guarantee."
)

_WRAP = (
    "CODEGRAPH HAS ALREADY BEEN QUERIED FOR THIS TASK. "
    "These are ranked starting points from the current working context "
    "(user ask plus files already touched this turn), not a verbatim "
    "on-disk guarantee. Confirm against the live tree. Prefer "
    "search_codegraph or a ranged read_file if a snippet looks stale.\n"
)


def working_query(session: Any, user_message: str) -> str:
    parts = [str(user_message or "").strip()]
    tx = getattr(session, "_task_tx", None)
    files = list(getattr(tx, "files", None) or [])[:12]
    if files:
        parts.append("Working files: " + " ".join(str(p) for p in files if str(p).strip()))
    return "\n".join(p for p in parts if p)


def _normalize_generated_guidance(text: str) -> str:
    parts = []
    prose = []
    fence = ""

    def flush_prose() -> None:
        parts.append("".join(prose).replace(
            _PM_SOURCE_GUIDANCE, _RANKED_GUIDANCE,
        ).replace(_PM_AUTHORITATIVE, _RANKED_GUIDANCE))
        prose.clear()

    for line in text.splitlines(keepends=True):
        if fence:
            parts.append(line)
            if re.fullmatch(
                r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*",
                line.rstrip("\r\n"),
            ):
                fence = ""
        else:
            opening = re.match(r" {0,3}(`{3,}|~{3,})", line)
            if opening:
                flush_prose()
                fence = opening.group(1)
                parts.append(line)
            else:
                prose.append(line)
    flush_prose()
    return "".join(parts)


def wrap_slice(cg_slice: str) -> tuple[str, int]:
    text = str(cg_slice or "")
    if not text.strip():
        return "", 0
    symbols = text.count("- **") + text.count("#### ")
    text = _normalize_generated_guidance(text)
    try:
        from puppetmaster.codegraph import codegraph_prompt_section

        section = codegraph_prompt_section(text)
        # The formatter wraps the context in an outer fence. Normalize its
        # generated trailer separately so nested source fences stay opaque.
        before, context, after = section.partition(text.strip())
        if context:
            section = (
                before.replace(_PM_AUTHORITATIVE, _RANKED_GUIDANCE)
                + context
                + after.replace(_PM_AUTHORITATIVE, _RANKED_GUIDANCE)
            )
    except Exception:
        section = text
    return _WRAP + section, symbols
