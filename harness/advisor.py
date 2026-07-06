"""Opt-in advisor pass: a read-only second opinion on pending pilot actions.

Before a turn's action list executes, a single cheap model call reviews it
for obvious footguns (destructive commands, writes to unexpected paths) and
returns warning strings. Warnings are advisory only -- execution proceeds
regardless, and total advisor failure yields zero warnings. Enabled with
HARNESS_ADVISOR=1 (default off). Stdlib-only.
"""
from __future__ import annotations

import json
import os
from typing import Any, List

MAX_ACTIONS_LISTED = 20
MAX_PROMPT_CHARS = 2000
MAX_WARNINGS = 5
MAX_WARNING_CHARS = 200

_SYSTEM = (
    "You are a cautious code-review advisor. You are shown a list of pending "
    "tool actions an autonomous coding agent is about to execute. Reply with "
    "ONLY a JSON array of short warning strings for actions that look "
    "dangerous, destructive, or inconsistent with each other. Reply with an "
    "empty JSON array [] when nothing stands out. No prose outside the array."
)


def advisor_enabled() -> bool:
    """Feature flag: the advisor is opt-in via HARNESS_ADVISOR."""
    return os.environ.get("HARNESS_ADVISOR", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _action_line(act: Any) -> str:
    kind = getattr(act, "kind", "") or "?"
    salient = (
        getattr(act, "command", None)
        or getattr(act, "path", None)
        or getattr(act, "query", None)
        or getattr(act, "url", None)
        or getattr(act, "tool", None)
        or ""
    )
    return f"- {kind}: {salient}" if salient else f"- {kind}"


def build_advisor_prompt(actions: list, repo: str) -> str:
    lines = [f"Workspace: {repo or '(none)'}", "Pending actions:"]
    for act in actions[:MAX_ACTIONS_LISTED]:
        lines.append(_action_line(act))
    if len(actions) > MAX_ACTIONS_LISTED:
        lines.append(f"- ... and {len(actions) - MAX_ACTIONS_LISTED} more")
    prompt = "\n".join(lines)
    return prompt[:MAX_PROMPT_CHARS]


def _parse_warnings(text: str) -> List[str]:
    """Warnings from the model reply; anything unparseable yields []."""
    if not text:
        return []
    snippet = text.strip()
    # Tolerate replies that wrap the array in prose or code fences.
    start = snippet.find("[")
    end = snippet.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        parsed = json.loads(snippet[start : end + 1])
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    warnings: List[str] = []
    for item in parsed:
        if isinstance(item, str) and item.strip():
            warnings.append(item.strip()[:MAX_WARNING_CHARS])
        if len(warnings) >= MAX_WARNINGS:
            break
    return warnings


def advise(actions: list, repo: str, driver: Any) -> List[str]:
    """One advisor call over the pending action list. Never raises."""
    if not actions or driver is None:
        return []
    try:
        prompt = build_advisor_prompt(actions, repo)
        response = driver.complete(prompt, system=_SYSTEM)
        text = getattr(response, "text", "") or ""
        return _parse_warnings(text)
    except Exception:
        return []
