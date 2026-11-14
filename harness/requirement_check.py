"""Acceptance check before a low-effort turn ends.

At low reasoning effort the pilot ends spec turns with stated requirements
unmet: option combinations, inverse modes, exact formats, ordering rules.
On DeepSWE at low effort, 65 of 89 failed trials passed 80% or more of the
hidden tests. A model at high effort does this check by itself; at high
effort the check gave no gain (v0.9.661 A/B), so it runs only at low effort.
"""
from __future__ import annotations

import os
from typing import Any

from .reasoning_effort import endpoint_reasoning_effort

# Hidden tests on spec tasks check single sentences of a long request.
MIN_REQUEST_CHARS = 800
LOW_EFFORTS = frozenset({"none", "low", "medium"})
EDIT_TOOLS = frozenset({"write_file", "edit_file", "hash_edit", "run_implement", "run_parallel"})
NOTE = (
    "Before you end the turn, do an acceptance check against the user's "
    "request. Read the request again, sentence by sentence, and list each "
    "concrete requirement in it: names and signatures, exact error messages, "
    "output formats, defaults, ordering rules, edge cases, error behavior, "
    "and each mode or option combination it names. For each one, show the "
    "command output or test result that proves it. If a requirement has no "
    "proof, check it now and fix what fails. Use throwaway scripts, and do "
    "not leave new files that the request did not ask for. Then give the "
    "final answer."
)
# Round 2. The pilot's own checks test its own reading of the request, so a
# misread passes them. Hidden tests come from the request text alone.
AUTHOR_NOTE = (
    "Now take the role of the person who wrote the hidden acceptance tests "
    "for this request. That person reads only the request text and calls the "
    "public API exactly as the request names it. Read the request again, "
    "sentence by sentence. Write new throwaway tests for what your checks did "
    "not exercise: each argument and keyword argument the request names, "
    "stated defaults, values for empty, missing and unnamed cases, exact "
    "strings and output formats, the errors raised, each mode and option "
    "combination, and each surface the request names (CLI commands, config "
    "loading, constructors, serializers, top-level exports). Where the "
    "request can be read two ways, use the reading that its examples and "
    "names support. Run the tests and fix each failure. Delete the throwaway "
    "tests. Then give the final answer."
)
NOTES = (NOTE, AUTHOR_NOTE)


def enabled() -> bool:
    """On at low effort. ``HARNESS_REQUIREMENT_CHECK`` 1 or 0 forces it."""
    forced = os.environ.get("HARNESS_REQUIREMENT_CHECK", "").strip()
    if forced in ("0", "1"):
        return forced == "1"
    return endpoint_reasoning_effort() in LOW_EFFORTS


def rounds() -> int:
    """Check rounds per turn, 2 by default. ``HARNESS_REQUIREMENT_CHECK_ROUNDS`` sets 1 or 2."""
    raw = os.environ.get("HARNESS_REQUIREMENT_CHECK_ROUNDS", "").strip()
    return 1 if raw == "1" else len(NOTES)


def _turn_edited_files(history: list) -> bool:
    """True when a row after the last user request called an edit tool."""
    from .working_set import is_user_request

    for row in reversed(history):
        if not isinstance(row, dict):
            continue
        if is_user_request(row):
            return False
        for call in row.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            name = (call.get("function") or {}).get("name") or call.get("name")
            if name in EDIT_TOOLS:
                return True
    return False


def maybe_inject(session: Any, user_message: str) -> bool:
    """Append the next check note when the turn qualifies, up to ``rounds()`` per turn."""
    done = int(getattr(session, "_requirement_checks", 0) or 0)
    if done >= rounds() or not enabled():
        return False
    if len(str(user_message or "")) < MIN_REQUEST_CHARS:
        return False
    history = getattr(session, "_history", None)
    if not isinstance(history, list) or not _turn_edited_files(history):
        return False
    session._requirement_checks = done + 1
    history.append({"role": "user", "content": NOTES[done], "source": "requirement_check"})
    return True
