"""The bridge must opt a swarm worker into the CDP browser toolset for a
live-site / browser goal (setting payload['allow_browser'] and a browser-aware
instruction), while leaving ordinary read-only code-analysis goals untouched.
Regression guard for the browser-path wiring (agentic adapter honors
allow_browser via its own _browser_enabled gate)."""
import os

import pytest

import pmharness.bridge as b


def test_browser_swarm_enabled_detects_urls_and_verbs():
    assert b._browser_swarm_enabled("navigate to https://dugoutfantasy.com and report the title")
    assert b._browser_swarm_enabled("open the site and take a screenshot of the homepage")
    assert b._browser_swarm_enabled("browse http://example.com")
    # Plain code-analysis goals must NOT trip the browser gate.
    assert not b._browser_swarm_enabled("audit the auth module for race conditions")
    assert not b._browser_swarm_enabled("map the pipeline end to end")


def test_browser_swarm_env_override(monkeypatch):
    monkeypatch.setenv("HARNESS_SWARM_BROWSER", "1")
    # Even a non-browser goal is enabled when the operator forces it.
    assert b._browser_swarm_enabled("audit the auth module")
    monkeypatch.delenv("HARNESS_SWARM_BROWSER", raising=False)
    assert not b._browser_swarm_enabled("audit the auth module")


# Stop conditions (ARC-AGI operations-manual style) make a sub-agent stop
# looping and report back. Both briefs must carry them.
_STOP_CONDITIONS = (
    # (a) stop after 2-3 failed variations and report back to the caller.
    "2-3 variations",
    "report back",
    # (b) never restart/reset to "think more carefully" / "clean approach".
    "Never restart or reset",
    "think more carefully",
    "clean approach",
    "discards the progress",
    # (c) prefer a few well-evidenced findings over endless exploration.
    "well-evidenced findings",
    "never concludes",
)


@pytest.mark.parametrize(
    ("goal", "browser", "required", "forbidden"),
    [
        pytest.param(
            "audit auth", False,
            ("Analyze the REAL codebase", "READ-ONLY", "submit_findings"),
            ("browser_navigate",),
            id="code",
        ),
        pytest.param(
            "open https://x.com", True,
            ("browser_navigate", "browser_snapshot", "browser_auth_handoff",
             "READ-ONLY", "credentials", "submit_findings"),
            (),
            id="browser",
        ),
    ],
)
def test_analysis_instruction_contents(goal, browser, required, forbidden):
    inst = b._analysis_instruction(goal, "/repo", "explore", browser=browser)
    for sub in _STOP_CONDITIONS:
        assert sub in inst, f"missing stop-condition guidance: {sub!r}"
    for sub in required:
        assert sub in inst, f"missing: {sub!r}"
    for sub in forbidden:
        assert sub not in inst, f"unexpected: {sub!r}"
    assert inst.isascii(), "brief must stay plain ASCII (no emoji)"
