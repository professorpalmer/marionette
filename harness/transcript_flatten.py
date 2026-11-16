"""History as flat text, for the compaction summarizer and completion pilots.

Flat text marks each turn with a role header such as ``USER:``. Tool output and
harness notes can hold a line that looks like such a header, and a reader then
takes that line as words from the user. Only a request that the human typed
keeps its lines as they are. Every tool result gets its own header.
"""
from __future__ import annotations

import re

from .working_set import is_user_request

_ROLE_HEADER = re.compile(
    r"(?im)^(?=[ \t]*\[?[ \t]*(?:user|human|assistant|system|tool)\b[^\n:]{0,40}:)"
)


def defuse_role_headers(text: str) -> str:
    """Put ``| `` before each line that starts like a role header."""
    return _ROLE_HEADER.sub("| ", text)


def flatten_message(message: dict) -> str:
    role = str(message.get("role") or "user").upper()
    content = message.get("content") or ""
    calls = message.get("tool_calls") or []
    if calls:
        call_text = [
            f"({(tc.get('function') or {}).get('name')} with arguments "
            f"{(tc.get('function') or {}).get('arguments')})"
            for tc in calls
        ]
        content = (str(content) + "\n" + "\n".join(call_text)).strip()
    if message.get("role") == "tool" or message.get("_action_result"):
        tc_id = message.get("tool_call_id") or ""
        role = f"TOOL RESULT ({tc_id})" if tc_id else "TOOL RESULT"
    if isinstance(content, str) and not is_user_request(message):
        content = defuse_role_headers(content)
    return f"{role}: {content}"
