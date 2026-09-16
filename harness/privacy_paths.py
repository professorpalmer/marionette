"""Tool-layer forbidden file patterns. Prompt text cannot bypass this."""
from __future__ import annotations

import fnmatch
import json
import os
import re
from typing import Iterable, List, Optional, Sequence

from .secure_files import restrict_to_owner

FORBIDDEN_ENV = "MARIONETTE_FORBIDDEN_PATTERNS"
_CONFIG_NAME = "privacy.json"
_MAX_PATTERNS = 256
_MAX_PATTERN_LEN = 256
_GLOB_SPECIAL = re.compile(r"[*?[]")


def privacy_config_path(state_dir: Optional[str] = None) -> str:
    root = (state_dir or os.environ.get("HARNESS_STATE_DIR") or "").strip()
    if not root:
        root = os.path.join(os.path.expanduser("~"), ".pmharness")
    return os.path.join(os.path.expanduser(root), _CONFIG_NAME)


def normalize_pattern(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text or len(text) > _MAX_PATTERN_LEN or "\x00" in text:
        raise ValueError("forbidden pattern must be a nonempty path glob")
    if text.startswith("/") or re.match(r"^[A-Za-z]:/", text):
        raise ValueError("forbidden patterns are workspace-relative")
    return text


def load_forbidden_patterns(state_dir: Optional[str] = None) -> List[str]:
    path = privacy_config_path(state_dir)
    try:
        raw = json.loads(open(path, encoding="utf-8").read())
    except FileNotFoundError:
        return []
    except (OSError, ValueError, TypeError):
        return []
    items = raw.get("forbidden_patterns") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out: List[str] = []
    seen = set()
    for item in items[:_MAX_PATTERNS]:
        try:
            pattern = normalize_pattern(item)
        except ValueError:
            continue
        if pattern not in seen:
            seen.add(pattern)
            out.append(pattern)
    env_raw = os.environ.get(FORBIDDEN_ENV, "")
    if env_raw.strip():
        try:
            extra = json.loads(env_raw)
        except (ValueError, TypeError):
            extra = []
        if isinstance(extra, list):
            for item in extra:
                try:
                    pattern = normalize_pattern(item)
                except ValueError:
                    continue
                if pattern not in seen:
                    seen.add(pattern)
                    out.append(pattern)
    return out


def save_forbidden_patterns(patterns: Sequence[object], state_dir: Optional[str] = None) -> List[str]:
    cleaned: List[str] = []
    seen = set()
    for item in patterns:
        pattern = normalize_pattern(item)
        if pattern not in seen:
            seen.add(pattern)
            cleaned.append(pattern)
        if len(cleaned) >= _MAX_PATTERNS:
            break
    path = privacy_config_path(state_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = json.dumps({"forbidden_patterns": cleaned}, indent=2) + "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(payload)
    restrict_to_owner(path)
    export_forbidden_patterns_env(cleaned)
    return cleaned


def export_forbidden_patterns_env(patterns: Sequence[str]) -> str:
    """Hand the same list to worker processes. Not a Puppetmaster feature."""
    body = json.dumps(list(patterns))
    os.environ[FORBIDDEN_ENV] = body
    return body


def _candidate_names(path: str) -> List[str]:
    raw = str(path or "").replace("\\", "/").rstrip("/")
    if not raw:
        return []
    base = raw.rsplit("/", 1)[-1]
    names = [raw, base]
    if raw.startswith("./"):
        names.append(raw[2:])
    return [name for name in names if name]


def pattern_matches(path: str, pattern: str) -> bool:
    try:
        compiled = normalize_pattern(pattern)
    except ValueError:
        return False
    for name in _candidate_names(path):
        if fnmatch.fnmatch(name, compiled):
            return True
        if fnmatch.fnmatch(name.lower(), compiled.lower()):
            return True
        if not _GLOB_SPECIAL.search(compiled) and (
            name == compiled or name.endswith("/" + compiled)
        ):
            return True
    return False


def forbidden_reason(path: str, patterns: Iterable[str]) -> Optional[str]:
    """Return the deny text when ``path`` matches a forbidden pattern."""
    try:
        matched = next((p for p in patterns if pattern_matches(path, p)), None)
    except re.error:
        return 'Access denied: privacy pattern is invalid (fail-closed).'
    if matched is None:
        return None
    shown = os.path.basename(str(path or "").replace("\\", "/")) or str(path)
    return 'Access denied: "%s" is blocked for security.' % shown


def refuse_path(path: str, *, state_dir: Optional[str] = None) -> Optional[str]:
    return forbidden_reason(path, load_forbidden_patterns(state_dir))
