"""Hash-anchored file edits (stdlib-only, cross-platform).

read_file can emit stable short content tags; hash_edit applies replace/insert/delete
operations that verify anchors before writing. Stale anchors are rejected with no
partial writes. Line endings are normalized for hashing and preserved on write.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any, Literal, Optional

from .context_budget import content_hash

ANCHOR_OPEN = "[@anchor"
ANCHOR_CLOSE = "[@/anchor]"

OpKind = Literal["replace", "insert", "delete"]


def hash_edit_enabled() -> bool:
    """Feature flag: hash-anchored edits are opt-in via HARNESS_HASH_EDIT."""
    import os
    return os.environ.get("HARNESS_HASH_EDIT", "").strip().lower() in ("1", "true", "yes")


def normalize_newlines(text: str) -> str:
    """Normalize CRLF/CR to LF for stable cross-platform hashing."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def detect_eol_style(text: str) -> str:
    """Return the dominant line ending style in ``text``."""
    crlf = text.count("\r\n")
    lf_only = text.count("\n") - crlf
    if crlf > 0 and crlf >= lf_only:
        return "\r\n"
    return "\n"


def split_lines(text: str) -> list[str]:
    """Split normalized text into lines without trailing empty from final newline."""
    norm = normalize_newlines(text)
    if not norm:
        return []
    parts = norm.split("\n")
    if norm.endswith("\n"):
        parts.pop()
    return parts


def join_lines(lines: list[str], eol: str) -> str:
    """Join lines with ``eol``, matching read_file/write conventions."""
    if not lines:
        return ""
    body = "\n".join(lines)
    return body.replace("\n", eol)


def range_text(lines: list[str], start_line: int, end_line: int) -> str:
    """Extract inclusive 1-based line range as LF-normalized text for hashing."""
    if start_line < 1 or end_line < start_line:
        raise ValueError(f"invalid line range {start_line}-{end_line}")
    if start_line > len(lines):
        return ""
    end_line = min(end_line, len(lines))
    return "\n".join(lines[start_line - 1 : end_line])


def file_text(lines: list[str]) -> str:
    """Full file body as LF-normalized text for hashing."""
    return "\n".join(lines)


def compute_range_hash(lines: list[str], start_line: int, end_line: int) -> str:
    return content_hash(range_text(lines, start_line, end_line))


def compute_file_hash(lines: list[str]) -> str:
    return content_hash(file_text(lines))


def format_anchor_tag(
    *,
    kind: str,
    hash_value: str,
    start_line: int,
    end_line: int,
) -> str:
    return f"{ANCHOR_OPEN} {kind} hash={hash_value} lines={start_line}-{end_line}]"


def wrap_with_anchor(content: str, tag_line: str) -> str:
    """Wrap ``content`` with open/close anchor markers for read_file output."""
    if not content:
        return f"{tag_line}\n{ANCHOR_CLOSE}\n"
    if content.endswith("\n"):
        return f"{tag_line}\n{content}{ANCHOR_CLOSE}\n"
    return f"{tag_line}\n{content}\n{ANCHOR_CLOSE}\n"


