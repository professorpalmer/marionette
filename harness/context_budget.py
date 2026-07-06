import os
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Tuple, List, Dict, Optional, Any

logger = logging.getLogger(__name__)

PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"


def byte_size(content: str) -> int:
    """Return the UTF-8 byte length of content (multibyte-aware sizing)."""
    if not isinstance(content, str):
        content = "" if content is None else str(content)
    return len(content.encode("utf-8"))


def truncate_bytes(content: str, max_bytes: int) -> str:
    """Truncate content to at most max_bytes UTF-8 bytes without splitting a
    multibyte character. Always returns valid text."""
    if max_bytes <= 0:
        return ""
    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content
    # Decode the leading max_bytes, dropping any partial trailing character.
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def content_hash(content: str, length: int = 12) -> str:
    """Stable short hex hash of content for dedupe of identical large outputs."""
    if not isinstance(content, str):
        content = "" if content is None else str(content)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return digest[:length]


def _default_max_result() -> int:
    try:
        return int(os.environ.get("HARNESS_MAX_TOOL_RESULT_CHARS", "8000"))
    except ValueError:
        return 8000


def _default_turn_budget() -> int:
    try:
        return int(os.environ.get("HARNESS_TURN_BUDGET_CHARS", "48000"))
    except ValueError:
        return 48000


@dataclass(frozen=True)
class BudgetConfig:
    max_result_chars: int = field(default_factory=_default_max_result)
    turn_budget_chars: int = field(default_factory=_default_turn_budget)
    preview_chars: int = 1500


