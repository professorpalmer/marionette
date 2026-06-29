"""The pilot must treat CodeGraph as the FIRST move on code work -- including
broad audits/sweeps, not just point lookups -- so it stops opening with a blind
grep and needing the user to remind it."""
from harness.pilot import PILOT_SYSTEM


def test_codegraph_first_directive_present():
    s = PILOT_SYSTEM.lower()
    assert "codegraph first" in s


def test_codegraph_covers_audits_and_sweeps():
    s = PILOT_SYSTEM.lower()
    # The directive must explicitly name audit-style tasks so the pilot doesn't
    # treat "look through the codebase / find all X" as a grep job.
    for phrase in ["audit", "find all", "look through the codebase"]:
        assert phrase in s, f"missing audit-coverage phrase: {phrase!r}"


def test_blind_grep_called_out_as_defect():
    s = PILOT_SYSTEM.lower()
    assert "blind" in s and "grep" in s
