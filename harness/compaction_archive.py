from __future__ import annotations

"""Session-scoped durable archive of history elided by compaction.

``_maybe_compact_history`` rewrites live history to ``[system, summary] + tail``
and the subsequent transcript persist writes only that residual. This sidecar
keeps the elided middle retrievable for ``peek_history`` without changing the
normal transcript display file.

Layout (same containment as ``save_transcript``):
    ``{state_dir}/transcripts/{safe_session_id}.archive.json``

Writes are atomic UTF-8 JSON. Residual transcript persist must not touch this
file. Load/append/remove never raise — corrupt, foreign, or oversized files
fail closed. Repeated Compact Now must not grow the sidecar without limit:
append and load both apply oldest/newest retention under explicit message and
serialized-byte caps.
"""

import json
import os
from typing import Any, Optional


ARCHIVE_VERSION = 1
_ARCHIVE_SUFFIX = ".archive.json"

# Conservative retention: a normal first compaction of a few dozen turns
# stays under both caps. Repeated Compact Now drops the middle, not the
# oldest or newest retained rows, and records a synthetic truncation marker.
ARCHIVE_MAX_MESSAGES = 400
ARCHIVE_MAX_SERIALIZED_BYTES = 256 * 1024
# File-level fail-closed cap (indented envelope is larger than compact
# message JSON). Never json.load an unbounded sidecar.
ARCHIVE_LOAD_MAX_BYTES = 512 * 1024

ARCHIVE_TRUNCATION_FLAG = "_archive_truncated"
ARCHIVE_TRUNCATION_PREFIX = "[compaction-archive truncated:"


def safe_session_id(session_id: str) -> str:
    return "".join(c for c in (session_id or "") if c.isalnum() or c in ("-", "_"))


def compaction_archive_path(state_dir: str, session_id: str) -> str:
    safe_sid = safe_session_id(session_id)
    if not state_dir or not safe_sid:
        return ""
    return os.path.join(state_dir, "transcripts", f"{safe_sid}{_ARCHIVE_SUFFIX}")


def _copy_messages(messages: Any) -> list[dict]:
    copied: list[dict] = []
    if not isinstance(messages, list):
        return copied
    for raw in messages:
        if not isinstance(raw, dict):
            continue
        try:
            item = json.loads(json.dumps(raw, default=str))
        except Exception:
            item = {
                "role": str(raw.get("role") or ""),
                "content": raw.get("content") if isinstance(raw.get("content"), (str, list)) else str(raw.get("content") or ""),
            }
        if isinstance(item, dict):
            copied.append(item)
    return copied


def _serialized_message_bytes(messages: list[dict]) -> int:
    try:
        return len(json.dumps(messages, default=str).encode("utf-8"))
    except Exception:
        return ARCHIVE_MAX_SERIALIZED_BYTES + 1


def _fits_retention(messages: list[dict]) -> bool:
    return (
        len(messages) <= ARCHIVE_MAX_MESSAGES
        and _serialized_message_bytes(messages) <= ARCHIVE_MAX_SERIALIZED_BYTES
    )


def _truncation_marker(omitted: int) -> dict:
    omitted = max(0, int(omitted))
    return {
        "role": "system",
        "content": (
            f"{ARCHIVE_TRUNCATION_PREFIX} omitted {omitted} messages "
            f"to stay within {ARCHIVE_MAX_MESSAGES} messages / "
            f"{ARCHIVE_MAX_SERIALIZED_BYTES} serialized bytes]"
        ),
        ARCHIVE_TRUNCATION_FLAG: True,
    }


def retain_archive_messages(messages: Any) -> list[dict]:
    """Keep oldest + newest rows under both caps. No-op for small archives.

    When rows must be dropped, a single synthetic marker is inserted between
    the retained prefix and suffix. Prior markers are stripped so repeated
    appends do not accumulate truncation rows.
    """
    cleaned = [
        item for item in _copy_messages(messages)
        if not item.get(ARCHIVE_TRUNCATION_FLAG)
    ]
    if _fits_retention(cleaned):
        return cleaned
    n = len(cleaned)
    if n == 0:
        return []

    # Leave one slot for the marker. Prefer keeping more real rows, then
    # shrink until the serialized-byte cap also fits.
    max_keep = max(0, min(n - 1, ARCHIVE_MAX_MESSAGES - 1))
    for keep in range(max_keep, -1, -1):
        oldest_n = keep // 2
        newest_n = keep - oldest_n
        omitted = n - oldest_n - newest_n
        head = cleaned[:oldest_n]
        tail = cleaned[n - newest_n:] if newest_n else []
        result = head + [_truncation_marker(omitted)] + tail
        if _fits_retention(result):
            return result

    marker = [_truncation_marker(n)]
    return marker if _fits_retention(marker) else []


def _atomic_write_json(path: str, data: dict) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_archive_document(state_dir: str, session_id: str) -> Optional[dict]:
    path = compaction_archive_path(state_dir, session_id)
    if not path or not os.path.isfile(path):
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size > ARCHIVE_LOAD_MAX_BYTES:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != ARCHIVE_VERSION:
        return None
    messages = data.get("messages")
    if messages is not None and not isinstance(messages, list):
        return None
    return data


def load_compaction_archive_messages(state_dir: str, session_id: str) -> list[dict]:
    """Return archived elided rows. Missing, corrupt, or oversized files yield ``[]``."""
    try:
        data = _load_archive_document(state_dir, session_id)
        if data is None:
            return []
        messages = _copy_messages(data.get("messages") or [])
        # Already-bounded documents keep their truncation marker. Re-retain
        # only when a pre-cap sidecar still exceeds the live limits.
        if _fits_retention(messages):
            return messages
        return retain_archive_messages(messages)
    except Exception:
        return []


def append_compaction_archive(
    state_dir: str,
    session_id: str,
    messages: Any,
) -> bool:
    """Append elided rows before a history rewrite. Never raises.

    Subsequent residual transcript writes use a different filename and must
    not replace this sidecar. A later compaction appends; it does not replace
    earlier elided rows, but both append and load apply retention so the
    sidecar cannot grow without limit.
    """
    try:
        safe_sid = safe_session_id(session_id)
        if not state_dir or not safe_sid:
            return False
        incoming = _copy_messages(messages)
        if not incoming:
            return False
        existing = _load_archive_document(state_dir, session_id)
        prior = _copy_messages((existing or {}).get("messages") or [])
        retained = retain_archive_messages(prior + incoming)
        if not retained:
            return False
        payload = {
            "version": ARCHIVE_VERSION,
            "session_id": safe_sid,
            "messages": retained,
            "truncated": any(item.get(ARCHIVE_TRUNCATION_FLAG) for item in retained),
        }
        _atomic_write_json(compaction_archive_path(state_dir, safe_sid), payload)
        return True
    except Exception:
        return False


def remove_compaction_archive(state_dir: str, session_id: str) -> None:
    """Delete the session archive (and a leftover tmp). Never raises."""
    try:
        path = compaction_archive_path(state_dir, session_id)
        if not path:
            return
        trans_dir = os.path.abspath(os.path.join(state_dir, "transcripts"))
        for candidate in (path, path + ".tmp"):
            abs_path = os.path.abspath(candidate)
            if not abs_path.startswith(trans_dir):
                continue
            if os.path.exists(abs_path):
                try:
                    os.remove(abs_path)
                except OSError:
                    pass
    except Exception:
        return
