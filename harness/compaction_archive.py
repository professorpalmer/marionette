from __future__ import annotations

"""Session-scoped durable archive of history elided by compaction.

``_maybe_compact_history`` rewrites live history to ``[system, summary] + tail``
and the subsequent transcript persist writes only that residual. This sidecar
keeps the elided middle retrievable for ``peek_history`` without changing the
normal transcript display file.

Layout (same containment as ``save_transcript``):
    ``{state_dir}/transcripts/{safe_session_id}.archive.json``

Complete compactions publish a constant-size manifest pointing to immutable,
hash-addressed segments. Each segment is bounded by the existing message and
byte caps; reads validate the chain one segment at a time. Legacy v1 sidecars
remain readable. The legacy best-effort append API retains its explicit caps.
"""

from collections import deque
import json
import os
import hashlib
import tempfile
from typing import Any, Optional


ARCHIVE_VERSION = 1
_ARCHIVE_SUFFIX = ".archive.json"

# Conservative retention: a normal first compaction of a few dozen turns
# stays under both caps. Repeated Compact Now drops the middle, not the
# oldest or newest retained rows, and records a synthetic truncation marker.
ARCHIVE_MAX_MESSAGES = 400
ARCHIVE_MAX_SERIALIZED_BYTES = 8 * 1024 * 1024
# File-level fail-closed cap (indented envelope is larger than compact
# message JSON). Never json.load an unbounded sidecar.
ARCHIVE_LOAD_MAX_BYTES = 16 * 1024 * 1024

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


def json_digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()


def _atomic_write_json(path: str, data: Any) -> None:
    """Publish flushed JSON, sync the directory where supported, then read back."""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            directory = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        with open(path, encoding="utf-8") as handle:
            if json_digest(json.load(handle)) != json_digest(data):
                raise OSError("JSON persistence readback mismatch")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def verified_archive_digest(state_dir: str, session_id: str, generation: Optional[dict] = None, *, ancestor: str = "") -> str:
    data = generation if generation is not None else _load_archive_document(state_dir, session_id)
    if data is None:
        raise OSError("Compaction archive unavailable or corrupt")
    for _ in _archive_batches(state_dir, session_id, data, ancestor=ancestor):
        pass
    return json_digest(data)


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
    if data.get("version") not in (ARCHIVE_VERSION, 2):
        return None
    messages = data.get("messages")
    if messages is not None and not isinstance(messages, list):
        return None
    if data.get("session_id") != safe_session_id(session_id):
        return None
    if data.get("version") == 2:
        if set(data) != {"version", "session_id", "head", "total", "commit_id"}:
            return None
        return data
    if "digest" in data and data["digest"] != json_digest(messages):
        return None
    return data


