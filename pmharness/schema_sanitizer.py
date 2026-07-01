"""Tool-call JSON schema sanitizer (pure layer, PM-free).

Adapted from Hermes tools/schema_sanitizer.py. Defensive, side-effect free,
stdlib-only. Helps weaker open-weights drivers whose tool-call arguments are
often not strictly valid JSON.

Public API:
    sanitize_tool_arguments(raw) -> (dict, error_reason)

The caller branches on the result: on success it gets ``(parsed_dict, "")``;
on failure it gets ``({}, reason_string)`` and can decide how to recover.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Tuple

__all__ = ["sanitize_tool_arguments"]


_FENCE_RE = re.compile(
    r"```(?:json|JSON)?\s*(?P<body>.*?)\s*```",
    re.DOTALL,
)

# Trailing comma before a closing } or ] (allowing whitespace between).
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")

# Unquoted object keys: { key: ... } or , key: ...
# Matches an identifier used as a key that is not already double-quoted.
_UNQUOTED_KEY_RE = re.compile(
    r"(?P<pre>[{,]\s*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<post>\s*:)"
)

# Scalar tokens that are obviously intended as JSON scalars but arrive as
# quoted strings.
_BOOL_TRUE = {"true", "True", "TRUE"}
_BOOL_FALSE = {"false", "False", "FALSE"}
_NULL = {"null", "None", "NULL", "nil"}


def _strip_fences(text: str) -> str:
    """Return the inner body of a fenced code block, if present."""
    match = _FENCE_RE.search(text)
    if match:
        return match.group("body").strip()
    # A lone leading/trailing set of backticks with no language tag.
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped[3:]
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    return stripped.strip()


def _strip_trailing_commas(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = _TRAILING_COMMA_RE.sub(r"\1", text)
    return text


def _quote_unquoted_keys(text: str) -> str:
    return _UNQUOTED_KEY_RE.sub(
        lambda m: '{pre}"{key}"{post}'.format(
            pre=m.group("pre"), key=m.group("key"), post=m.group("post")
        ),
        text,
    )


def _replace_single_quotes(text: str) -> str:
    """Best-effort conversion of single-quoted JSON to double-quoted JSON.

    Only applied when the text does not already contain double quotes as
    string delimiters, to avoid corrupting valid apostrophe-bearing content.
    """
    if '"' in text:
        return text
    return text.replace("'", '"')


def _coerce_scalar(value: Any) -> Any:
    """Coerce a stringified scalar to a real JSON scalar where obvious."""
    if not isinstance(value, str):
        return value
    token = value.strip()
    if token in _BOOL_TRUE:
        return True
    if token in _BOOL_FALSE:
        return False
    if token in _NULL:
        return None
    # Integer.
    if re.fullmatch(r"[+-]?\d+", token):
        try:
            return int(token)
        except ValueError:
            return value
    # Float.
    if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?", token):
        try:
            return float(token)
        except ValueError:
            return value
    return value


def _coerce_scalars(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {key: _coerce_scalars(val) for key, val in obj.items()}
    if isinstance(obj, list):
        return [_coerce_scalars(item) for item in obj]
    return _coerce_scalar(obj)


def _try_load(text: str) -> Any:
    return json.loads(text)


def sanitize_tool_arguments(raw: Any) -> Tuple[Dict[str, Any], str]:
    """Sanitize tool-call arguments into a dict.

    Accepts a dict, a JSON string, or a fenced ```json block. Returns a tuple
    ``(parsed_dict, error_reason)``. On success ``error_reason`` is the empty
    string; on failure the dict is empty and ``error_reason`` explains why.
    """
    # Already a dict: coerce obvious stringified scalars and pass through.
    if isinstance(raw, dict):
        return _coerce_scalars(raw), ""

    if raw is None:
        return {}, "input is None"

    if not isinstance(raw, str):
        return {}, "unsupported input type: {}".format(type(raw).__name__)

    text = raw.strip()
    if not text:
        return {}, "empty input"

    text = _strip_fences(text)
    if not text:
        return {}, "empty after fence strip"

    # Attempt 1: straight parse.
    candidates = [text]
    # Attempt 2: trailing commas removed.
    candidates.append(_strip_trailing_commas(text))
    # Attempt 3: unquoted keys quoted (plus trailing comma repair).
    step3 = _quote_unquoted_keys(_strip_trailing_commas(text))
    candidates.append(step3)
    # Attempt 4: single quotes converted (plus prior repairs).
    step4 = _replace_single_quotes(step3)
    step4 = _quote_unquoted_keys(_strip_trailing_commas(step4))
    candidates.append(step4)

    last_error = "no candidate parsed"
    for candidate in candidates:
        try:
            parsed = _try_load(candidate)
        except (ValueError, TypeError) as exc:
            last_error = str(exc)
            continue
        if isinstance(parsed, dict):
            return _coerce_scalars(parsed), ""
        # Parsed but not a dict (e.g. a bare list or scalar).
        return {}, "parsed value is not an object: {}".format(
            type(parsed).__name__
        )

    return {}, "could not parse arguments: {}".format(last_error)
