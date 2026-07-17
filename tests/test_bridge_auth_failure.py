"""Bridge surfacing of a provider auth failure.

A dead/revoked/wrong API key makes a swarm worker fail with an auth rejection.
Puppetmaster's agentic adapter now emits a dedicated RISK artifact stamped
``failure="auth_failed:<status>"``. The bridge must (a) carry that tag through
compaction, (b) hoist the auth-risk to the FRONT so a fixed digest slice can't
drop it, and (c) expose a loud ``auth_failure`` note on the result -- so the
harness flags a credential problem instead of laundering it into a generic
"completed without structured findings" degrade.

Pure/hermetic: exercises the bridge helpers directly with fixture artifacts,
no Puppetmaster process and no network.
"""
from pmharness.bridge import (
    _auth_failure_note,
    _compact_artifact,
    _hoist_auth_risks,
    _is_auth_failure_tag,
    _promote_degraded_prose,
    _summary_leading_with_auth,
)


class _Art:
    """Minimal stand-in for a Puppetmaster Artifact."""

    def __init__(self, type_, payload, confidence=0.9):
        self.type = type_
        self.payload = payload
        self.confidence = confidence


def _auth_risk_artifact():
    return _Art(
        "risk",
        {
            "risk": "AUTH FAILURE: provider 'openai' rejected the API key (HTTP 401).",
            "mitigation": "Fix or remove OPENAI_API_KEY, then retry.",
            "failure": "auth_failed:401",
            "provider": "openai",
        },
    )


def test_compact_carries_failure_tag():
    compact = _compact_artifact(_auth_risk_artifact())
    assert compact["failure"] == "auth_failed:401"
    assert compact["type"] == "risk"
    assert "AUTH FAILURE" in compact["headline"]
    assert "OPENAI_API_KEY" in compact["headline"]


def test_compact_failure_none_when_absent():
    compact = _compact_artifact(_Art("finding", {"claim": "some finding"}))
    assert compact["failure"] is None


def test_hoist_moves_auth_risk_to_front():
    # Auth risk buried behind many other artifacts must be pulled to index 0 so
    # a downstream artifacts[:8] slice can never drop it.
    others = [_compact_artifact(_Art("finding", {"claim": f"f{i}"})) for i in range(10)]
    auth = _compact_artifact(_auth_risk_artifact())
    hoisted = _hoist_auth_risks(others + [auth])
    assert hoisted[0]["failure"] == "auth_failed:401"
    assert len(hoisted) == len(others) + 1  # nothing dropped


def test_hoist_noop_without_auth():
    compact = [_compact_artifact(_Art("finding", {"claim": "x"}))]
    assert _hoist_auth_risks(compact) == compact


def test_auth_note_present_and_absent():
    with_auth = [_compact_artifact(_auth_risk_artifact())]
    note = _auth_failure_note(with_auth)
    assert "AUTH FAILURE" in note and "openai" in note
    assert "OPENAI_API_KEY" in note

    without = [_compact_artifact(_Art("finding", {"claim": "clean"}))]
    assert _auth_failure_note(without) == ""


def test_verification_http_401_is_auth_failure():
    # Agentic ``_fail`` stamps failure=http_status:401 on a verification
    # artifact when the dedicated auth RISK is missing. Must still surface.
    compact = _compact_artifact(_Art(
        "verification",
        {
            "check": "audit the repo",
            "result": "failed",
            "failure": "http_status:401",
            "returncode": 401,
            "stderr": "invalid_api_key",
        },
    ))
    assert _is_auth_failure_tag(compact["failure"], compact["headline"])
    note = _auth_failure_note([compact])
    assert "AUTH FAILURE" in note
    assert "without structured findings" not in note.lower()


def test_promote_skips_when_auth_failure_present():
    # Zero signal + auth verification must NOT become a synthetic finding that
    # buries the credential failure as "structured findings".
    auth_ver = _compact_artifact(_Art(
        "verification",
        {
            "check": "x",
            "result": "failed",
            "failure": "http_status:401",
            "returncode": 401,
            "stderr": "x" * 80,
            "stdout": "long stderr-looking prose that would otherwise promote " * 3,
        },
    ))
    out = _promote_degraded_prose([auth_ver])
    assert all(a.get("promoted_from") != "verification" for a in out)
    assert _auth_failure_note(out)


def test_summary_leads_with_auth_over_generic_degrade():
    note = "AUTH FAILURE: provider 'openai' rejected the API key (HTTP 401). Fix OPENAI_API_KEY."
    generic = "Agentic worker completed without structured findings."
    led = _summary_leading_with_auth(generic, note)
    assert led.startswith("AUTH FAILURE")
    assert "without structured findings" not in led
    assert "OPENAI_API_KEY" in led or "openai" in led
