from __future__ import annotations

"""Cursor beforeSubmitPrompt helper. Fail-open; never raise to the host."""

import json
import re
from typing import Any, Iterable, Optional

from .judge import Judgment, judge_turn, suggestion_block
from .roster import load_cursor_skills

SILLY_RE = re.compile(
    r"""
    gpu|cuda|tensorrt|triton|ptx|sass|nvml|rocm|mlx|metal\s*gpu|
    llama\.cpp|vllm|ollama|gguf|ggml|exllama|awq|gptq|quantiz|
    local\s*inference|speculative\s*decod|flash.?attention|
    undervolt|overclock|same.?silicon|
    invited\s*review|authorized\s*review|hackerone|
    world\s*record|speedrun|silly\s*humans|
    kernel\s*(fuse|hack|opt)|encoding|decoding
    """,
    re.IGNORECASE | re.VERBOSE,
)

SILLY_CONTEXT = (
    "Silly humans is in force. Software is a human translation of machine code. "
    "The current best or solved label is a world record, not a ceiling. "
    "Name the translation layer, measure it, look for leftover. "
    "Full text: ~/.cursor/skills/silly-humans/SKILL.md. "
    "Do not assess systems Cary does not own and was not invited to review."
)


def prompt_text(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    chunks = []
    for key in ("prompt", "user_prompt", "content", "message", "text"):
        value = payload.get(key)
        if isinstance(value, str):
            chunks.append(value)
    attachments = payload.get("attachments")
    if isinstance(attachments, list):
        for item in attachments:
            if isinstance(item, dict):
                for key in ("content", "text", "prompt"):
                    value = item.get(key)
                    if isinstance(value, str):
                        chunks.append(value)
    return "\n".join(chunks)


def silly_humans_wanted(judgment: Optional[Judgment], text: str) -> bool:
    regex_hit = bool(text and SILLY_RE.search(text))
    if judgment is None:
        return regex_hit
    if (judgment.skill or "") == "silly-humans":
        return True
    if judgment.silly_humans >= 0.5:
        return True
    # Empty/failed judgment only: keep the old regex as fail-open.
    if regex_hit and not (
        judgment.gate or judgment.depth or judgment.lane or judgment.skill
    ):
        return True
    return False


def hook_context(
    text: str,
    roster: Iterable[Any] = (),
    *,
    judged: Optional[Judgment] = None,
    decide=None,
) -> str:
    judgment = judged
    if judgment is None:
        try:
            judgment = judge_turn(text, roster, decide=decide)
        except Exception:
            judgment = None
    parts = []
    try:
        note = suggestion_block(judgment)
        if note:
            parts.append(note)
    except Exception:
        pass
    try:
        if silly_humans_wanted(judgment, text):
            parts.append(SILLY_CONTEXT)
    except Exception:
        if text and SILLY_RE.search(text):
            parts.append(SILLY_CONTEXT)
    return "\n\n".join(parts)


def hook_response(payload: object, roster: Optional[Iterable[Any]] = None, decide=None) -> dict:
    try:
        text = prompt_text(payload)
        if not text.strip():
            return {}
        skills = list(roster) if roster is not None else load_cursor_skills()
        context = hook_context(text, skills, decide=decide)
        if not context:
            return {}
        return {
            "continue": True,
            "additional_context": context,
            "agent_message": context,
        }
    except Exception:
        return {}


def run_stdin(raw: str = "") -> str:
    try:
        payload = json.loads(raw) if (raw or "").strip() else {}
    except ValueError:
        return "{}"
    return json.dumps(hook_response(payload))
