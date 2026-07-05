"""A swarm whose worker analyzed in PROSE (no submit_findings) must still surface
that analysis. The agentic adapter parks degraded final_text in a VERIFICATION
artifact's stdout; without promotion the pilot digest hides it as plumbing and
the swarm reads as 'completed without structured findings' despite real work.
"""
from pmharness.bridge import _promote_degraded_prose


def test_prose_verification_is_promoted_when_no_signal():
    prose = ("harness/server.py:2834 bills cached tokens at full price; apply the "
             "cache discount so multi-turn cost is accurate.")
    compact = [
        {"type": "routing", "headline": "", "empty_headline": True},
        {"type": "verification", "headline": prose, "empty_headline": False},
    ]
    out = _promote_degraded_prose(compact)
    findings = [a for a in out if a.get("type") == "finding"]
    assert findings, "prose analysis must be promoted to a finding"
    assert findings[0]["headline"].startswith("harness/server.py:2834")
    assert findings[0].get("promoted_from") == "verification"


def test_no_promotion_when_real_findings_exist():
    compact = [
        {"type": "finding", "headline": "real finding", "empty_headline": False},
        {"type": "verification", "headline": "some long verification prose here that is over forty chars", "empty_headline": False},
    ]
    out = _promote_degraded_prose(compact)
    # Exactly the one real finding; the verification prose is NOT promoted.
    assert sum(1 for a in out if a.get("type") == "finding") == 1


def test_short_verification_not_promoted():
    compact = [
        {"type": "routing", "headline": "", "empty_headline": True},
        {"type": "verification", "headline": "passed", "empty_headline": False},
    ]
    out = _promote_degraded_prose(compact)
    assert not any(a.get("type") == "finding" for a in out), "one-word status must not become a finding"


def test_empty_input_is_safe():
    assert _promote_degraded_prose([]) == []
