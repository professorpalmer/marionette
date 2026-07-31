"""Background swarm results carry the same current-run evidence boundary.

A backgrounded job settles turns after its dispatch, so its result lands in the
transcript surrounded by older conclusions. Without the boundary the pilot has
no way to tell which run produced which line — the exact failure mode the
synchronous digest already guards. These tests hold the drain path to the same
contract, and hold it to degrading quietly (the drain runs on the chat hot path
under the single-writer lock and must never raise).
"""

from __future__ import annotations

import tempfile

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.swarm_run_facts import clear_probe_cache


@pytest.fixture(autouse=True)
def _stub_environment_probe(monkeypatch):
    clear_probe_cache()
    monkeypatch.setattr(
        "harness.environment_fingerprint.compute_environment_fingerprint",
        lambda cwd, strict=True: ({
            "tool_paths": {"pyright": "~/n/pyright", "tsc": ""},
            "browser_path": "",
            "puppetmaster_version": "1.21.5",
            "mcp_server_names": ["discord-mcp"],
        }, ""),
    )
    yield
    clear_probe_cache()


def _session(tmp_path):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = str(tmp_path)
    return ConversationalSession(cfg)


def _assistant_messages(session):
    return [m["content"] for m in session._history if m["role"] == "assistant"]


def test_completed_background_swarm_states_the_boundary(tmp_path):
    session = _session(tmp_path)
    session._local_jobs["job_bg_done"] = {
        "id": "job_bg_done",
        "cwd": str(tmp_path),
        "acceptance_criteria": ["pyright is clean"],
    }
    session._swarm_results.put({
        "job_id": "job_bg_done",
        "objective": "audit the router",
        "result": {
            "applied": True,
            "files": [],
            "summary": "1 finding",
            "artifacts": [{
                "type": "finding",
                "headline": "harness/router.py:88 drops the rejected alternative",
                "execution_ref": {"job_id": "job_bg_done"},
            }],
        },
    })

    list(session.drain_swarm_results())

    message = next(
        m for m in _assistant_messages(session)
        if "[swarm result for: audit the router]" in m
    )
    assert "Exact current job id: job_bg_done" in message
    assert f"Subject cwd (read-only audit target): {tmp_path}" in message
    assert "Direct execution provenance: 1/1 non-routing artifacts." in message
    assert "historical/untrusted" in message
    assert "[not_verified] pyright is clean" in message


def test_stamped_local_job_artifacts_back_a_completed_run(tmp_path):
    """A queued result carries only ``ar_list``; the settled job has the real rows.

    ``_finish_local_job`` runs before the result is enqueued and stamps
    attributed artifacts. Reading only the queued payload made every completed
    background job report ``0/0`` — a run that did the work reading as a run
    that produced nothing.
    """
    session = _session(tmp_path)
    session._local_jobs["job_bg_stamped"] = {
        "id": "job_bg_stamped",
        "cwd": str(tmp_path),
        "acceptance_criteria": ["the router keeps rejected alternatives"],
        "artifacts": [
            {
                "id": "job_bg_stamped:terminal",
                "type": "analysis",
                "headline": "audited harness/router.py",
                "execution_ref": {"job_id": "job_bg_stamped"},
            },
            {
                "id": "job_bg_stamped:finding:0",
                "type": "finding",
                "headline": "harness/router.py:88 drops the rejected alternative",
                "body": "The router keeps rejected alternatives nowhere.",
                "acceptance_criteria": [{
                    "criterion": "the router keeps rejected alternatives",
                    "status": "passed",
                    "evidence": "harness/router.py:88",
                }],
                "execution_ref": {"job_id": "job_bg_stamped"},
            },
        ],
    }
    session._swarm_results.put({
        "job_id": "job_bg_stamped",
        "objective": "audit the router",
        "result": {
            "applied": True,
            "files": [],
            "summary": "1 finding",
            "ar_list": [{"type": "finding", "headline": "raw worker row"}],
        },
    })

    list(session.drain_swarm_results())

    message = next(
        m for m in _assistant_messages(session) if "job_bg_stamped" in m
    )
    assert "Direct execution provenance: 2/2 non-routing artifacts." in message
    assert "[verified] the router keeps rejected alternatives" in message


def test_settled_artifacts_win_over_competing_raw_result_rows(tmp_path):
    session = _session(tmp_path)
    criterion = "the router keeps rejected alternatives"
    session._local_jobs["job_bg_prefer_settled"] = {
        "id": "job_bg_prefer_settled",
        "cwd": str(tmp_path),
        "acceptance_criteria": [criterion],
        "artifacts": [{
            "type": "finding",
            "headline": "harness/router.py:88 retains rejected alternatives",
            "acceptance_criteria": [{
                "criterion": criterion,
                "status": "passed",
                "evidence": "harness/router.py:88",
            }],
            "execution_ref": {"job_id": "job_bg_prefer_settled"},
        }],
    }
    session._swarm_results.put({
        "job_id": "job_bg_prefer_settled",
        "objective": "audit the router",
        "result": {
            "applied": True,
            "files": [],
            "summary": "one finding",
            "ar_list": [],
            "artifacts": [{
                "type": "finding",
                "job_id": "job_bg_prefer_settled",
                "headline": "raw duplicate",
            }],
        },
    })

    list(session.drain_swarm_results())

    message = next(
        m for m in _assistant_messages(session) if "job_bg_prefer_settled" in m
    )
    assert "Direct execution provenance: 1/1 non-routing artifacts." in message
    assert f"[verified] {criterion}" in message


