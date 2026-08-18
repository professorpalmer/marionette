from __future__ import annotations

"""Experimental compaction residual representations (Wave 2 lab seam).

``HARNESS_COMPACTION_RESIDUAL`` selects how the compacted middle is rewritten:

- ``summary`` (default): existing LLM / extractive four-heading snapshot
- ``catalog``: deterministic unique-handle index from the pruned middle
- ``hybrid``: real LLM four-heading summary plus a capped unique-handle index
- ``off``: test-only no-compaction ceiling (must be an explicit value)

Empty, missing, or unknown values resolve to ``summary`` — never to ``off``
or ``hybrid``. Catalog is extractive and skips the summarizer. Hybrid runs
the existing LLM summarizer path; timeout / degenerate / insufficient
reduction fall back to the extractive four-heading body plus handle index.
Neither path invents turn IDs or peek offsets. Text copied into a residual
is redacted for likely secrets.
"""

import os
import re
from typing import Any, Iterable

from harness.api.redaction import redact_secret_text

_SPILL_URI_RE = re.compile(r"spill://[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


def _min_summary_seed_chars() -> int:
    """Lazy read so this module cannot import-cycle the mixin."""
    try:
        from harness.compaction_mixin import MIN_SUMMARY_SEED_CHARS
        return int(MIN_SUMMARY_SEED_CHARS)
    except Exception:
        return 200

RESIDUAL_SUMMARY = "summary"
RESIDUAL_CATALOG = "catalog"
RESIDUAL_HYBRID = "hybrid"
RESIDUAL_OFF = "off"

VALID_RESIDUAL_MODES = frozenset({
    RESIDUAL_SUMMARY,
    RESIDUAL_CATALOG,
    RESIDUAL_HYBRID,
    RESIDUAL_OFF,
})

CATALOG_HEADING = "## Handle catalog"
HYBRID_INDEX_HEADING = "## Unique handles"

_PATH_RE = re.compile(
    r"(?:[A-Za-z]:)?(?:[\w.-]+/)+\w[\w.-]*\.[A-Za-z0-9]+"
)
_ARTIFACT_URI_RE = re.compile(r"artifact://[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
_JOB_URI_RE = re.compile(r"job://[A-Za-z0-9._-]+")
# Optional leading "- " so catalog bullets still match on re-extract.
_ERROR_RE = re.compile(
    r"(?im)^\s*(?:-\s+)?(?:error|exception|failed|traceback|fatal)[:\s].+$"
)
_STEM_RE = re.compile(
    r"(?im)^\s*(?:-\s+)?(?:decision|decided|constraint|must not|never|require[sd]?)[:\s].+$"
)
_LEADING_BULLET_RE = re.compile(r"^\s*-\s+")
_CATALOG_SECTION_RE = re.compile(
    r"^###\s+(Files|Tools|Handles|Stems|Last ask)\s*$",
    re.IGNORECASE,
)
_HYBRID_LIST_RE = re.compile(
    r"^-\s+(files(?:\s*\(\d+\))?|tools|uris|stems)\s*:\s*(.*)$",
    re.IGNORECASE,
)
# Split a hybrid stems line only at a new decision/constraint/error prefix so
# an inner "; " inside one stem is not treated as a list separator.
_STEM_SPLIT_RE = re.compile(
    r";\s+(?=(?:decision|decided|constraint|must not|never|"
    r"require[sd]?|error|exception|failed|traceback|fatal)[:\s])",
    re.IGNORECASE,
)
_PLACEHOLDER_HANDLES = frozenset({
    "(no file pointers found)",
    "(no tool names found)",
    "(no durable handles found)",
    "(no error/decision/constraint stems)",
    "(no open user ask captured)",
    "(none)",
})

_MAX_FILES = 24
_MAX_TOOLS = 16
_MAX_HANDLES = 16
_MAX_STEMS = 10
_STEM_CHARS = 160
_LAST_ASK_CHARS = 240
_HYBRID_INDEX_FILES = 8
_HYBRID_INDEX_HANDLES = 8
_HYBRID_INDEX_TOOLS = 8
_HYBRID_INDEX_STEMS = 6
_HYBRID_INDEX_CHARS = 800


def compaction_residual_mode() -> str:
    """Return the residual representation. Empty / invalid -> ``summary``."""
    try:
        raw = (os.environ.get("HARNESS_COMPACTION_RESIDUAL") or "").strip().lower()
    except Exception:
        return RESIDUAL_SUMMARY
    if not raw:
        return RESIDUAL_SUMMARY
    if raw in VALID_RESIDUAL_MODES:
        return raw
    return RESIDUAL_SUMMARY


def _unique_append(dst: list[str], seen: set[str], value: str, cap: int) -> None:
    text = redact_secret_text((value or "").strip())
    if not text or text in seen or len(dst) >= cap:
        return
    seen.add(text)
    dst.append(text)


def _without_leading_bullet(text: str) -> str:
    return _LEADING_BULLET_RE.sub("", (text or "").strip(), count=1).strip()


def _is_placeholder_handle(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return True
    if text in _PLACEHOLDER_HANDLES:
        return True
    lowered = text.lower()
    return lowered.startswith("(no ") and lowered.endswith(")")


def _error_stem(match: str) -> str:
    """Keep first-pass 'error: ' prefix without doubling catalog bullets."""
    body = _without_leading_bullet(match)
    if body.lower().startswith("error:"):
        return body[: len("error: ") + _STEM_CHARS]
    return "error: " + body[:_STEM_CHARS]


def _split_classified_list(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text or _is_placeholder_handle(text):
        return []
    sep = ";" if ";" in text else ","
    items: list[str] = []
    for part in text.split(sep):
        item = part.strip()
        if item and not _is_placeholder_handle(item):
            items.append(item)
    return items


def _split_stem_list(raw: str) -> list[str]:
    """Split a hybrid stems line without breaking stems that contain '; '."""
    text = (raw or "").strip()
    if not text or _is_placeholder_handle(text):
        return []
    items: list[str] = []
    for part in _STEM_SPLIT_RE.split(text):
        item = part.strip()
        if item and not _is_placeholder_handle(item):
            items.append(item)
    return items


def _ingest_catalog_sections(
    text: str,
    files: list[str],
    seen_files: set[str],
    tools: list[str],
    seen_tools: set[str],
    handles: list[str],
    seen_handles: set[str],
    stems: list[str],
    seen_stems: set[str],
) -> None:
    start = text.find(CATALOG_HEADING)
    if start < 0:
        return
    section = ""
    for raw_line in text[start:].splitlines():
        heading = _CATALOG_SECTION_RE.match(raw_line.strip())
        if heading:
            section = heading.group(1).lower()
            if section == "last ask":
                section = ""
            continue
        if raw_line.strip().startswith("## ") and CATALOG_HEADING not in raw_line:
            section = ""
            continue
        if not section or not raw_line.lstrip().startswith("- "):
            continue
        item = _without_leading_bullet(raw_line)
        if _is_placeholder_handle(item):
            continue
        if section == "files":
            _unique_append(files, seen_files, item, _MAX_FILES)
        elif section == "tools":
            _unique_append(tools, seen_tools, item, _MAX_TOOLS)
        elif section == "handles":
            _unique_append(handles, seen_handles, item, _MAX_HANDLES)
        elif section == "stems":
            _unique_append(stems, seen_stems, item, _MAX_STEMS)


def _ingest_hybrid_lists(
    text: str,
    files: list[str],
    seen_files: set[str],
    tools: list[str],
    seen_tools: set[str],
    handles: list[str],
    seen_handles: set[str],
    stems: list[str],
    seen_stems: set[str],
) -> None:
    start = text.find(HYBRID_INDEX_HEADING)
    if start < 0:
        return
    for raw_line in text[start:].splitlines():
        parsed = _HYBRID_LIST_RE.match(raw_line.strip())
        if not parsed:
            continue
        kind = parsed.group(1).split("(", 1)[0].strip().lower()
        raw_items = parsed.group(2)
        if kind == "files":
            for item in _split_classified_list(raw_items):
                _unique_append(files, seen_files, item, _MAX_FILES)
        elif kind == "tools":
            for item in _split_classified_list(raw_items):
                _unique_append(tools, seen_tools, item, _MAX_TOOLS)
        elif kind == "uris":
            for item in _split_classified_list(raw_items):
                _unique_append(handles, seen_handles, item, _MAX_HANDLES)
        elif kind == "stems":
            for item in _split_stem_list(raw_items):
                _unique_append(stems, seen_stems, item, _MAX_STEMS)


def _ingest_structured_residual(
    text: str,
    files: list[str],
    seen_files: set[str],
    tools: list[str],
    seen_tools: set[str],
    handles: list[str],
    seen_handles: set[str],
    stems: list[str],
    seen_stems: set[str],
) -> None:
    """Re-ingest already-classified catalog / hybrid residual text."""
    if CATALOG_HEADING in text:
        _ingest_catalog_sections(
            text,
            files,
            seen_files,
            tools,
            seen_tools,
            handles,
            seen_handles,
            stems,
            seen_stems,
        )
    if HYBRID_INDEX_HEADING in text:
        _ingest_hybrid_lists(
            text,
            files,
            seen_files,
            tools,
            seen_tools,
            handles,
            seen_handles,
            stems,
            seen_stems,
        )


def _iter_message_text(message: dict) -> Iterable[str]:
    content = message.get("content")
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                yield block
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    yield text
    read_path = message.get("_read_path")
    if isinstance(read_path, str) and read_path.strip():
        yield read_path
    for tc in message.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        func = tc.get("function") or {}
        name = func.get("name")
        if isinstance(name, str):
            yield name
        args = func.get("arguments")
        if isinstance(args, str):
            yield args


def extract_handle_index(middle_block: list[dict]) -> dict[str, Any]:
    """Collect unique files / tools / handles / stems from a pruned middle.

    O(unique handles), not one row per message. Does not invent turn IDs.
    """
    files: list[str] = []
    tools: list[str] = []
    handles: list[str] = []
    stems: list[str] = []
    seen_files: set[str] = set()
    seen_tools: set[str] = set()
    seen_handles: set[str] = set()
    seen_stems: set[str] = set()
    last_ask = ""

    if not isinstance(middle_block, list):
        middle_block = []

    for message in middle_block:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "user" and not message.get("_compressed_summary"):
            raw_ask = str(message.get("content") or "").strip().split("\n", 1)[0]
            last_ask = redact_secret_text(raw_ask[:_LAST_ASK_CHARS])
        read_path = message.get("_read_path")
        if isinstance(read_path, str):
            _unique_append(files, seen_files, read_path, _MAX_FILES)
        for tc in message.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function") or {}
            name = func.get("name")
            if isinstance(name, str) and name.strip():
                _unique_append(tools, seen_tools, name.strip(), _MAX_TOOLS)
        for text in _iter_message_text(message):
            if not text:
                continue
            _ingest_structured_residual(
                text,
                files,
                seen_files,
                tools,
                seen_tools,
                handles,
                seen_handles,
                stems,
                seen_stems,
            )
            for match in _PATH_RE.findall(text):
                _unique_append(files, seen_files, match, _MAX_FILES)
            for match in _SPILL_URI_RE.findall(text):
                _unique_append(handles, seen_handles, match, _MAX_HANDLES)
            for match in _ARTIFACT_URI_RE.findall(text):
                _unique_append(handles, seen_handles, match, _MAX_HANDLES)
            for match in _JOB_URI_RE.findall(text):
                _unique_append(handles, seen_handles, match, _MAX_HANDLES)
            for match in _ERROR_RE.findall(text):
                _unique_append(stems, seen_stems, _error_stem(match), _MAX_STEMS)
            for match in _STEM_RE.findall(text):
                _unique_append(
                    stems,
                    seen_stems,
                    _without_leading_bullet(match)[:_STEM_CHARS],
                    _MAX_STEMS,
                )

    return {
        "files": files,
        "tools": tools,
        "handles": handles,
        "stems": stems,
        "last_ask": last_ask,
        "middle_messages": len(middle_block),
    }


def _bullet_block(title: str, items: list[str], empty: str) -> str:
    if not items:
        return f"{title}\n- {empty}\n"
    lines = "\n".join(f"- {item}" for item in items)
    return f"{title}\n{lines}\n"


def _clip(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 40:
        return text[:limit]
    return text[: max(0, limit - 40)].rstrip() + "\n... [truncated to fit budget]"


def _pad_catalog(summary: str, index: dict[str, Any], char_budget: int) -> str:
    if len(summary.strip()) >= _min_summary_seed_chars():
        return summary
    pad = (
        f"[Handle catalog: {len(index['files'])} files, "
        f"{len(index['tools'])} tools, {len(index['handles'])} handles "
        f"from {index['middle_messages']} middle messages. "
        "Unique-handle index, not a turn log. "
        "peek_history offsets are not implied by this catalog.]"
    )
    if index.get("last_ask"):
        pad += f" Last ask retained: {index['last_ask']}"
    combined = summary.rstrip() + "\n" + pad
    if len(combined) > char_budget:
        combined = _clip(combined, char_budget)
    return combined


def build_catalog_residual(
    middle_block: list[dict],
    *,
    char_budget: int,
) -> str:
    """Deterministic unique-handle catalog bounded by ``char_budget``."""
    char_budget = max(int(char_budget), _min_summary_seed_chars() + 160)
    index = extract_handle_index(middle_block)
    last_ask = index["last_ask"] or "(no open user ask captured)"
    summary = (
        f"{CATALOG_HEADING}\n"
        "Unique files, tools, and durable handles from the compacted middle. "
        "Not a per-message log.\n"
        + _bullet_block("### Files", index["files"], "(no file pointers found)")
        + _bullet_block("### Tools", index["tools"], "(no tool names found)")
        + _bullet_block("### Handles", index["handles"], "(no durable handles found)")
        + _bullet_block("### Stems", index["stems"], "(no error/decision/constraint stems)")
        + f"### Last ask\n- {last_ask}\n"
    )
    if len(summary) > char_budget:
        summary = _clip(summary, char_budget)
    return _pad_catalog(summary, index, char_budget)


def build_hybrid_index(
    middle_block: list[dict],
    *,
    max_chars: int = _HYBRID_INDEX_CHARS,
) -> str:
    """Small capped unique-handle appendix for the hybrid residual."""
    index = extract_handle_index(middle_block)
    files = index["files"][:_HYBRID_INDEX_FILES]
    tools = index["tools"][:_HYBRID_INDEX_TOOLS]
    handles = index["handles"][:_HYBRID_INDEX_HANDLES]
    stems = index["stems"][:_HYBRID_INDEX_STEMS]
    file_part = "; ".join(files) if files else "(none)"
    tool_part = ", ".join(tools) if tools else "(none)"
    handle_part = "; ".join(handles) if handles else "(none)"
    stem_part = "; ".join(stems) if stems else "(none)"
    text = (
        f"{HYBRID_INDEX_HEADING}\n"
        f"- files ({len(index['files'])}): {file_part}\n"
        f"- tools: {tool_part}\n"
        f"- uris: {handle_part}\n"
        f"- stems: {stem_part}\n"
    )
    if len(text) > max_chars:
        text = _clip(text, max_chars)
    return text


def append_handle_index(
    body: str,
    middle_block: list[dict],
    *,
    char_budget: int,
) -> str:
    """Append a capped handle index to an LLM or extractive snapshot."""
    char_budget = max(int(char_budget), _min_summary_seed_chars() + 160)
    index_budget = min(_HYBRID_INDEX_CHARS, max(80, char_budget // 4))
    index = build_hybrid_index(middle_block, max_chars=index_budget)
    body = (body or "").rstrip()
    if HYBRID_INDEX_HEADING in body:
        combined = body
    else:
        room = char_budget - len(index) - 1
        if room < _min_summary_seed_chars():
            index = _clip(index, max(40, char_budget - len(body) - 1))
            combined = body + "\n" + index
        else:
            if len(body) > room:
                body = _clip(body, room)
            combined = body + "\n" + index
    if len(combined) > char_budget:
        combined = _clip(combined, char_budget)
    return combined
