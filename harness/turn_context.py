"""Turn-context journal for time-travel rule inspection (round 6, v1).

At each turn boundary the session appends one JSON line to
{state_dir}/turn_context.jsonl recording which check specs and behavior
toggles were active, so a regression at turn N can be reproduced against the
exact configuration that produced it. v1 is observability only: nothing here
changes live behavior. Stdlib-only; recording never raises into the caller.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Optional

JOURNAL_FILENAME = "turn_context.jsonl"

# Env toggles that alter pilot behavior between runs. Recorded verbatim
# (empty string when unset) so a replayer can diff configurations.
TRACKED_ENV_TOGGLES = (
    "HARNESS_DECLARATIVE_CHECKS",
    "HARNESS_HASH_EDIT",
    "HARNESS_TOOL_DISCOVERY",
    "HARNESS_ADVISOR",
    "HARNESS_AST_PREVIEW",
)


def turn_context_enabled() -> bool:
    """Recording is on by default; opt out with HARNESS_TURN_CONTEXT=0."""
    return os.environ.get("HARNESS_TURN_CONTEXT", "").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )


def _journal_path(state_dir: str) -> str:
    return os.path.join(os.path.abspath(state_dir), JOURNAL_FILENAME)


def _check_specs_fingerprint(repo: str, state_dir: str) -> tuple[str, int]:
    """(sha256 hex, spec count) over the currently active check specs.

    An empty hash means "no specs". The hash covers the parsed spec fields
    (sorted, JSON-serialized) so editing any spec file changes it while
    filesystem noise (mtime, ordering) does not.
    """
    try:
        from .declarative_checks import find_check_specs

        specs = find_check_specs(repo, state_dir)
    except Exception:
        return "", 0
    if not specs:
        return "", 0
    try:
        from dataclasses import asdict

        canonical = json.dumps(
            sorted((asdict(s) for s in specs), key=lambda d: str(d.get("id", ""))),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), len(specs)
    except Exception:
        return "", len(specs)


def record_turn_context(
    state_dir: str,
    session_id: str,
    turn: int,
    repo: str = "",
) -> None:
    """Append one turn-context record. Failures are swallowed."""
    if not state_dir or turn <= 0 or not turn_context_enabled():
        return
    try:
        specs_hash, spec_count = _check_specs_fingerprint(repo, state_dir)
        record = {
            "session_id": session_id or "default",
            "turn": int(turn),
            "ts": time.time(),
            "check_specs_hash": specs_hash,
            "check_spec_count": spec_count,
            "env": {name: os.environ.get(name, "") for name in TRACKED_ENV_TOGGLES},
        }
        os.makedirs(os.path.abspath(state_dir), exist_ok=True)
        with open(_journal_path(state_dir), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass


def context_at(
    state_dir: str,
    session_id: str,
    turn: int,
) -> Optional[dict[str, Any]]:
    """Newest recorded snapshot for ``turn`` in ``session_id``, or None.

    Malformed lines are skipped; a missing journal yields None.
    """
    if not state_dir or turn <= 0:
        return None
    path = _journal_path(state_dir)
    if not os.path.isfile(path):
        return None
    sid = session_id or "default"
    newest: Optional[dict[str, Any]] = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("session_id") != sid:
                    continue
                if int(record.get("turn", -1)) != int(turn):
                    continue
                newest = record
    except Exception:
        return None
    return newest