def test_raw_store_rows_restore_exact_current_job_attribution(tmp_path):
    session = _session(tmp_path)
    criterion = "the router keeps rejected alternatives"
    session._local_jobs["job_bg_raw_store"] = {
        "id": "job_bg_raw_store",
        "cwd": str(tmp_path),
        "acceptance_criteria": [criterion],
    }
    session._swarm_results.put({
        "job_id": "job_bg_raw_store",
        "objective": "audit the router",
        "result": {
            "applied": True,
            "files": [],
            "summary": "one finding",
            "ar_list": [],
            "artifacts": [{
                "type": "finding",
                "job_id": "job_bg_raw_store",
                "task_id": "task_current",
                "headline": "harness/router.py:88 retains rejected alternatives",
                "acceptance_criteria": [{
                    "criterion": criterion,
                    "status": "passed",
                    "evidence": "harness/router.py:88",
                }],
            }],
        },
    })

    list(session.drain_swarm_results())

    message = next(
        m for m in _assistant_messages(session) if "job_bg_raw_store" in m
    )
    assert "Direct execution provenance: 1/1 non-routing artifacts." in message
    assert f"[verified] {criterion}" in message


def test_unstamped_background_rows_report_zero_provenance(tmp_path):
    """Normalization must not launder unattributed rows into direct evidence."""
    session = _session(tmp_path)
    session._local_jobs["job_bg_unstamped"] = {
        "id": "job_bg_unstamped",
        "cwd": str(tmp_path),
        "acceptance_criteria": ["the router keeps rejected alternatives"],
        "artifacts": [
            {
                "type": "finding",
                "headline": "harness/router.py:88 drops the rejected alternative",
                "body": "The router keeps rejected alternatives nowhere.",
            },
            {
                "type": "finding",
                "headline": "harness/cost.py:12 bills cached tokens at full price",
                "execution_ref": {"job_id": "job_older"},
            },
        ],
    }
    session._swarm_results.put({
        "job_id": "job_bg_unstamped",
        "objective": "audit the router",
        "result": {"applied": True, "files": [], "summary": "2 findings"},
    })

    list(session.drain_swarm_results())

    message = next(
        m for m in _assistant_messages(session) if "job_bg_unstamped" in m
    )
    assert "Direct execution provenance: 0/2 non-routing artifacts." in message
    # An unstamped row cites the criterion in its body, but cannot settle it.
    assert "[not_verified] the router keeps rejected alternatives" in message


def test_failed_background_swarm_also_states_the_boundary(tmp_path):
    session = _session(tmp_path)
    session._swarm_results.put({
        "job_id": "job_bg_failed",
        "objective": "audit the router",
        "result": {
            "applied": False,
            "files": [],
            "summary": "worker died",
            "error": "provider timeout",
            "artifacts": [],
        },
    })

    list(session.drain_swarm_results())

    message = next(
        m for m in _assistant_messages(session)
        if "[swarm FAILED for: audit the router]" in m
    )
    assert "Exact current job id: job_bg_failed" in message
    assert "Direct execution provenance: 0/0 non-routing artifacts." in message
    assert "Acceptance criteria: none supplied (do not invent any)" in message


def test_missing_optional_tools_stay_readiness_facts(tmp_path):
    session = _session(tmp_path)
    session._swarm_results.put({
        "job_id": "job_bg_ready",
        "objective": "audit the router",
        "result": {"applied": True, "files": [], "summary": "ok", "artifacts": []},
    })

    list(session.drain_swarm_results())

    message = next(
        m for m in _assistant_messages(session) if "job_bg_ready" in m
    )
    assert "[not_verified] tsc (unavailable)" in message
    assert "[not_verified] browser (unavailable)" in message
    assert "never as a product finding, risk, or harness defect" in message


    def test_drain_survives_a_broken_boundary_builder(tmp_path, monkeypatch):
        monkeypatch.setattr(
            "harness.swarm_run_facts.build_swarm_run_facts",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("probe exploded")),
        )
        session = _session(tmp_path)
        session._swarm_results.put({
            "job_id": "job_bg_broken",
            "objective": "audit the router",
            "result": {"applied": True, "files": [], "summary": "ok", "artifacts": []},
        })

        events = list(session.drain_swarm_results())

        assert any(event.kind == "swarm_result" for event in events)
        message = next(
            m for m in _assistant_messages(session)
            if "[swarm result for: audit the router]" in m
        )
        assert "Exact current job id: job_bg_broken" in message
        assert "Evidence boundary construction failed" in message
        assert "Boundary error: RuntimeError" in message
        assert "none settled (boundary unavailable)" in message
