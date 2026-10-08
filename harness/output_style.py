"""Opt-in output style for the pilot and its workers (Settings: Output style).

``HARNESS_OUTPUT_STYLE=ste`` makes the pilot write ASD-STE100 Simplified
Technical English. The directive comes from the Puppetmaster ``ste`` preset,
so the pilot and its workers follow one rule set. The Settings toggle also
sets ``PUPPETMASTER_OUTPUT_STYLE`` so that each new worker gets the same
directive.

The directive goes into each turn, not into the frozen system prompt. Thus a
change in Settings applies to the next turn of an open session.
"""
from __future__ import annotations

import os

OUTPUT_STYLE_ENV = "HARNESS_OUTPUT_STYLE"
WORKER_OUTPUT_STYLE_ENV = "PUPPETMASTER_OUTPUT_STYLE"
STE = "ste"
OFF = "off"
CHOICES = (OFF, STE)


def pilot_output_style() -> str:
    """The active pilot style: ``ste`` or ``off``."""
    raw = (os.environ.get(OUTPUT_STYLE_ENV) or "").strip().lower()
    return raw if raw in CHOICES else OFF


def output_style_turn_note() -> str:
    """The per-turn style directive, or ``""`` when the style is off."""
    if pilot_output_style() != STE:
        return ""
    try:
        from puppetmaster.output_style import directive_for
    except ImportError:
        return ""
    return (
        directive_for(STE)
        + "\n- This applies to each reply in this session: answers, plans, "
        "status reports, commit messages, and documents."
    )
