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
    assert "re-dispatch a narrowed run_swarm" in resume[0]["content"]
    assert "inline exploration campaign" in resume[0]["content"]
    assert any(e.kind == "pilot_resume" for e in events)


def test_drain_success_keepalive_nudges_redispatch_not_inline_explore():
    """Successful analysis keep-alive must forbid substituting inline exploration."""
    s = _session()
    s._swarm_results.put({
        "job_id": "analysis-thin",
        "objective": "audit auth",
        "result": {
            # Analysis success: no patch applied; analysis_ok marks findings-accepted.
            "applied": False,
            "analysis_ok": True,
            "files": [],
            "summary": "Successfully completed analysis task",
            "error": None,
            "has_patch_art": False,
        },
    })
    list(s.drain_swarm_results())
    resume = [
        m for m in s._history
        if m["role"] == "user" and "[background job analysis-thin finished]" in m["content"]
    ]
    assert resume
    text = resume[0]["content"]
    assert "FAILED" not in text
    assert "read-only analysis swarm" in text
    assert "re-dispatch a narrowed run_swarm" in text
    assert "list_dir/search_files/grep/read sweeps" in text
    assert "do NOT open a broad inline exploration campaign" in text


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


def test_drain_analysis_ok_empty_files_not_failed_apply():
    """expects_diff=False success: applied=False + analysis_ok is not a failed apply."""
    s = _session()
    finding = (
        "FINDING: race in harness/send_loop.py:412 — busy lock leaked after interrupt"
    )
    s._swarm_results.put({
        "job_id": "analysis-ok",
        "objective": "audit send loop",
        "result": {
            "applied": False,
            "analysis_ok": True,
            "files": [],
            "summary": finding,
            "error": None,
            "has_patch_art": False,
            "ar_list": [{"type": "finding", "headline": finding}],
            "artifact_types": ["finding"],
        },
    })
    events = list(s.drain_swarm_results())
    swarm = [e for e in events if e.kind == "swarm_result"]
    assert len(swarm) == 1
    assert "FAILED" not in (swarm[0].data.get("message") or "")
    assert any(
        m["role"] == "assistant"
        and "[swarm result for: audit send loop]" in m["content"]
        for m in s._history
    )
    assert not any(
        m["role"] == "user" and "FAILED" in m["content"]
        for m in s._history
    )
    assert any(e.kind == "pilot_resume" for e in events)
    # Durable display badge must keep analysis_ok so reload stays honest.
    display = [
        row for row in s._display_transcript
        if isinstance(row, dict) and row.get("type") == "swarm_result"
    ]
    assert display
    assert display[-1].get("analysis_ok") is True
    assert display[-1].get("held_for_review") is False
    assert display[-1].get("applied") is False
    assert display[-1].get("error") in (None, "")


def test_drain_held_for_review_display_result_honesty():
    """held_for_review persists on display_result and yields pending_review."""
    s = _session()
    s._swarm_results.put({
        "job_id": "held-job",
        "objective": "ship patch",
        "result": {
            "applied": False,
            "held_for_review": True,
            "files": ["a.ts"],
            "summary": "Patch held for review (ID: rev-abc)",
            "error": None,
            "has_patch_art": True,
            "pending_review": {
                "id": "rev-abc123",
                "summary": "Held 1 files for review",
            },
        },
    })
    events = list(s.drain_swarm_results())
    assert any(e.kind == "pending_review" for e in events)
    pending = [e for e in events if e.kind == "pending_review"][0]
    assert pending.data.get("id") == "rev-abc123"
    display = [
        row for row in s._display_transcript
        if isinstance(row, dict) and row.get("type") == "swarm_result"
    ]
    assert display
    assert display[-1].get("held_for_review") is True
    assert display[-1].get("analysis_ok") is False
    assert display[-1].get("applied") is False
    assert display[-1].get("error") in (None, "")
    assert not any(
        m["role"] == "assistant" and "[swarm FAILED" in m["content"]
        for m in s._history
    )
