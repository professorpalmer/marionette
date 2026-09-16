"""Compaction continuity: edited and mentioned files stay tracked."""
from __future__ import annotations

import os
from typing import Iterable, List, Set


def normalize_working_path(path: str, repo: str = "") -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    if repo and os.path.isabs(text):
        try:
            rel = os.path.relpath(text, repo)
        except ValueError:
            rel = text
        if not rel.startswith(".."):
            text = rel
    return text.replace("\\", "/")


def remember_path(bucket: Set[str], path: str, repo: str = "") -> None:
    normalized = normalize_working_path(path, repo)
    if normalized:
        bucket.add(normalized)


def working_set_note(edited: Iterable[str], mentioned: Iterable[str], *, limit: int = 40) -> str:
    edited_list = sorted({item for item in edited if item})[:limit]
    mentioned_list = sorted({item for item in mentioned if item and item not in edited_list})[:limit]
    if not edited_list and not mentioned_list:
        return ""
    lines = ["Working set still tracked after compact:"]
    for path in edited_list:
        lines.append("- edited: %s" % path)
    for path in mentioned_list:
        lines.append("- mentioned: %s" % path)
    return "\n".join(lines)


def merge_working_set_into_history(history: List[dict], note: str) -> None:
    """Keep one continuity block after the system prompt. Never duplicates."""
    if not note or not isinstance(history, list) or not history:
        return
    marker = "Working set still tracked after compact:"
    for index, message in enumerate(history):
        if not isinstance(message, dict):
            continue
        if marker in str(message.get("content") or ""):
            history[index] = dict(message, content=note, role=message.get("role") or "user")
            return
    insert_at = 1 if str((history[0] or {}).get("role") or "") == "system" else 0
    history.insert(insert_at, {"role": "user", "content": note})