def load_compaction_archive_messages(state_dir: str, session_id: str) -> list[dict]:
    """Return archived elided rows. Missing, corrupt, or oversized files yield ``[]``."""
    try:
        data = _load_archive_document(state_dir, session_id)
        if data is None:
            return []
        if data.get("version") == 2:
            first, total = load_compaction_archive_page(state_dir, session_id, limit=ARCHIVE_MAX_MESSAGES)
            if total <= len(first):
                return first
            half = max(0, (ARCHIVE_MAX_MESSAGES - 1) // 2)
            last, _ = load_compaction_archive_page(state_dir, session_id, offset=max(0, total - half), limit=half)
            rows = first[:half] + [_truncation_marker(total - len(first[:half]) - len(last))] + last
            while rows and not _fits_retention(rows):
                if not first and not last:
                    return []
                if len(first) > len(last):
                    first = first[:max(0, min(half, len(first)) - 1)]
                else:
                    last = last[1:]
                rows = first[:half] + [_truncation_marker(total - len(first[:half]) - len(last))] + last
            return rows
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
    *,
    require_complete: bool = False,
    commit_id: str = "",
) -> bool:
    """Append elided rows before a history rewrite. Never raises.

    Subsequent residual transcript writes use a different filename and must
    not replace this sidecar. A later compaction appends; it does not replace
    earlier elided rows. Complete writes use bounded immutable segments;
    legacy best-effort writes retain the historical retention behavior.
    """
    try:
        safe_sid = safe_session_id(session_id)
        if not state_dir or not safe_sid:
            return False
        incoming = _copy_messages(messages)
        if not incoming:
            return False
        existing = _load_archive_document(state_dir, session_id)
        if require_complete and existing is None and os.path.exists(
            compaction_archive_path(state_dir, session_id)
        ):
            return False
        if require_complete or (existing and existing.get("version") == 2):
            return _append_complete(state_dir, safe_sid, incoming, existing, commit_id)
        if commit_id and existing and existing.get("commit_id") == commit_id:
            return True
        prior = _copy_messages((existing or {}).get("messages") or [])
        retained = retain_archive_messages(prior + incoming)
        if not retained:
            return False
        payload = {
            "version": ARCHIVE_VERSION,
            "session_id": safe_sid,
            "commit_id": commit_id,
            "messages": retained,
            "digest": json_digest(retained),
            "truncated": any(item.get(ARCHIVE_TRUNCATION_FLAG) for item in retained),
        }
        _atomic_write_json(compaction_archive_path(state_dir, safe_sid), payload)
        return _load_archive_document(state_dir, safe_sid) == payload
    except Exception:
        return False


def _segment_path(state_dir: str, session_id: str, digest: str) -> str:
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise OSError("Invalid archive segment reference")
    return compaction_archive_path(state_dir, session_id) + ".segments/" + digest + ".json"


def _archive_batches(state_dir: str, session_id: str, document: dict, *, ancestor: str = ""):
    """Validate and yield newest-first batches, with bounded memory and reads."""
    sid = safe_session_id(session_id)
    if document.get("session_id") != sid:
        raise OSError("Foreign archive generation")
    if document.get("version") == 1:
        if ancestor:
            raise OSError("Archive generation is not visible")
        rows = document.get("messages") or []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise OSError("Invalid legacy archive rows")
        if "digest" in document and document["digest"] != json_digest(rows):
            raise OSError("Legacy archive digest mismatch")
        yield rows
        return
    if document.get("version") != 2:
        raise OSError("Invalid archive version")
    remaining = document.get("total")
    head = document.get("head")
    if type(remaining) is not int or remaining < 0:
        raise OSError("Invalid archive count")
    found = not ancestor
    while head:
        found = found or head == ancestor
        path = _segment_path(state_dir, sid, head)
        with open(path, "rb") as handle:
            raw = handle.read(ARCHIVE_LOAD_MAX_BYTES + 1)
        if len(raw) > ARCHIVE_LOAD_MAX_BYTES:
            raise OSError("Oversized archive segment")
        segment = json.loads(raw)
        if not isinstance(segment, dict) or json_digest(segment) != head:
            raise OSError("Archive segment digest mismatch")
        rows = segment.get("messages")
        if (segment.get("session_id") != sid or segment.get("version") != 2
                or segment.get("total") != remaining or not isinstance(rows, list)
                or not rows or any(not isinstance(row, dict) for row in rows)
                or not _fits_retention(rows)):
            raise OSError("Invalid archive segment")
        remaining -= len(rows)
        if remaining < 0:
            raise OSError("Invalid archive chain count")
        head = segment.get("parent")
        yield rows
    if remaining != 0:
        raise OSError("Incomplete archive chain")
    if not found:
        raise OSError("Archive generation is not visible")