def annotate_read_content(
    content: str,
    *,
    total_lines: int,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> str:
    """Add hash anchor tags to read_file output when hash_edit is enabled."""
    if not hash_edit_enabled():
        return content

    norm = normalize_newlines(content)
    range_header = ""
    payload = norm
    parsed_start = start_line
    parsed_end = end_line

    if payload.startswith("[lines "):
        nl = payload.find("\n")
        if nl >= 0:
            range_header = payload[: nl + 1]
            payload = payload[nl + 1 :]
            # Parse "[lines 3-6 of 10]"
            m = re.match(r"\[lines (\d+)-(\d+) of \d+\]", range_header.strip())
            if m:
                parsed_start = int(m.group(1))
                parsed_end = int(m.group(2))

    lines = split_lines(payload)
    s = parsed_start if parsed_start is not None else 1
    e = parsed_end if parsed_end is not None else total_lines
    kind = "range" if (parsed_start is not None or parsed_end is not None) else "file"
    if kind == "file":
        h = compute_file_hash(lines) if lines else content_hash("")
    else:
        h = compute_range_hash(lines, 1, max(1, len(lines))) if lines else content_hash("")

    tag = format_anchor_tag(kind=kind, hash_value=h, start_line=s, end_line=e)
    wrapped = wrap_with_anchor(payload if payload else "", tag)
    return range_header + wrapped


@dataclass
class HashEditOp:
    op: OpKind
    anchor: str = ""
    start_line: int = 0
    end_line: int = 0
    after_line: int = -1
    text: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "HashEditOp":
        op = (raw.get("op") or raw.get("kind") or "").strip().lower()
        if op not in ("replace", "insert", "delete"):
            raise ValueError(f"unknown hash_edit op: {op!r}")
        anchor = (raw.get("anchor") or raw.get("hash") or "").strip()
        start_line = int(raw.get("start_line") or raw.get("line") or 0)
        end_line = int(raw.get("end_line") or raw.get("start_line") or raw.get("line") or 0)
        after_line = int(raw.get("after_line") if raw.get("after_line") is not None else -1)
        text = raw.get("text")
        if text is None:
            text = raw.get("new_text") or raw.get("content") or ""
        return cls(
            op=op,  # type: ignore[arg-type]
            anchor=anchor,
            start_line=start_line,
            end_line=end_line,
            after_line=after_line,
            text=str(text),
        )


@dataclass
class ApplyResult:
    ok: bool
    message: str
    stale_anchors: list[str]
    applied_ops: int = 0


def _validate_op(op: HashEditOp, lines: list[str]) -> Optional[str]:
    """Return an error string if the op fails anchor validation."""
    if op.op in ("replace", "delete"):
        if op.start_line < 1:
            return f"{op.op}: start_line must be >= 1"
        if op.end_line < op.start_line:
            op.end_line = op.start_line
        if not op.anchor:
            return f"{op.op}: anchor hash is required"
        if op.start_line > len(lines):
            return f"{op.op}: start_line {op.start_line} beyond file ({len(lines)} lines)"
        actual = compute_range_hash(lines, op.start_line, op.end_line)
        if actual != op.anchor:
            return (
                f"stale anchor for {op.op} lines {op.start_line}-{op.end_line}: "
                f"expected {op.anchor}, found {actual}"
            )
        return None

    # insert
    if op.after_line < 0:
        return "insert: after_line is required (0 = before first line)"
    if op.after_line > len(lines):
        return f"insert: after_line {op.after_line} beyond file ({len(lines)} lines)"
    if op.anchor:
        if op.after_line == 0:
            expected = content_hash("") if not lines else compute_range_hash(lines, 1, 1)
        else:
            expected = compute_range_hash(lines, op.after_line, op.after_line)
        if op.anchor != expected:
            return (
                f"stale anchor for insert after line {op.after_line}: "
                f"expected {op.anchor}, found {expected}"
            )
    return None


def _apply_op(lines: list[str], op: HashEditOp) -> None:
    """Apply a single validated op to ``lines`` in place."""
    if op.op == "replace":
        end = min(op.end_line, len(lines))
        new_lines = split_lines(normalize_newlines(op.text))
        lines[op.start_line - 1 : end] = new_lines
    elif op.op == "delete":
        end = min(op.end_line, len(lines))
        del lines[op.start_line - 1 : end]
    elif op.op == "insert":
        insert_at = op.after_line
        new_lines = split_lines(normalize_newlines(op.text))
        if insert_at == 0:
            lines[:0] = new_lines
        else:
            lines[insert_at:insert_at] = new_lines


def apply_hash_edits(
    original_text: str,
    ops: list[HashEditOp],
) -> tuple[str, ApplyResult]:
    """Validate all ops against ``original_text``, then apply in memory.

    Returns (new_text, result). On failure ``new_text`` is the unchanged
    original and nothing should be written.
    """
    eol = detect_eol_style(original_text)
    lines = split_lines(normalize_newlines(original_text))
    stale: list[str] = []
    errors: list[str] = []

    for i, op in enumerate(ops):
        err = _validate_op(op, lines)
        if err:
            errors.append(f"op[{i}]: {err}")
            if "stale anchor" in err and op.anchor:
                stale.append(op.anchor)

    if errors:
        return original_text, ApplyResult(
            ok=False,
            message="; ".join(errors),
            stale_anchors=stale,
        )

    # Apply in reverse line order so earlier line numbers stay valid during mutation.
    indexed = list(enumerate(ops))

    def sort_key(item: tuple[int, HashEditOp]) -> tuple[int, int]:
        _, op = item
        if op.op == "insert":
            return (op.after_line, 0)
        return (op.start_line, 1)

    for _, op in sorted(indexed, key=sort_key, reverse=True):
        _apply_op(lines, op)

    new_text = join_lines(lines, eol)
    # Preserve trailing newline semantics from the original.
    norm_orig = normalize_newlines(original_text)
    if norm_orig.endswith("\n") and not new_text.endswith(eol):
        new_text += eol
    elif not norm_orig.endswith("\n") and new_text.endswith(eol):
        new_text = new_text[: -len(eol)]

    return new_text, ApplyResult(
        ok=True,
        message=f"applied {len(ops)} op(s)",
        stale_anchors=[],
        applied_ops=len(ops),
    )


def atomic_write_text(path: str, content: str) -> None:
    """Write ``content`` atomically via temp file + replace."""
    target_dir = os.path.dirname(path) or "."
    os.makedirs(target_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=target_dir, prefix=".tmp-hash-edit-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def apply_hash_edits_to_file(path: str, ops: list[HashEditOp]) -> ApplyResult:
    """Read, validate, and atomically write hash-anchored edits to ``path``."""
    if not os.path.exists(path):
        return ApplyResult(ok=False, message=f"file not found: {path}", stale_anchors=[])
    if os.path.isdir(path):
        return ApplyResult(ok=False, message=f"path is a directory: {path}", stale_anchors=[])

    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        original = f.read()

    new_text, result = apply_hash_edits(original, ops)
    if not result.ok:
        return result

    atomic_write_text(path, new_text)
    return result
