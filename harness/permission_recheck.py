from __future__ import annotations

"""Recheck permission after a tool input rewrite.

A rewrite of a previously classified command is not covered by the
original hash. Danger on the new text needs its own approval.
"""

from dataclasses import dataclass

from .command_policy import classify_command


@dataclass(frozen=True)
class RecheckResult:
    allowed: bool
    reason: str
    rewritten: bool


def recheck_rewritten_input(original: str, rewritten: str) -> RecheckResult:
    """Compare original vs rewritten command text.

    Unchanged text is allowed. A rewritten command that classifies as
    danger is not allowed until a caller presents a matching approval.
    """
    before = (original or "").strip()
    after = (rewritten or "").strip()
    if after == before:
        return RecheckResult(True, "unchanged", False)
    if not after:
        return RecheckResult(False, "rewritten_empty", True)
    verdict = classify_command(after)
    if verdict.danger:
        return RecheckResult(False, "rewritten_needs_approval", True)
    return RecheckResult(True, "rewritten_safe", True)
