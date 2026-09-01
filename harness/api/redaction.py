"""Redact obvious secrets from peeled HTTP list/status JSON responses."""

from __future__ import annotations

import re
from hashlib import sha256
from typing import Any, TypedDict

_REDACTED = "REDACTED"
_MAX_LABEL = 40
_MAX_SUMMARY = 240
_SECRET_KEYS = {"env", "headers", "authorization", "cookie", "set-cookie", "access_token", "refresh_token", "client_secret", "api_key", "password", "secret", "token"}
_SECRET_KV_RE = re.compile(r"(?i)((?:api[_-]?key|secret|password|token|bearer|authorization)\s*[=:]\s*)(\S+)")
_URL_CREDENTIAL_RE = re.compile(r"(?i)([?&](?:api[_-]?key|access_token|refresh_token|client_secret|password|secret|token|key)=[^&#\s]*)")
_TOKENISH_RE = re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|Bearer\s+\S+|Basic\s+\S+)(?!\w)")
_AUTH_FAILURE_RE = re.compile(r"(?i)\b(authentication\s+(?:failed|failure)|unauthori[sz]ed|forbidden|credential(?:s)?\s+rejected)\b")
_HTTP_AUTH_STATUS_RE = re.compile(
    r"(?ix)(?:\bHTTP\s+|\bstatus\s*[:=]\s*)(?P<status>401|403)\b"
    r"|\b(?P<response_status>401|403)\s+(?:unauthori[sz]ed|forbidden)\b"
)


class AuthFailureAttribution(TypedDict, total=False):
    category: str
    consumer_kind: str
    consumer_id: str
    http_status: int
    remediation: str
    summary: str


def _redact_string(text: str) -> str:
    if not text:
        return text
    out = _URL_CREDENTIAL_RE.sub(lambda m: m.group(1).split("=", 1)[0] + "=" + _REDACTED, text)
    out = _SECRET_KV_RE.sub(rf"\1{_REDACTED}", out)
    return _TOKENISH_RE.sub(_REDACTED, out)


def redact_secret_text(text: str) -> str:
    """Redact obvious secrets from a single string (receipts, error lines)."""
    return _redact_string(text or "")


def redact_api_secrets(value: Any) -> Any:
    """Deep-copy redaction with case-insensitive handling of sensitive keys."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_name = key.casefold().replace("-", "_") if isinstance(key, str) else ""
            if key_name == "headers":
                out[key] = redact_api_secrets(item)
            elif key_name in {k.replace("-", "_") for k in _SECRET_KEYS}:
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


def _safe_label(value: Any) -> str:
    text = str(value or "")
    if re.search(r"(?i)https?://|[/\\?&#=]", text):
        return "label-" + sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    text = re.sub(r"[\x00-\x20\x7f]+", "-", text)
    text = re.sub(r"[^A-Za-z0-9_.:-]", "-", text).strip("-._:")
    return text[:_MAX_LABEL]


def build_auth_failure_attribution(consumer_kind: Any, consumer_id: Any, http_status: Any = None, error_text: Any = "") -> dict | None:
    """Build a bounded, privacy-safe attribution only for clear auth failures."""
    status = http_status if http_status in (401, 403) else None
    text = str(error_text or "")
    status_match = _HTTP_AUTH_STATUS_RE.search(text) if status is None else None
    if status_match:
        status = int(status_match.group("status") or status_match.group("response_status"))
    match = _AUTH_FAILURE_RE.search(text)
    if status is None and match:
        status = 403 if re.search(r"(?i)forbidden", match.group(0)) else 401
    if not match and status is None:
        return None
    forbidden = status == 403 or bool(re.search(r"(?i)forbidden", text))
    authentication = status == 401 or bool(re.search(r"(?i)authentication|credential(?:s)?\s+rejected", text))
    category = "forbidden" if forbidden else "authentication"
    summary = _redact_string(text).replace("\r", " ").replace("\n", " ")
    summary = re.sub(r"https?://[^\s]+", "[URL]", summary)[:_MAX_SUMMARY]
    result: AuthFailureAttribution = {"category": category, "consumer_kind": _safe_label(consumer_kind), "consumer_id": _safe_label(consumer_id), "remediation": "check_permissions" if forbidden else "reauthenticate", "summary": summary}
    if status is not None:
        result["http_status"] = status
    return dict(result)
