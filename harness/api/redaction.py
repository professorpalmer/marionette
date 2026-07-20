"""Redact obvious secrets from peeled HTTP list/status JSON responses."""

from __future__ import annotations

import re
from typing import Any

_REDACTED = "REDACTED"
_SECRET_KV_RE = re.compile(
    r"(?i)((?:api[_-]?key|secret|password|token|bearer|authorization)\s*[=:]\s*)(\S+)"
)


def _redact_string(text: str) -> str:
    if not text:
        return text
    return _SECRET_KV_RE.sub(rf"\1{_REDACTED}", text)


def redact_api_secrets(value: Any) -> Any:
    """Deep-copy redaction aligned with ``redact_mcp_secrets`` env/headers handling."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in ("env", "headers") and isinstance(item, dict):
                out[key] = {k: _REDACTED for k in item}
            elif key in ("env", "headers") and item:
                out[key] = _REDACTED
            elif isinstance(item, str):
                out[key] = _redact_string(item)
            else:
                out[key] = redact_api_secrets(item)
        return out
    if isinstance(value, list):
        return [redact_api_secrets(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value
