"""_compact_artifact must NEVER surface a real finding as an empty headline.

Regression for a real defect: a full 5-worker audit swarm returned empty-headline
artifacts ("Agentic worker completed without structured findings") despite 8.4M
tokens of real work. The cause: _compact_artifact derived the headline ONLY from
a narrow set of payload keys (claim/decision/risk/check/summary/change). Workers
that emitted their analysis under a different key (report, message, text, note,
mitigation, why, result, observation, ...) had their headline collapse to '' and
the finding was dropped.

The fix broadens headline extraction to those keys, then falls back to the first
non-empty string value in the payload, and only labels by type when the payload
is genuinely empty of usable text. This exercises _compact_artifact directly on
artifact-shaped objects -- pure/hermetic, no Puppetmaster import.
"""

from pmharness.bridge import _compact_artifact


class _FakeArtifact:
    def __init__(self, type_="finding", payload=None, confidence=None):
        self.type = type_
        self.payload = payload or {}
        self.confidence = confidence


def test_report_key_becomes_headline():
    a = _FakeArtifact(payload={"report": "auth token is logged in plaintext"})
    c = _compact_artifact(a)
    assert c["headline"] == "auth token is logged in plaintext"
    assert c["empty_headline"] is False


def test_message_key_becomes_headline():
    a = _FakeArtifact(payload={"message": "race condition in cache eviction"})
    c = _compact_artifact(a)
    assert c["headline"] == "race condition in cache eviction"
    assert c["empty_headline"] is False


def test_risk_artifact_surfaces_payload_risk():
    a = _FakeArtifact(type_="risk", payload={"risk": "SQL injection in query builder"})
    c = _compact_artifact(a)
    assert c["headline"] == "SQL injection in query builder"
    assert c["empty_headline"] is False


def test_arbitrary_string_key_still_surfaces():
    # A key not in the canonical list: last-resort first-non-empty-string value.
    a = _FakeArtifact(payload={"weird_custom_field": "unbounded recursion here"})
    c = _compact_artifact(a)
    assert c["headline"] == "unbounded recursion here"
    assert c["empty_headline"] is False


def test_empty_payload_yields_stable_type_labeled_headline_no_crash():
    a = _FakeArtifact(type_="finding", payload={})
    c = _compact_artifact(a)
    # Empty payload -> no crash, empty headline flagged honestly.
    assert c["empty_headline"] is True
    assert c["headline"] == ""
    assert c["type"] == "finding"


def test_payload_present_but_no_string_gets_type_label():
    # Payload exists but has no usable string text -> label by type, flag empty.
    a = _FakeArtifact(type_="risk", payload={"score": 7, "flags": []})
    c = _compact_artifact(a)
    assert c["empty_headline"] is True
    assert c["headline"] == "risk"


def test_headline_is_capped_at_240_chars():
    long = "x" * 500
    a = _FakeArtifact(payload={"report": long})
    c = _compact_artifact(a)
    assert len(c["headline"]) == 240


def test_empty_unstructured_prefers_stdout_over_plumbing_summary():
    """Plumbing summary must not hide parked stdout analysis from promote."""
    from pmharness.bridge import _promote_degraded_prose

    analysis = (
        "harness/worker.py:483 coerce_unlabeled_analysis_prose wraps unlabeled "
        "substantive prose as FINDING so Mode-Scoped audits stop failing empty."
    )
    a = _FakeArtifact(
        type_="verification",
        payload={
            "summary": "Agentic worker completed without structured findings",
            "stdout": analysis,
            "failure": "empty_or_unstructured_agentic_result",
        },
    )
    c = _compact_artifact(a)
    assert "without structured findings" not in (c.get("headline") or "").lower()
    assert "harness/worker.py:483" in (c.get("body") or c.get("headline") or "")
    out = _promote_degraded_prose([c])
    findings = [row for row in out if row.get("type") == "finding"]
    assert findings, "stdout analysis behind plumbing summary must promote"
    assert "harness/worker.py:483" in findings[0]["body"]
    assert findings[0]["body"].startswith("FINDING: ")
