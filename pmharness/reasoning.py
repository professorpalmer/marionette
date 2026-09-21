from __future__ import annotations
import re

from pmharness.stream_snapshot import collapse_repeated_thought_blocks


def _thought_key(text: str) -> str:
    return collapse_repeated_thought_blocks(text).strip()


def extract_reasoning(message: dict | object) -> str:
    """
    Extract reasoning/thinking content from an assistant message.
    Accepts either a dict (decoded from JSON) or an object (e.g. from an SDK).
    Returns "" if no reasoning is found.
    """
    reasoning_parts = []

    def get_val(obj, key):
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    reasoning = get_val(message, "reasoning")
    if reasoning:
        reasoning_parts.append(str(reasoning))

    reasoning_content = get_val(message, "reasoning_content")
    if reasoning_content:
        rc_str = str(reasoning_content)
        if _thought_key(rc_str) not in [_thought_key(part) for part in reasoning_parts]:
            reasoning_parts.append(rc_str)

    reasoning_details = get_val(message, "reasoning_details")
    if reasoning_details and isinstance(reasoning_details, list):
        for detail in reasoning_details:
            if isinstance(detail, dict):
                summary = (
                    detail.get("summary")
                    or detail.get("thinking")
                    or detail.get("content")
                    or detail.get("text")
                )
                if summary:
                    summary_str = str(summary)
                    if _thought_key(summary_str) not in [
                        _thought_key(part) for part in reasoning_parts
                    ]:
                        reasoning_parts.append(summary_str)

    content = get_val(message, "content")
    if not reasoning_parts:
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "thinking":
                    thinking_text = block.get("thinking") or block.get("text") or ""
                    thinking_text = thinking_text.strip()
                    if thinking_text and thinking_text not in reasoning_parts:
                        reasoning_parts.append(thinking_text)
        elif isinstance(content, str) and content:
            inline_patterns = (
                r"<think>(.*?)</think>",
                r"<thinking>(.*?)</thinking>",
                r"<thought>(.*?)</thought>",
                r"<reasoning>(.*?)</reasoning>",
                r"<REASONING_SCRATCHPAD>(.*?)</REASONING_SCRATCHPAD>",
            )
            for pattern in inline_patterns:
                flags = re.DOTALL | re.IGNORECASE
                for block in re.findall(pattern, content, flags=flags):
                    cleaned = block.strip()
                    if cleaned and _thought_key(cleaned) not in [
                        _thought_key(part) for part in reasoning_parts
                    ]:
                        reasoning_parts.append(cleaned)

    if reasoning_parts:
        return collapse_repeated_thought_blocks("\n\n".join(reasoning_parts))
    return ""


def strip_think_blocks(content: str) -> str:
    """Strip any inline thinking tags from string content."""
    if not content:
        return ""
    inline_patterns = (
        r"<think>(.*?)</think>",
        r"<thinking>(.*?)</thinking>",
        r"<thought>(.*?)</thought>",
        r"<reasoning>(.*?)</reasoning>",
        r"<REASONING_SCRATCHPAD>(.*?)</REASONING_SCRATCHPAD>",
    )
    for pattern in inline_patterns:
        content = re.sub(pattern, "", content, flags=re.DOTALL | re.IGNORECASE)
    return content.strip()
