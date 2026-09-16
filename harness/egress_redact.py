"""Single choke for secrets leaving the machine (logs, crash, feedback, export)."""
from __future__ import annotations

import re
from typing import Any, Iterable, List, Tuple

REDACTED = "***"

# Each kind is a named compiled pattern. A regex error fail-closes the payload.
_KIND_SPECS: Tuple[Tuple[str, str], ...] = (
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ("pem", r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----[\s\S]+?-----END (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    ("basic_auth_url", r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"),
    ("postgres_url", r"(?i)\bpostgres(?:ql)?://[^\s]+"),
    ("mysql_url", r"(?i)\bmysql://[^\s]+"),
    ("mongodb_url", r"(?i)\bmongodb(?:\+srv)?://[^\s]+"),
    ("redis_url", r"(?i)\bredis://[^\s]+"),
    ("amqp_url", r"(?i)\bamqps?://[^\s]+"),
    ("sk_ant", r"\bsk-ant-[A-Za-z0-9_-]{8,}\b"),
    ("sk_star", r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    ("stripe_live", r"\bsk_live_[A-Za-z0-9]{10,}\b"),
    ("google_api", r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    ("github_pat", r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ("npm", r"\bnpm_[A-Za-z0-9]{20,}\b"),
    ("slack", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ("aws_access", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ("aws_secret", r"(?i)(?:aws_secret_access_key|secret[_-]?access[_-]?key)\s*[=:]\s*[A-Za-z0-9/+=]{30,}"),
)


def _compile_kinds() -> List[Tuple[str, re.Pattern[str]]]:
    compiled: List[Tuple[str, re.Pattern[str]]] = []
    for name, spec in _KIND_SPECS:
        compiled.append((name, re.compile(spec)))
    return compiled


try:
    SECRET_KINDS = _compile_kinds()
except re.error:  # pragma: no cover — fail-closed if the table itself is broken
    SECRET_KINDS = []
    _TABLE_BROKEN = True
else:
    _TABLE_BROKEN = False


def secret_kind_names() -> Tuple[str, ...]:
    return tuple(name for name, _spec in _KIND_SPECS)


def redact_egress_text(text: str) -> str:
    """Rewrite every known secret kind to ``***``. Fail-closed on regex error."""
    if text is None:
        return ""
    if _TABLE_BROKEN:
        return REDACTED
    out = str(text)
    try:
        for _name, pattern in SECRET_KINDS:
            out = pattern.sub(REDACTED, out)
    except re.error:
        return REDACTED
    return out


def redact_egress(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact_egress(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_egress(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_egress(item) for item in value)
    if isinstance(value, str):
        return redact_egress_text(value)
    return value


def assert_kinds_seeded(samples: Iterable[Tuple[str, str]]) -> None:
    """Test helper: each (kind, seeded_text) must disappear from the output."""
    for kind, seeded in samples:
        redacted = redact_egress_text(seeded)
        if REDACTED not in redacted or seeded in redacted:
            raise AssertionError("kind %s was not redacted" % kind)