def generate_preview(
    content: str,
    max_chars: int = 1500,
    head_tail: bool = False,
    tail_chars: Optional[int] = None,
) -> Tuple[str, bool]:
    """Truncate content to a preview. Returns (preview, has_more).

    Default (head_tail=False): keep the head, truncating at the last newline
    within max_chars. Backward-compatible with the original behavior.

    head_tail=True: keep the first N and last M lines/chars so the error at the
    end of command output is preserved. When tail_chars is None it defaults to
    a third of max_chars. The two halves are joined with an elision marker.
    """
    if len(content) <= max_chars:
        return content, False

    if not head_tail:
        truncated = content[:max_chars]
        last_nl = truncated.rfind("\n")
        if last_nl > max_chars // 2:
            truncated = truncated[:last_nl + 1]
        return truncated, True

    tail = tail_chars if tail_chars is not None else max(1, max_chars // 3)
    tail = min(tail, max_chars)
    head_budget = max_chars - tail

    head = content[:head_budget]
    head_nl = head.rfind("\n")
    if head_nl > head_budget // 2:
        head = head[:head_nl + 1]

    tail_part = content[-tail:] if tail > 0 else ""
    tail_nl = tail_part.find("\n")
    if 0 <= tail_nl < tail // 2:
        tail_part = tail_part[tail_nl + 1:]

    omitted = len(content) - len(head) - len(tail_part)
    marker = f"\n... [omitted {omitted:,} characters] ...\n"
    return head + marker + tail_part, True


def _looks_like_command_result(result_id: str) -> bool:
    """Heuristic: command-like results benefit from head+tail preview because
    the failure usually lands at the end of the output."""
    rid = (result_id or "").lower()
    return any(tok in rid for tok in ("command", "cmd", "shell", "bash", "exec", "run"))


def spill_to_disk(
    content: str,
    result_id: str,
    state_dir: str,
    dedupe: bool = False,
) -> str:
    """Write FULL content to {state_dir}/pmharness-results/{result_id}.txt.

    When dedupe is True a stable content-hash suffix is appended to the file
    name so identical large outputs collapse onto a single file on disk.
    """
    abs_state_dir = os.path.abspath(state_dir)
    target_dir = os.path.join(abs_state_dir, "pmharness-results")
    os.makedirs(target_dir, exist_ok=True)
    if dedupe:
        file_name = f"{result_id}-{content_hash(content)}.txt"
    else:
        file_name = f"{result_id}.txt"
    file_path = os.path.join(target_dir, file_name)
    with open(file_path, "w", encoding="utf-8", errors="replace") as f:
        f.write(content)
    # Forward slashes everywhere: the path is echoed to the model (which reads
    # it back via read_file), and Windows accepts both separators anyway.
    return file_path.replace(os.sep, "/")


def build_persisted_message(
    preview: str,
    has_more: bool,
    original_size: int,
    file_path: str,
) -> str:
    """Build the <persisted-output> replacement block.

    The preview is passed through verbatim; because generate_preview slices on
    character boundaries the preview never splits a multibyte character.
    """
    size_kb = original_size / 1024
    if size_kb >= 1024:
        size_str = f"{size_kb / 1024:.1f} MB"
    else:
        size_str = f"{size_kb:.1f} KB"

    head_tail = "... [omitted" in preview
    label = "head and tail" if head_tail else f"first {len(preview)} chars"

    msg = f"{PERSISTED_OUTPUT_TAG}\n"
    msg += f"This tool result was too large ({original_size:,} characters, {size_str}).\n"
    msg += f"Full output saved to: {file_path}\n"
    msg += "Use read_file with start_line and limit to read specific sections\n\n"
    msg += f"Preview ({label}):\n"
    msg += preview
    if has_more:
        msg += "\n..."
    msg += f"\n{PERSISTED_OUTPUT_CLOSING_TAG}"
    return msg


def maybe_persist_result(
    content: str,
    result_id: str,
    state_dir: str,
    config: BudgetConfig,
    threshold: Optional[int] = None,
    head_tail: Optional[bool] = None,
    dedupe: bool = False,
) -> str:
    """Layer 2: persist oversized result, return preview + path. Falls back to inline truncation if write fails.

    head_tail: when True the preview shows the first and last lines of the
    output; when None (default) a smart default is used that enables head+tail
    for command-like results so the trailing error is visible.
    dedupe: when True the persisted file name carries a content-hash suffix so
    identical large outputs share one file.
    """
    effective_threshold = threshold if threshold is not None else config.max_result_chars

    if len(content) <= effective_threshold:
        return content

    if head_tail is None:
        head_tail = _looks_like_command_result(result_id)

    preview, has_more = generate_preview(
        content, max_chars=config.preview_chars, head_tail=head_tail
    )

    try:
        file_path = spill_to_disk(content, result_id, state_dir, dedupe=dedupe)
        return build_persisted_message(preview, has_more, len(content), file_path)
    except Exception as e:
        logger.warning("Spill to disk failed for %s: %s", result_id, e)
        fallback_msg = (
            f"{preview}\n\n"
            f"[Truncated: tool response was {len(content):,} chars. "
            f"Full output could not be saved: {e}]"
        )
        return fallback_msg


def enforce_turn_budget(
    tool_messages: List[Dict[str, Any]],
    state_dir: str,
    config: BudgetConfig,
) -> List[Dict[str, Any]]:
    """Layer 3: enforce aggregate budget across all tool results in a turn."""
    candidates = []
    total_size = 0
    for i, msg in enumerate(tool_messages):
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = str(content) if content is not None else ""
        size = len(content)
        total_size += size
        if PERSISTED_OUTPUT_TAG not in content:
            candidates.append((i, size))

    if total_size <= config.turn_budget_chars:
        return tool_messages

    candidates.sort(key=lambda x: x[1], reverse=True)

    for idx, size in candidates:
        if total_size <= config.turn_budget_chars:
            break
        msg = tool_messages[idx]
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = str(content) if content is not None else ""
        
        tc_id = msg.get("tool_call_id") or f"turn_budget_{idx}"

        replacement = maybe_persist_result(
            content=content,
            result_id=tc_id,
            state_dir=state_dir,
            config=config,
            threshold=0,
        )
        if replacement != content:
            total_size -= size
            total_size += len(replacement)
            tool_messages[idx]["content"] = replacement
            logger.info("Turn budget enforcement: persisted tool result %s (%d chars)", tc_id, size)

    return tool_messages