def load_compaction_archive_page(state_dir: str, session_id: str, *,
                                 offset: int = 0, limit: int = 20,
                                 role: str = "") -> tuple[list[dict], int]:
    """Read an exact page; validate every segment before exposing any rows.

    Memory is bounded by one segment plus at most 400 selected rows. Corrupt
    or missing archives raise, allowing callers to distinguish refusal from
    an empty archive. Role-filtered offsets count only matching rows.
    """
    document = _load_archive_document(state_dir, session_id)
    if document is None:
        if os.path.exists(compaction_archive_path(state_dir, session_id)):
            raise OSError("Compaction archive unavailable or corrupt")
        return [], 0
    offset, limit = max(0, offset), max(0, min(ARCHIVE_MAX_MESSAGES, limit))
    def matches(row):
        return not role or str(row.get("role") or "").lower() == role
    if role:
        total = sum(sum(matches(row) for row in batch)
                    for batch in _archive_batches(state_dir, session_id, document))
    else:
        total = document.get("total") if document.get("version") == 2 else len(document.get("messages") or [])
        if type(total) is not int or total < 0:
            raise OSError("Invalid archive count")
    result = deque()
    result_bytes = 0
    end = total
    for batch in _archive_batches(state_dir, session_id, document):
        batch = [row for row in batch if matches(row)]
        start = end - len(batch)
        lo, hi = max(offset, start), min(offset + limit, end)
        if lo < hi:
            for row in reversed(batch[lo - start:hi - start]):
                size = _serialized_message_bytes([row])
                result.appendleft((row, size))
                result_bytes += size
                while result and result_bytes > ARCHIVE_MAX_SERIALIZED_BYTES:
                    result_bytes -= result.pop()[1]
        end = start
    return [row for row, _ in result], total


def _append_complete(state_dir: str, sid: str, incoming: list[dict],
                     existing: Optional[dict], commit_id: str) -> bool:
    # One segment stays under the message/byte caps. A fat Compact Now
    # (400+ tool turns, typical on local hosts) must still retain every
    # row — split across segments — rather than aborting archive_failed.
    # A single row that cannot fit a segment still fails closed.
    if existing is not None:
        verified_archive_digest(state_dir, sid, existing)
        if commit_id and existing.get("commit_id") == commit_id:
            return True
    head, total = "", 0
    if existing and existing.get("version") == 2:
        head, total = existing["head"], existing["total"]
    def publish(rows, parent, count):
        segment = {"version": 2, "session_id": sid, "parent": parent,
                   "total": count + len(rows), "messages": rows}
        digest = json_digest(segment)
        path = _segment_path(state_dir, sid, digest)
        if not os.path.exists(path):
            _atomic_write_json(path, segment)
        return digest, count + len(rows)

    def publish_batches(rows, parent, count):
        batch = []
        for row in rows:
            if not _fits_retention(batch + [row]):
                if not batch:
                    return None
                parent, count = publish(batch, parent, count)
                batch = []
            if not _fits_retention([row]):
                return None
            batch.append(row)
        if batch:
            parent, count = publish(batch, parent, count)
        return parent, count

    if existing and existing.get("version") == 1:
        migrated = publish_batches(list(existing.get("messages") or []), head, total)
        if migrated is None:
            return False
        head, total = migrated
    published = publish_batches(incoming, head, total)
    if published is None:
        return False
    head, total = published
    manifest = {"version": 2, "session_id": sid, "head": head,
                "total": total, "commit_id": commit_id}
    verified_archive_digest(state_dir, sid, manifest)
    _atomic_write_json(compaction_archive_path(state_dir, sid), manifest)
    return _load_archive_document(state_dir, sid) == manifest


def restore_archive_generation(state_dir: str, session_id: str, document: Optional[dict]) -> None:
    """Rollback only the published pointer; immutable orphan segments are safe."""
    path = compaction_archive_path(state_dir, session_id)
    if document is None:
        if os.path.exists(path):
            os.unlink(path)
            if os.name != "nt":
                directory = os.open(os.path.dirname(path), os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        return
    _atomic_write_json(path, document)


def remove_compaction_archive(state_dir: str, session_id: str) -> None:
    """Delete the session archive (and a leftover tmp). Never raises."""
    try:
        path = compaction_archive_path(state_dir, session_id)
        if not path:
            return
        import shutil
        shutil.rmtree(path + ".segments", ignore_errors=True)
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
