from __future__ import annotations

"""Soft tool-use requirements (oh-my-pi remind-then-escalate).

On MICRO/STANDARD turns that touch files, nudge the pilot to run tests/verify
before settling. After one reminder with still no ``run_command``,
callers may escalate rather than silently accept unverified edits.
"""

from typing import Iterable, Optional, Union

from .task_profile import MICRO, STANDARD, normalize_profile

_REMIND_PROFILES = frozenset({MICRO, STANDARD})
# One nudge: a pilot that ignored the reminder answers the same text the same
# way, so repeats only burn round trips before escalation.
_MAX_REMINDS = 1

# Prose edits have nothing for a test run to check.
_DOC_SUFFIXES = (".md", ".mdx", ".rst", ".adoc")


def verifiable_files(files: Iterable[str]) -> list:
    """Touched files a verify command could meaningfully check (not docs)."""
    return [f for f in files if not str(f).lower().endswith(_DOC_SUFFIXES)]


def _as_int(value: Union[int, float, None]) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _profile_uses_soft_verify(profile: Optional[str]) -> bool:
    resolved = normalize_profile(profile)
    return resolved in _REMIND_PROFILES


class SoftToolRequirement:
    """Remind-then-escalate verify gate for MICRO/STANDARD file edits."""

    MAX_REMINDS = _MAX_REMINDS

    @staticmethod
    def should_remind_verify(
        profile: Optional[str],
        files_touched: int,
        ran_command: bool,
        remind_count: int,
    ) -> bool:
        """True when MICRO/STANDARD edits lack a command and reminds remain."""
        if not _profile_uses_soft_verify(profile):
            return False
        if _as_int(files_touched) < 1:
            return False
        if ran_command:
            return False
        return _as_int(remind_count) < SoftToolRequirement.MAX_REMINDS

    @staticmethod
    def remind_message() -> str:
        """Short nudge to run tests/verify before finishing."""
        return (
            "You changed files but have not run tests or a verify command yet. "
            "Run tests/verify before finishing."
        )

    @staticmethod
    def should_escalate_unverified(
        profile: Optional[str],
        files_touched: int,
        ran_command: bool,
        remind_count: int,
    ) -> bool:
        """True after the remind budget with edits still unverified by a command.

        Profile is accepted for API symmetry with ``should_remind_verify``;
        escalation itself keys off remind budget, missing command, and edits.
        """
        if _as_int(files_touched) < 1:
            return False
        if ran_command:
            return False
        return _as_int(remind_count) >= SoftToolRequirement.MAX_REMINDS
