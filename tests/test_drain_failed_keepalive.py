"""Loud FAILED keep-alive when a local implement worker dies.

A failed drain must put [swarm FAILED for: ...] in history, stamp an error on
the swarm_result event, and inject a pilot_resume continuation that says FAILED
and tells the pilot not to pretend the patch landed.
"""
from __future__ import annotations

import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def _session() -> ConversationalSession:
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    return ConversationalSession(cfg)


def test_drain_failed_local_result_loud_keepalive():
    s = _session()
    s._swarm_results.put({
        "job_id": "local-dead",
        "objective": "add a helper",
        "result": {
            "applied": False,
            "files": [],
            "summary": "Worker failed to produce patch",
            "error": "no changes produced",
            "has_patch_art": False,
            "apply_msg": "no changes produced",
        },
    })
    events = list(s.drain_swarm_results())

    swarm = [e for e in events if e.kind == "swarm_result"]
    assert len(swarm) == 1
    assert swarm[0].data["result"].get("error")
    assert "FAILED" in (swarm[0].data.get("message") or "")

    assert any(
        m["role"] == "assistant"
        and "[swarm FAILED for: add a helper]" in m["content"]
        for m in s._history
    )

    resume = [
        m for m in s._history
        if m["role"] == "user" and "FAILED" in m["content"]
    ]
    assert resume, "expected FAILED pilot-resume continuation"
    assert "do not pretend" in resume[0]["content"].lower()
    assert any(e.kind == "pilot_resume" for e in events)


def test_drain_success_still_says_finished():
    s = _session()
    s._swarm_results.put({
        "job_id": "local-ok",
        "objective": "add a helper",
        "result": {
            "applied": True,
            "files": ["helper.py"],
            "summary": "added it",
            "error": None,
        },
    })
    events = list(s.drain_swarm_results())
    assert any(
        m["role"] == "assistant"
        and "[swarm result for: add a helper]" in m["content"]
        for m in s._history
    )
    resume = [
        m for m in s._history
        if m["role"] == "user" and "[background job local-ok finished]" in m["content"]
    ]
    assert resume
    assert "FAILED" not in resume[0]["content"]
    assert any(e.kind == "pilot_resume" for e in events)


def test_drain_applied_false_without_error_still_failed():
    """applied=False (and not held_for_review) is failure even when error is unset."""
    s = _session()
    s._swarm_results.put({
        "job_id": "local-noapply",
        "objective": "patch files",
        "result": {
            "applied": False,
            "files": [],
            "summary": "PATCH DID NOT APPLY: conflict",
            "error": None,
            "held_for_review": False,
            "has_patch_art": True,
            "apply_msg": "conflict",
        },
    })
    list(s.drain_swarm_results())
    assert any(
        m["role"] == "assistant" and "[swarm FAILED for: patch files]" in m["content"]
        for m in s._history
    )
    assert any(
        m["role"] == "user" and "FAILED" in m["content"] and "do not pretend" in m["content"].lower()
        for m in s._history
    )
