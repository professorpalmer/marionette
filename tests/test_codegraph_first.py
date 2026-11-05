"""The pilot must treat CodeGraph as the FIRST move on code work -- including
broad audits/sweeps, not just point lookups -- so it stops opening with a blind
grep and needing the user to remind it."""
import pytest

from harness.pilot import PILOT_SYSTEM


# The directive must name audit-style tasks, so the pilot does not treat
# "look through the codebase / find all X" as a grep job. It must also call
# out a blind grep as a defect.
@pytest.mark.parametrize("phrase", [
    pytest.param("codegraph first", id="directive"),
    pytest.param("audit", id="audit"),
    pytest.param("find all", id="find-all"),
    pytest.param("look through the codebase", id="look-through-codebase"),
    pytest.param("blind", id="blind"),
    pytest.param("grep", id="grep"),
])
def test_pilot_system_names_codegraph_first(phrase):
    assert phrase in PILOT_SYSTEM.lower(), f"missing phrase: {phrase!r}"
