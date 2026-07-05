"""A swarm whose worker analyzed in PROSE (no submit_findings) must still surface
that analysis. The agentic adapter parks degraded final_text in a VERIFICATION
artifact's stdout; without promotion the pilot digest hides it as plumbing and
the swarm reads as 'completed without structured findings' despite real work.
"""
from pmharness.bridge import _compact_artifact, _promote_degraded_prose


class _Artifact:
    def __init__(self, type, payload, confidence=None):
        self.type = type
        self.payload = payload
        self.confidence = confidence


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


def test_long_stdout_prose_promoted_with_full_body_preserved():
    # A broad audit's real analysis (>1000 chars) parked in a verification
    # artifact's stdout must be promoted WITH its full body intact -- not clipped
    # to the 240-char display headline.
    long_prose = "AUDIT: " + ("line of real analysis; " * 80)
    assert len(long_prose) > 1000
    compact = [
        _compact_artifact(_Artifact("routing", {})),
        _compact_artifact(_Artifact("verification", {"stdout": long_prose})),
    ]
    out = _promote_degraded_prose(compact)
    findings = [a for a in out if a.get("type") == "finding"]
    assert findings, "long prose analysis must be promoted to a finding"
    f = findings[0]
    # (a) FULL body preserved, not truncated to 240.
    assert f["body"] == long_prose.strip()
    assert len(f["body"]) > 1000
    # (b) headline stays <= 240 for display.
    assert len(f["headline"]) <= 240
    assert f.get("promoted_from") == "verification"


def test_headline_clipped_but_body_full_in_compact():
    long_prose = "X" * 3000
    c = _compact_artifact(_Artifact("verification", {"stdout": long_prose}))
    assert len(c["headline"]) <= 240
    assert c["body"] == long_prose
    assert len(c["body"]) == 3000


def test_no_double_promote_with_real_signal_and_long_body():
    long_prose = "Y" * 2000
    compact = [
        _compact_artifact(_Artifact("finding", {"claim": "a real structured finding"})),
        _compact_artifact(_Artifact("verification", {"stdout": long_prose})),
    ]
    out = _promote_degraded_prose(compact)
    # The real finding wins; the verification prose is NOT promoted.
    assert sum(1 for a in out if a.get("type") == "finding") == 1


def test_short_body_under_40_not_promoted():
    compact = [
        _compact_artifact(_Artifact("routing", {})),
        _compact_artifact(_Artifact("verification", {"stdout": "too short"})),
    ]
    out = _promote_degraded_prose(compact)
    assert not any(a.get("type") == "finding" for a in out)
