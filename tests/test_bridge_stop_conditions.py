"""The assembled worker/analysis brief must carry crisp, first-principles STOP
conditions (ARC-AGI operations-manual style) so a sub-agent stops looping and
reports back instead of burning its whole turn budget. Regression guard for the
prompt-copy hardening: the code-analysis and browser briefs both must contain
the stop-condition guidance substrings. Hermetic, no network."""
import pmharness.bridge as b


def _expected_substrings():
    return (
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


def test_code_analysis_brief_has_stop_conditions():
    inst = b._analysis_instruction("audit auth", "/repo", "explore", browser=False)
    for sub in _expected_substrings():
        assert sub in inst, f"missing stop-condition guidance: {sub!r}"
    # Existing turn-budget guidance is preserved (tightened, not removed).
    assert "submit_findings" in inst
    assert "READ-ONLY" in inst


def test_browser_swarm_brief_has_stop_conditions():
    inst = b._analysis_instruction("open https://x.com", "/repo", "explore", browser=True)
    for sub in _expected_substrings():
        assert sub in inst, f"missing stop-condition guidance: {sub!r}"
    # Browser wiring is untouched.
    assert "browser_navigate" in inst
    assert "submit_findings" in inst


def test_no_emoji_in_briefs():
    for browser in (False, True):
        inst = b._analysis_instruction("goal", "/repo", "explore", browser=browser)
        assert inst.isascii(), "brief must stay plain ASCII (no emoji)"
