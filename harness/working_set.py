"""Compaction continuity: the user's requests and the touched files survive a compact.

Both live inside the residual message as reference sections. A standalone
user-role note read to the model as a new request ("Working set still
tracked after compact") and the model restarted its task from it.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, List, Optional, Set

REQUESTS_HEADING = "### User requests in this task (verbatim, oldest first)"
FILES_HEADING = "### Files in this task (reference only, not a request)"
LEGACY_NOTE_MARKER = "Working set still tracked after compact:"
_REQUEST_OPEN = "<<<request"
_REQUEST_CLOSE = ">>>"
_REQUEST_RE = re.compile(r"^<<<request\n(.*?)\n>>>$", re.S | re.M)
_MAX_REQUESTS = 3
_MAX_REQUEST_CHARS = 12000
_CONTINUE_LINE = (
    "The task continues. Work on the newest request from the point where the "
    "messages after this summary stop. Do not start it again."
)


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
    lines = [FILES_HEADING]
    for path in edited_list:
        lines.append("- edited: %s" % path)
    for path in mentioned_list:
        lines.append("- mentioned: %s" % path)
    return "\n".join(lines)


def is_legacy_note(message: dict) -> bool:
    return str(message.get("content") or "").startswith(LEGACY_NOTE_MARKER)


def is_user_request(message: dict) -> bool:
    """A request the human typed (send or steer), not a harness-injected user row."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    if message.get("_compressed_summary") or is_legacy_note(message):
        return False
    return bool(message.get("_user_request") or message.get("input_id") or message.get("input_ids"))


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        parts = [str(part.get("text") or "") for part in content
                 if isinstance(part, dict) and part.get("type") in {"text", "input_text"}]
        return "\n".join(part for part in parts if part).strip()
    return str(content or "").strip()


def carried_requests(residual: str) -> List[str]:
    """Requests an earlier residual pinned. Empty when it has no request section."""
    _, sep, section = str(residual or "").partition(REQUESTS_HEADING)
    if not sep:
        return []
    return [match.group(1) for match in _REQUEST_RE.finditer(section)]


def requests_for_residual(middle_block: List[dict]) -> List[str]:
    """The newest requests in the compacted span, carried requests included."""
    requests: List[str] = []
    for message in middle_block or []:
        if not isinstance(message, dict):
            continue
        if message.get("_compressed_summary"):
            requests.extend(carried_requests(str(message.get("content") or "")))
        elif is_user_request(message):
            text = _message_text(message)
            if text:
                requests.append(text)
    return requests[-_MAX_REQUESTS:]


def requests_block(requests: List[str]) -> str:
    """Newest request first gets the character budget; order stays oldest first."""
    room = _MAX_REQUEST_CHARS
    kept: List[str] = []
    for text in reversed(requests):
        if room <= 200:
            break
        if len(text) > room:
            text = text[: room - 40].rstrip() + "\n... [request clipped]"
        kept.append(text)
        room -= len(text)
    if not kept:
        return ""
    bodies = ["%s\n%s\n%s" % (_REQUEST_OPEN, text, _REQUEST_CLOSE) for text in reversed(kept)]
    return (REQUESTS_HEADING + "\n" + _CONTINUE_LINE + "\n" + "\n".join(bodies))


def _strip_files_section(content: str) -> str:
    head, sep, _ = content.partition("\n\n" + FILES_HEADING)
    return head if sep else content


def merge_working_set_into_history(history: List[dict], note: str) -> None:
    """Put the file list in the residual message. Never a separate user row."""
    if not isinstance(history, list) or not history:
        return
    history[:] = [m for m in history if not (isinstance(m, dict) and is_legacy_note(m))]
    residual: Optional[int] = None
    for index, message in enumerate(history):
        if isinstance(message, dict) and message.get("_compressed_summary"):
            residual = index
    if residual is None:
        return
    content = _strip_files_section(str(history[residual].get("content") or ""))
    if note:
        content = content.rstrip() + "\n\n" + note
    history[residual] = dict(history[residual], content=content)
