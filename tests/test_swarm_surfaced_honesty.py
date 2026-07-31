from __future__ import annotations

"""Swarm honesty: promotion needs positive evidence, counts describe what is shown.

Covers the three coupled surfaces:
- ``_promote_degraded_prose`` never launders a tagged failure into a finding.
- ``BridgeResult`` counts always describe the surfaced compact artifact list.
- ``dispatch_swarm_action`` reports the surfaced count/adapter, never the raw ones.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness.pilot import PilotAction
from harness.send_loop_dispatch import dispatch_swarm_action
from pmharness.bridge import (
    BridgeResult,
    _analysis_bridge_status,
    _bridge_result,
    _promote_degraded_prose,
)

# Every failure tag that must block promotion of degraded prose.
NON_PROMOTABLE_TAGS = [
    "timeout",
    "model_not_found",
    "insufficient_credits",
    "no_credentials",
    "context_length_exceeded",
    "http_status:429",
    "http_status:500",
    "provider_error",
    "no_tool_calls",
    "empty_or_unstructured_agentic_result",
    "auth_failed:401",
]

_REAL_PROSE = (
    "The router drops rejected alternatives before the receipt is written, so "
    "harness/local_job_routing.py:88 reports savings against a baseline that no "
    "longer exists. Every downstream savings chip inherits the fabricated basis."
)


@pytest.mark.parametrize("tag", NON_PROMOTABLE_TAGS)
def test_failure_tagged_prose_is_never_promoted_to_a_finding(tag):
    compact = [{
        "type": "verification",
        "headline": _REAL_PROSE[:240],
        "body": _REAL_PROSE,
        "empty_headline": False,
        "failure": tag,
    }]

    promoted = _promote_degraded_prose(compact)

    assert promoted == compact, f"{tag} produced a synthetic finding"
    assert not any(a.get("type") == "finding" for a in promoted)


@pytest.mark.parametrize("tag", NON_PROMOTABLE_TAGS)
def test_failure_tagged_run_is_not_green(tag):
    compact = [{
        "type": "verification",
        "headline": _REAL_PROSE[:240],
        "body": _REAL_PROSE,
        "empty_headline": False,
        "failure": tag,
    }]

    status, summary = _analysis_bridge_status(
        _promote_degraded_prose(compact),
        job_status="completed",
        summary="Analysis complete.",
    )

    assert status in ("failed", "degraded", "error")
    assert "no structured findings" in summary.lower()


def test_untagged_prose_still_promotes():
    """Promotion must keep rescuing a worker that analyzed but skipped submit."""
    compact = [{
        "type": "verification",
        "headline": _REAL_PROSE[:240],
        "body": _REAL_PROSE,
        "empty_headline": False,
        "failure": None,
    }]

    promoted = _promote_degraded_prose(compact)

    findings = [a for a in promoted if a.get("type") == "finding"]
    assert len(findings) == 1
    assert findings[0]["body"] == _REAL_PROSE
    assert findings[0]["promoted_from"] == "verification"


def test_bridge_result_positional_auth_failure_compat():
    """Historical 8th positional arg still binds to auth_failure; new fields default."""
    result = BridgeResult(
        "job_pos",
        "completed",
        "swarm",
        1,
        ["finding"],
        "done",
        [{"type": "finding", "headline": "x"}],
        "401 unauthorized",
        "agentic",
    )
    assert result.auth_failure == "401 unauthorized"
    assert result.adapter == "agentic"
    assert result.raw_num_artifacts == 0
    assert result.dropped_artifacts == 0


def test_bridge_result_counts_describe_the_surfaced_list():
    surfaced = [
        {"type": "finding", "headline": "real"},
        {"type": "verification", "headline": "ran"},
    ]

    res = _bridge_result(
        job_id="job_abc",
        status="completed",
        mode="swarm",
        raw_num_artifacts=5,
        summary="done",
        artifacts=surfaced,
        adapter="agentic",
    )

    assert res.num_artifacts == len(res.artifacts) == 2
    assert res.artifact_types == ["finding", "verification"]
    assert res.raw_num_artifacts == 5
    assert res.dropped_artifacts == 3


def test_bridge_result_reports_zero_when_everything_was_filtered():
    res = _bridge_result(
        job_id="job_abc",
        status="failed",
        mode="swarm",
        raw_num_artifacts=3,
        summary="no structured findings",
        artifacts=[],
        adapter="agentic",
    )

    assert res.num_artifacts == 0
    assert res.artifact_types == []
    assert res.dropped_artifacts == 3


def _session():
    return SimpleNamespace(
        config=SimpleNamespace(repo="/repo/wiki"),
        _session_job_ids=[],
        _register_local_job=MagicMock(),
        _finish_local_job=MagicMock(),
        _append_action_result=MagicMock(),
        _display_transcript=[],
    )


def _run_dispatch(monkeypatch, result, *, turn_findings=None):
    import harness.send_loop_dispatch as dispatch

    monkeypatch.delenv("HARNESS_ALLOW_DEMO_SWARM", raising=False)
    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(
        dispatch, "stream_swarm", lambda session, intent, q: q.put(("done", result)),
    )
    session = _session()
    events = list(dispatch_swarm_action(
        session,
        PilotAction(kind="run_swarm", goal="audit routing", roles=["explore"]),
        "a-1",
        True,
        counters={"swarms": 0, "demo_swarms": 0},
        turn_findings=turn_findings if turn_findings is not None else [],
    ))
    return session, events


def _pilot_text(session):
    return session._append_action_result.call_args.args[2]


def test_refused_demo_claims_no_count_and_no_via_demo(monkeypatch):
    findings = []
    result = SimpleNamespace(
        job_id="job-demo",
        adapter="demo",
        mode="swarm",
        num_artifacts=7,
        artifact_types=["finding", "verification"],
        artifacts=[{"type": "finding", "headline": "generic audit complete", "body": "x" * 300}],
        auth_failure="",
        summary="demo",
    )

    session, events = _run_dispatch(monkeypatch, result, turn_findings=findings)

    action_result = next(e for e in events if e.kind == "action_result").data
    assert action_result["num"] == 0
    assert action_result["types"] == []
    assert action_result["adapter"] == "refused-demo"

    badge = next(e for e in events if e.kind == "swarm_result").data["result"]
    assert badge["applied"] is False
    assert badge["adapter"] == "refused-demo"
    assert "7" not in badge["summary"]

    text = _pilot_text(session)
    assert "via demo" not in text
    assert "returned 0 artifacts" in text
    assert findings == []

    engines = {
        call.kwargs.get("engine")
        for call in session._finish_local_job.call_args_list
    }
    assert "demo" not in engines


def test_degraded_swarm_reports_surfaced_plumbing_count(monkeypatch):
    """A run whose findings were all filtered must not quote the raw count."""
    result = SimpleNamespace(
        job_id="job_dead",
        adapter="agentic",
        mode="swarm",
        num_artifacts=9,
        artifact_types=["finding", "verification"],
        artifacts=[
            {"type": "verification", "headline": "no structured findings (timeout)",
             "body": "no structured findings (timeout)", "failure": "timeout"},
        ],
        auth_failure="",
        summary="no structured findings (timeout)",
    )

    session, events = _run_dispatch(monkeypatch, result)

    action_result = next(e for e in events if e.kind == "action_result").data
    assert action_result["num"] == 1
    assert action_result["types"] == ["verification"]

    badge = next(e for e in events if e.kind == "swarm_result").data["result"]
    assert badge["applied"] is False
    assert "1 plumbing artifacts" in badge["summary"]
    assert "9" not in badge["summary"]
    assert "returned 1 artifacts via agentic" in _pilot_text(session)


def test_substantive_findings_still_report_applied(monkeypatch):
    body = (
        "harness/keys.py reads only the state keys.json, so an upgraded install "
        "with ~/.pmharness/keys.json appears keyless until every provider key is "
        "re-entered by hand. See harness/keys.py line 40."
    )
    result = SimpleNamespace(
        job_id="job_ok",
        adapter="agentic",
        mode="swarm",
        num_artifacts=2,
        artifact_types=["finding", "verification"],
        artifacts=[
            {"type": "finding", "headline": body[:240], "body": body},
            {"type": "verification", "headline": "ran", "body": "ran"},
        ],
        auth_failure="",
        summary="1 finding",
    )

    session, events = _run_dispatch(monkeypatch, result)

    badge = next(e for e in events if e.kind == "swarm_result").data["result"]
    assert badge["applied"] is True
    assert badge["summary"] == "1 findings via agentic (2 artifacts)"


def test_pilot_result_is_bounded_to_current_job_evidence(monkeypatch):
    current_job = "job-current"
    result = SimpleNamespace(
        job_id=current_job,
        adapter="agentic",
        mode="swarm",
        num_artifacts=3,
        artifact_types=["finding", "routing", "verification"],
        artifacts=[
            {
                "type": "finding",
                "headline": "harness/router.py:10 contains the current finding",
                "body": "The current evidence is specific and file-backed.",
                "execution_ref": {"job_id": current_job},
            },
            {"type": "routing", "headline": "route"},
            {"type": "verification", "headline": "checked", "execution_ref": {"job_id": current_job}},
        ],
        auth_failure="",
        summary="one finding",
    )

    session, _events = _run_dispatch(monkeypatch, result)
    text = _pilot_text(session)

    assert f"Exact current job id: {current_job}" in text
    assert "Current returned artifacts: 3 (finding=1, routing=1, verification=1)" in text
    assert "Direct execution provenance: 2/2 non-routing artifacts." in text
    assert "historical/untrusted" in text
    assert "only this job’s returned artifacts or explicit probes run after it" in text
    assert "not verified, never as a defect" in text
    assert "Never carry earlier issue examples forward" in text
    assert "blend prior findings" not in text
