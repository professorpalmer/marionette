"""The bridge must opt a swarm worker into the CDP browser toolset for a
live-site / browser goal (setting payload['allow_browser'] and a browser-aware
instruction), while leaving ordinary read-only code-analysis goals untouched.
Regression guard for the browser-path wiring (agentic adapter honors
allow_browser via its own _browser_enabled gate)."""
import os

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


def test_browser_instruction_mentions_browser_tools():
    inst = b._analysis_instruction("open https://x.com", "/repo", "explore", browser=True)
    assert "browser_navigate" in inst
    assert "browser_snapshot" in inst
    # Still read-only: never edit files or submit credentials.
    assert "READ-ONLY" in inst
    assert "credentials" in inst


def test_code_instruction_is_unchanged_read_only_code():
    inst = b._analysis_instruction("audit auth", "/repo", "explore", browser=False)
    assert "Analyze the REAL codebase" in inst
    assert "READ-ONLY" in inst
    assert "browser_navigate" not in inst
