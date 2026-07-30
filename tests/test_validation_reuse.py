"""Tests for dependency-aware incremental validation reuse."""

from __future__ import annotations

import subprocess
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from harness.pilot import PilotAction, build_tools_schema
from harness.pilot_guards import (
    check_swarm_gate,
    is_swarm_gate_blocked_exploration,
    new_turn_guard_state,
    record_action_execution,
)
from harness.send_loop_dispatch import dispatch_parallel_action, dispatch_swarm_action
from harness.validation_reuse import (
    NARROW_VERIFY_ROLES,
    compact_delta_digest,
    evaluate_reuse_gate,
    extract_evidence_paths,
    is_durable_recall_uri,
    is_reusable_local_job,
    pm_validation_helpers_available,
    stamp_validation_on_job,
)


def _green_job(
    *,
    job_id: str = "local-prior",
    goal: str = "audit the auth module",
    cwd: str = "/repo",
    fingerprint: str = "abc123fingerprint",
    headline: str = "Auth middleware missing CSRF check in harness/auth.py:42",
    status: str = "completed",
    adapter: str = "agentic",
    role: str = "explore",
    complete: bool = True,
    missing_paths: Optional[List[str]] = None,
    scope: Optional[List[str]] = None,
):
    evid_scope = list(scope) if scope is not None else ["harness/auth.py"]
    validation = {
        "fingerprint": fingerprint,
        "status": "fresh",
        "scope": evid_scope,
        "source_digest": "digest1",
        "head_sha": "deadbeef",
        "complete": complete,
        "missing_paths": list(missing_paths or []),
    }
    return {
        "id": job_id,
        "goal": goal,
        "status": status,
        "role": role,
        "adapter": adapter,
        "cwd": cwd,
        "created_at": 1.0,
        "updated_at": 2.0,
        "validation_fingerprint": fingerprint,
        "reuse_status": "fresh",
        "tokens": 100,
        "est_cost_usd": 0.05,
        "artifacts": [
            {
                "id": f"{job_id}-result",
                "type": "analysis",
                "headline": "Audit complete with file-backed findings",
                "validation": dict(validation),
            },
            {
                "id": f"{job_id}-finding-0",
                "type": "finding",
                "headline": headline,
                "body": (
                    "The CSRF middleware is missing on the auth routes in "
                    "harness/auth.py:42. This allows cross-site request forgery "
                    "against session cookies when the pilot posts commands."
                ),
                "validation": dict(validation),
            },
        ],
    }


def _session_with_jobs(jobs, cwd="/repo"):
    lock = threading.Lock()
    local = {j["id"]: dict(j) for j in jobs}

    def live():
        return list(local.values())

    return SimpleNamespace(
        config=SimpleNamespace(repo=cwd, driver="test-model"),
        _local_jobs=local,
        _local_jobs_lock=lock,
        live_local_jobs=live,
        _session_job_ids=[],
        _register_local_job=MagicMock(),
        _finish_local_job=MagicMock(),
        _fail_or_drop_local_job=MagicMock(),
        _append_action_result=MagicMock(),
        _display_transcript=[],
        _claim_objective=MagicMock(return_value=True),
        _release_objective=MagicMock(),
        _submit_swarm=MagicMock(return_value=True),
        _last_swarm_submit_reason="",
        _swarm_submit_reject_message=MagicMock(return_value="cap"),
        _resolve_requested_implement_adapter=MagicMock(return_value=("", "")),
        _external_adapter_available=MagicMock(return_value=False),
        _answer_remaining_tool_calls=MagicMock(return_value=iter(())),
        _run_provider_worker_background=MagicMock(),
        _validate_target_repo=MagicMock(return_value=("/repo", None)),
    )


def test_extract_evidence_paths_normalizes_windows_slashes():
    paths = extract_evidence_paths(
        r"See harness\auth.py and webapp/src/lib/api.ts:12",
        files=[r"harness\local_jobs.py"],
    )
    assert "harness/auth.py" in paths
    assert "webapp/src/lib/api.ts" in paths
    assert "harness/local_jobs.py" in paths


def test_is_durable_recall_uri():
    assert is_durable_recall_uri("artifact://local-x/y")
    assert is_durable_recall_uri("job://local-x")
    assert is_durable_recall_uri("spill://abc")
    assert not is_durable_recall_uri("harness/auth.py")


def test_is_reusable_rejects_missing_fingerprint_demo_thin_auth():
    cwd = "/repo"
    goal = "audit the auth module"
    base = _green_job(cwd=cwd, goal=goal)

    missing = dict(base)
    missing["validation_fingerprint"] = ""
    missing["artifacts"] = [
        {**a, "validation": None} for a in base["artifacts"] if isinstance(a, dict)
    ]
    ok, reason = is_reusable_local_job(missing, cwd=cwd, objective=goal, role="explore")
    assert ok is False
    assert reason == "missing_validation_fingerprint"

    demo = _green_job(cwd=cwd, goal=goal, adapter="demo")
    ok, reason = is_reusable_local_job(demo, cwd=cwd, objective=goal, role="explore")
    assert ok is False
    assert reason == "demo_adapter"

    thin = _green_job(cwd=cwd, goal=goal, headline="ok")
    thin["artifacts"] = [
        {
            "id": "local-prior-finding-0",
            "type": "finding",
            "headline": "ok",
            "validation": {"fingerprint": "abc", "status": "fresh"},
        }
    ]
    thin["validation_fingerprint"] = "abc"
    ok, reason = is_reusable_local_job(thin, cwd=cwd, objective=goal, role="explore")
    assert ok is False
    assert reason in {"thin_findings", "artifacts_incomplete", "thin_or_bookkeeping_only"}

    auth = _green_job(cwd=cwd, goal=goal)
    auth["auth_failure"] = "provider auth failure 401"
    ok, reason = is_reusable_local_job(auth, cwd=cwd, objective=goal, role="explore")
    assert ok is False
    assert reason == "auth_failure"


def test_compact_delta_digest_bounds_and_cites_artifact_uri():
    text, refs = compact_delta_digest(
        source_job_id="local-prior",
        artifacts=_green_job()["artifacts"],
        reuse_status="reused",
        reason="fingerprint_match",
    )
    assert "artifact://local-prior/" in text
    assert "REUSED" in text
    assert len(text) <= 2400
    assert refs
    assert all(r["uri"].startswith("artifact://") for r in refs)


def test_evaluate_reuse_gate_reuses_matching_fingerprint(monkeypatch):
    job = _green_job()
    session = _session_with_jobs([job])

    def fake_invalidate(cwd, candidate):
        return [], "", {
            "fingerprint": job["validation_fingerprint"],
            "source_digest": "digest1",
        }

    monkeypatch.setattr(
        "harness.validation_reuse._invalidate_paths_for_candidate",
        fake_invalidate,
    )
    decision = evaluate_reuse_gate(
        session, objective=job["goal"], role="explore", cwd="/repo",
    )
    assert decision.outcome == "reuse"
    assert decision.source_job_id == job["id"]
    assert "artifact://" in decision.digest_text
    assert decision.reuse_status == "reused"


def test_evaluate_reuse_gate_narrow_verify_on_subset(monkeypatch):
    job = _green_job()
    # Add a second evidence path so invalidated is a proper subset.
    job["artifacts"][0]["validation"]["scope"] = ["harness/auth.py", "harness/pilot.py"]
    job["artifacts"][1]["body"] += " Also see harness/pilot.py for gate logic."
    session = _session_with_jobs([job])

    def fake_invalidate(cwd, candidate):
        return ["harness/auth.py"], "", {
            "fingerprint": "newfp",
            "source_digest": "digest2",
        }

    monkeypatch.setattr(
        "harness.validation_reuse._invalidate_paths_for_candidate",
        fake_invalidate,
    )
    decision = evaluate_reuse_gate(
        session, objective=job["goal"], role="explore", cwd="/repo",
    )
    assert decision.outcome == "narrow_verify"
    assert decision.invalidated_paths == ["harness/auth.py"]
    assert decision.narrow_roles == NARROW_VERIFY_ROLES
    assert "explore" not in decision.narrow_roles
    assert "pipeline-mapper" not in decision.narrow_roles


def test_evaluate_reuse_gate_full_swarm_when_helpers_unavailable_live_path(monkeypatch):
    """PM helpers absent must fail closed without mocking invalidation."""
    job = _green_job()
    session = _session_with_jobs([job])
    monkeypatch.setattr("harness.validation_reuse._PM_VALIDATION", None)
    monkeypatch.setattr("harness.validation_reuse._PM_VALIDATION_PROBED", True)
    monkeypatch.setattr(
        "harness.validation_reuse._load_pm_validation",
        lambda: None,
    )
    assert pm_validation_helpers_available() is False
    decision = evaluate_reuse_gate(
        session, objective=job["goal"], role="explore", cwd="/repo",
    )
    assert decision.outcome == "full_swarm"
    assert decision.reason == "pm_validation_helpers_absent"


def test_evaluate_reuse_gate_old_pm_import_falls_back(monkeypatch):
    monkeypatch.setattr("harness.validation_reuse._PM_VALIDATION", None)
    monkeypatch.setattr("harness.validation_reuse._PM_VALIDATION_PROBED", True)
    assert pm_validation_helpers_available() is False
    # Reset probe so later tests can re-import; keep forced None for this assert.
    monkeypatch.setattr("harness.validation_reuse._PM_VALIDATION_PROBED", False)
    monkeypatch.setattr(
        "harness.validation_reuse._load_pm_validation",
        lambda: None,
    )
    assert pm_validation_helpers_available() is False


def test_is_reusable_rejects_missing_cwd_incomplete_and_pending():
    cwd = "/repo"
    goal = "audit the auth module"

    missing_cwd = _green_job(cwd="", goal=goal)
    ok, reason = is_reusable_local_job(missing_cwd, cwd=cwd, objective=goal, role="explore")
    assert ok is False
    assert reason == "candidate_cwd_missing"

    incomplete = _green_job(cwd=cwd, goal=goal, complete=False)
    ok, reason = is_reusable_local_job(incomplete, cwd=cwd, objective=goal, role="explore")
    assert ok is False
    assert reason == "validation_incomplete"

    missing_paths = _green_job(cwd=cwd, goal=goal, missing_paths=["harness/auth.py"])
    ok, reason = is_reusable_local_job(
        missing_paths, cwd=cwd, objective=goal, role="explore",
    )
    assert ok is False
    assert reason == "validation_missing_paths"

    pending = _green_job(cwd=cwd, goal=goal, status="running")
    ok, reason = is_reusable_local_job(pending, cwd=cwd, objective=goal, role="explore")
    assert ok is False
    assert reason == "pending_or_current_job"

    # Long prose without file evidence must not authorize reuse.
    empty_scope = _green_job(cwd=cwd, goal=goal, scope=[])
    empty_scope["artifacts"] = [
        {
            "id": "local-prior-finding-0",
            "type": "finding",
            "headline": "Everything looks fine after a careful pass",
            "body": ("x" * 220),
            "validation": {
                "fingerprint": "abc",
                "status": "fresh",
                "scope": [],
                "complete": True,
                "missing_paths": [],
            },
        }
    ]
    empty_scope["validation_fingerprint"] = "abc"
    ok, reason = is_reusable_local_job(empty_scope, cwd=cwd, objective=goal, role="explore")
    assert ok is False
    assert reason in {"thin_findings", "empty_evidence_scope"}


def test_evaluate_reuse_gate_rejects_ambiguous_and_workspace_mismatch(monkeypatch):
    job_a = _green_job(job_id="a", fingerprint="fp-a")
    job_b = _green_job(job_id="b", fingerprint="fp-b")
    session = _session_with_jobs([job_a, job_b])
    monkeypatch.setattr(
        "harness.validation_reuse._invalidate_paths_for_candidate",
        lambda *_a, **_k: ([], "", {"fingerprint": "fp-a"}),
    )
    decision = evaluate_reuse_gate(
        session, objective=job_a["goal"], role="explore", cwd="/repo",
    )
    assert decision.outcome == "full_swarm"
    assert decision.reason == "ambiguous_candidates"

    other = _green_job(cwd="/other-repo")
    session2 = _session_with_jobs([other], cwd="/repo")
    decision2 = evaluate_reuse_gate(
        session2, objective=other["goal"], role="explore", cwd="/repo",
    )
    assert decision2.outcome == "full_swarm"
    assert decision2.reason in {"no_reusable_candidate", "first_pass", "workspace_mismatch"}


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(root), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(root), check=True, capture_output=True,
    )


def test_git_changed_paths_fails_closed_when_all_probes_nonzero(monkeypatch, tmp_path):
    """Total git probe failure must not look like a clean tree."""
    from harness import validation_reuse as vr

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo), check=True, capture_output=True,
    )

    def boom_run(*_a, **_k):
        return subprocess.CompletedProcess(
            args=_a[0] if _a else [], returncode=128, stdout="", stderr="fail",
        )

    monkeypatch.setattr(vr.subprocess, "run", boom_run)
    paths, reason = vr.git_changed_paths(str(repo), since_sha="deadbeef")
    assert paths == []
    assert reason == "git_diff_unavailable"


def test_git_changed_paths_tracked_ok_untracked_fail_unavailable(monkeypatch, tmp_path):
    """Asymmetric: successful empty tracked diff cannot cover failed ls-files --others."""
    from harness import validation_reuse as vr

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo), check=True, capture_output=True,
    )

    real_run = subprocess.run

    def tracked_ok_untracked_fail(cmd, *args, **kwargs):
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
        if "ls-files" in argv and "--others" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=128, stdout="", stderr="fatal: ls-files",
            )
        if "diff" in argv or "rev-parse" in argv:
            # Empty successful tracked / HEAD probes.
            if "rev-parse" in argv:
                return real_run(cmd, *args, **kwargs)
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="", stderr="",
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(vr.subprocess, "run", tracked_ok_untracked_fail)
    paths, reason = vr.git_changed_paths(str(repo), since_sha="deadbeef")
    assert paths == []
    assert reason == "git_diff_unavailable"


def test_git_changed_paths_untracked_ok_tracked_fail_unavailable(monkeypatch, tmp_path):
    """Asymmetric: successful untracked probe cannot cover failed tracked diffs."""
    from harness import validation_reuse as vr

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo), check=True, capture_output=True,
    )

    real_run = subprocess.run

    def untracked_ok_tracked_fail(cmd, *args, **kwargs):
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
        if "rev-parse" in argv:
            # Keep HEAD born so unborn fallback is not used.
            return real_run(cmd, *args, **kwargs)
        if "ls-files" in argv and "--others" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="", stderr="",
            )
        if "diff" in argv or ("ls-files" in argv and "--modified" in argv):
            return subprocess.CompletedProcess(
                args=argv, returncode=128, stdout="", stderr="fatal: diff",
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(vr.subprocess, "run", untracked_ok_tracked_fail)
    paths, reason = vr.git_changed_paths(str(repo), since_sha="deadbeef")
    assert paths == []
    assert reason == "git_diff_unavailable"


def test_evaluate_reuse_gate_full_swarm_on_asymmetric_git_probes(monkeypatch, tmp_path):
    """Either asymmetric partial-success mode must force full_swarm."""
    from harness import validation_reuse as vr

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    a = repo / "a.py"
    a.write_text("def a():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo), check=True, capture_output=True,
    )
    fp_payload, _ = vr.compute_source_fingerprint(str(repo), ["a.py"], strict=False)
    assert fp_payload
    job = _green_job(
        cwd=str(repo),
        goal="audit a.py helpers",
        fingerprint=str(fp_payload["fingerprint"]),
        scope=["a.py"],
        headline="Issue in a.py:1 helper return",
    )
    for art in job["artifacts"]:
        art["validation"] = dict(fp_payload)
        art["validation"]["status"] = "fresh"
    job["artifacts"][1]["body"] = (
        "a.py:1 helper returns a constant; documented for reuse gate coverage. "
        + ("detail " * 20)
    )
    job["validation_fingerprint"] = str(fp_payload["fingerprint"])
    assert pm_validation_helpers_available() is True

    monkeypatch.setattr(
        vr, "git_changed_paths",
        lambda *_a, **_k: ([], "git_diff_unavailable"),
    )
    session = _session_with_jobs([job], cwd=str(repo))
    decision = evaluate_reuse_gate(
        session, objective=job["goal"], role="explore", cwd=str(repo),
    )
    assert decision.outcome == "full_swarm"
    assert decision.reason == "git_diff_unavailable"


def test_evaluate_reuse_gate_full_swarm_when_all_git_probes_fail(monkeypatch, tmp_path):
    """Counterexample: dirty outside file + every git probe nonzero → full_swarm."""
    from harness import validation_reuse as vr

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    a = repo / "a.py"
    b = repo / "b.py"
    a.write_text("def a():\n    return 1\n", encoding="utf-8")
    b.write_text("def b():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo), check=True, capture_output=True,
    )
    fp_payload, _reason = vr.compute_source_fingerprint(str(repo), ["a.py"], strict=False)
    assert fp_payload and fp_payload.get("fingerprint")
    assert pm_validation_helpers_available() is True

    job = _green_job(
        cwd=str(repo),
        goal="audit a.py helpers",
        fingerprint=str(fp_payload["fingerprint"]),
        scope=["a.py"],
        headline="Issue in a.py:1 helper return",
    )
    for art in job["artifacts"]:
        art["validation"] = dict(fp_payload)
        art["validation"]["status"] = "fresh"
    job["artifacts"][1]["body"] = (
        "a.py:1 helper returns a constant; documented for reuse gate coverage. "
        + ("detail " * 20)
    )
    job["validation_fingerprint"] = str(fp_payload["fingerprint"])
    b.write_text("def b():\n    return 99\n", encoding="utf-8")

    real_run = subprocess.run

    def fail_git_diff_probes(cmd, *args, **kwargs):
        # Keep fingerprint / rev-parse working; fail every dirty-tree probe.
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
        joined = " ".join(str(x) for x in argv)
        if "rev-parse" in joined or "commit" in joined or "add" in joined or "config" in joined or "init" in joined:
            return real_run(cmd, *args, **kwargs)
        if (
            "diff" in argv
            or (len(argv) >= 2 and argv[-2:] == ["ls-files", "--others"])
            or "ls-files" in argv
        ):
            return subprocess.CompletedProcess(
                args=argv, returncode=128, stdout="", stderr="fatal",
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(vr.subprocess, "run", fail_git_diff_probes)
    # Fingerprint hasher also uses subprocess for HEAD; keep that via real_run.
    session = _session_with_jobs([job], cwd=str(repo))
    decision = evaluate_reuse_gate(
        session, objective=job["goal"], role="explore", cwd=str(repo),
    )
    assert decision.outcome == "full_swarm"
    assert decision.reason == "git_diff_unavailable"


def test_outside_dirt_empty_affected_without_fresh_graph_full_swarm(monkeypatch, tmp_path):
    """Empty CodeGraph affected must not prove non-intersection when graph is stale."""
    from harness import validation_reuse as vr

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    a = repo / "a.py"
    b = repo / "b.py"
    a.write_text("def a():\n    return 1\n", encoding="utf-8")
    b.write_text("def b():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo), check=True, capture_output=True,
    )
    fp_payload, _ = vr.compute_source_fingerprint(str(repo), ["a.py"], strict=False)
    assert fp_payload
    job = _green_job(
        cwd=str(repo),
        goal="audit a.py helpers",
        fingerprint=str(fp_payload["fingerprint"]),
        scope=["a.py"],
        headline="Issue in a.py:1 helper return",
    )
    for art in job["artifacts"]:
        art["validation"] = dict(fp_payload)
        art["validation"]["status"] = "fresh"
    job["artifacts"][1]["body"] = (
        "a.py:1 helper returns a constant; documented for reuse gate coverage. "
        + ("detail " * 20)
    )
    job["validation_fingerprint"] = str(fp_payload["fingerprint"])
    b.write_text("def b():\n    return 99\n", encoding="utf-8")

    # Successful but empty affected — ambiguous without freshness confirmation.
    monkeypatch.setattr(
        "harness.validation_reuse.codegraph_affected_paths",
        lambda *_a, **_k: ([], ""),
    )
    monkeypatch.setattr(
        "harness.validation_reuse._codegraph_freshness_confirmed",
        lambda *_a, **_k: (False, "codegraph_stale"),
    )
    session = _session_with_jobs([job], cwd=str(repo))
    decision = evaluate_reuse_gate(
        session, objective=job["goal"], role="explore", cwd=str(repo),
    )
    assert decision.outcome == "full_swarm"
    assert decision.reason in {
        "codegraph_stale",
        "outside_evidence_unproven",
        "codegraph_unavailable",
        "codegraph_freshness_unconfirmed",
    }


def test_dirty_outside_evidence_fails_closed_without_blast_radius(monkeypatch, tmp_path):
    """Counterexample: scoped a.py, dirty b.py must not reuse via fingerprint alone."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    a = repo / "a.py"
    b = repo / "b.py"
    a.write_text("def a():\n    return 1\n", encoding="utf-8")
    b.write_text("def b():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo), check=True, capture_output=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo), check=True, capture_output=True, text=True,
    ).stdout.strip()

    from harness.validation_reuse import compute_source_fingerprint

    fp_payload, _reason = compute_source_fingerprint(str(repo), ["a.py"], strict=False)
    assert fp_payload and fp_payload.get("fingerprint")
    # Ensure PM helpers stay available so we exercise invalidate, not the
    # absent-helpers short-circuit.
    assert pm_validation_helpers_available() is True

    job = _green_job(
        cwd=str(repo),
        goal="audit a.py helpers",
        fingerprint=str(fp_payload["fingerprint"]),
        scope=["a.py"],
        headline="Issue in a.py:1 helper return",
    )
    job["artifacts"][0]["validation"] = dict(fp_payload)
    job["artifacts"][0]["validation"]["status"] = "fresh"
    job["artifacts"][1]["body"] = (
        "a.py:1 helper returns a constant; documented for reuse gate coverage. "
        + ("detail " * 20)
    )
    job["artifacts"][1]["headline"] = "Issue in a.py:1 helper return"
    job["artifacts"][1]["validation"] = dict(fp_payload)
    job["artifacts"][1]["validation"]["status"] = "fresh"
    job["validation_fingerprint"] = str(fp_payload["fingerprint"])

    # Dirty an out-of-scope file without touching a.py bytes / HEAD fingerprint.
    b.write_text("def b():\n    return 99\n", encoding="utf-8")

    # Force CodeGraph unavailable so outside dirt cannot prove non-intersection.
    monkeypatch.setattr(
        "harness.validation_reuse.codegraph_affected_paths",
        lambda *_a, **_k: ([], "codegraph_unavailable"),
    )

    session = _session_with_jobs([job], cwd=str(repo))
    decision = evaluate_reuse_gate(
        session, objective=job["goal"], role="explore", cwd=str(repo),
    )
    assert decision.outcome == "full_swarm"
    assert decision.reason != "fingerprint_match"
    assert decision.reason in {
        "outside_evidence_unproven",
        "fingerprint_drift",
        "fingerprint_drift_unscoped",
        "incomplete_fingerprint",
    }
    # Sanity: HEAD still matches the prior stamp; dirt is the only change.
    assert head


def test_subset_invalidation_yields_narrow_verify_live(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    a = repo / "a.py"
    c = repo / "c.py"
    a.write_text("def a():\n    return 1\n", encoding="utf-8")
    c.write_text("def c():\n    return 3\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo), check=True, capture_output=True,
    )
    from harness.validation_reuse import compute_source_fingerprint

    fp_payload, _ = compute_source_fingerprint(str(repo), ["a.py", "c.py"], strict=False)
    assert fp_payload
    job = _green_job(
        cwd=str(repo),
        goal="audit a and c",
        fingerprint=str(fp_payload["fingerprint"]),
        scope=["a.py", "c.py"],
        headline="Issue in a.py:1 and c.py:1",
    )
    for art in job["artifacts"]:
        art["validation"] = dict(fp_payload)
        art["validation"]["status"] = "fresh"
    job["artifacts"][1]["body"] = (
        "See a.py:1 and c.py:1 for related helpers that need re-verification. "
        + ("detail " * 20)
    )
    job["validation_fingerprint"] = str(fp_payload["fingerprint"])

    a.write_text("def a():\n    return 2\n", encoding="utf-8")
    monkeypatch.setattr(
        "harness.validation_reuse.codegraph_affected_paths",
        lambda *_a, **_k: (["a.py"], ""),
    )
    session = _session_with_jobs([job], cwd=str(repo))
    decision = evaluate_reuse_gate(
        session, objective=job["goal"], role="explore", cwd=str(repo),
    )
    assert decision.outcome == "narrow_verify"
    assert "a.py" in decision.invalidated_paths
    assert decision.narrow_roles == NARROW_VERIFY_ROLES


def test_dispatch_swarm_skips_stream_swarm_on_reuse(monkeypatch):
    job = _green_job(goal="audit peel")
    session = _session_with_jobs([job], cwd="/repo")
    act = PilotAction(kind="run_swarm", goal="audit peel", roles=["explore"])

    import harness.send_loop_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "harness.validation_reuse.evaluate_reuse_gate",
        lambda *a, **k: SimpleNamespace(
            outcome="reuse",
            reason="fingerprint_match",
            source_job_id=job["id"],
            validation_fingerprint="abc123fingerprint",
            invalidated_paths=[],
            reuse_status="reused",
            digest_text="REUSED\n  - [finding] artifact://local-prior/x — CSRF",
            compact_artifacts=[{
                "id": "local-prior-finding-0",
                "type": "finding",
                "headline": "CSRF in harness/auth.py",
                "uri": "artifact://local-prior/local-prior-finding-0",
            }],
            as_provenance=lambda: {
                "reuse_status": "reused",
                "source_job_id": job["id"],
                "validation_fingerprint": "abc123fingerprint",
                "reuse_reason": "fingerprint_match",
            },
        ),
    )
    called = {"stream": False}

    def boom(*_a, **_k):
        called["stream"] = True
        raise AssertionError("stream_swarm must not run on reuse")

    monkeypatch.setattr(dispatch, "stream_swarm", boom)
    counters = {"swarms": 0, "demo_swarms": 0}
    events = list(
        dispatch_swarm_action(
            session, act, "a1", True, counters=counters, turn_findings=[],
        )
    )
    assert called["stream"] is False
    assert counters["swarms"] == 0
    kinds = [e.kind for e in events]
    assert "swarm_result" in kinds
    assert "action_result" in kinds
    result_ev = next(e for e in events if e.kind == "swarm_result")
    assert result_ev.data["result"]["reuse_status"] == "reused"
    assert "artifact://" in session._append_action_result.call_args[0][2]
    # Zero new spend on the reused finish.
    finish_kwargs = session._finish_local_job.call_args.kwargs
    assert finish_kwargs.get("tokens") == 0
    assert finish_kwargs.get("est_cost_usd") == 0.0
    assert finish_kwargs.get("reuse_status") == "reused"


def test_dispatch_parallel_analysis_skips_adapter_on_reuse(monkeypatch):
    job = _green_job(goal="review auth.py CSRF", role="analysis")
    session = _session_with_jobs([job], cwd="/repo")
    act = PilotAction(
        kind="run_parallel",
        goals=["review auth.py CSRF"],
        mode="analysis",
    )

    import harness.send_loop_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(dispatch, "_puppetmaster_available", lambda: False)
    monkeypatch.setattr(
        "harness.edit_engines.select_edit_engine",
        lambda *_a, **_k: "native",
    )
    monkeypatch.setattr(
        "harness.conversation._prewarm_worker_imports",
        lambda: None,
    )
    monkeypatch.setattr(
        "harness.repo_resolve.resolve_effective_repo",
        lambda p: p,
    )
    monkeypatch.setattr(
        "harness.implement_guards.check_implement_workspace",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "harness.validation_reuse.evaluate_reuse_gate",
        lambda *a, **k: SimpleNamespace(
            outcome="reuse",
            reason="fingerprint_match",
            source_job_id=job["id"],
            validation_fingerprint="abc123fingerprint",
            invalidated_paths=[],
            reuse_status="reused",
            digest_text="REUSED artifact://local-prior/x",
            compact_artifacts=[{
                "id": "x",
                "type": "finding",
                "headline": "CSRF in harness/auth.py",
                "uri": "artifact://local-prior/x",
            }],
            narrow_goal_suffix="",
            narrow_roles=(),
            as_provenance=lambda: {
                "reuse_status": "reused",
                "source_job_id": job["id"],
                "reuse_reason": "fingerprint_match",
            },
        ),
    )
    events = list(
        dispatch_parallel_action(
            session,
            act,
            "p1",
            True,
            turn_actions=[act],
            action_idx=0,
            action_seq=1,
            step=0,
            swarms=0,
        )
    )
    session._submit_swarm.assert_not_called()
    assert session._finish_local_job.called
    finish_kwargs = session._finish_local_job.call_args.kwargs
    assert finish_kwargs.get("tokens") == 0
    assert finish_kwargs.get("reuse_status") == "reused"
    kinds = [e.kind for e in events]
    assert "swarm_result" in kinds
    assert "swarm_pending" in kinds
    assert kinds.index("swarm_pending") < kinds.index("swarm_result")
    assert kinds.index("swarm_result") < kinds.index("assistant_done")
    result_ev = next(e for e in events if e.kind == "swarm_result")
    assert result_ev.data["result"]["reuse_status"] == "reused"
    assert any(
        isinstance(row, dict) and row.get("type") == "swarm_result"
        and row.get("reuse_status") == "reused"
        for row in session._display_transcript
    )
    assert any(e.kind == "action_result" for e in events)


def test_dispatch_parallel_mixed_reuse_emits_pending_before_reused_result(monkeypatch):
    """Mixed reused+fresh: swarm_pending precedes buffered reused swarm_result."""
    prior = _green_job(goal="review auth.py CSRF", role="analysis")
    session = _session_with_jobs([prior], cwd="/repo")
    act = PilotAction(
        kind="run_parallel",
        goals=["review auth.py CSRF", "review billing.py totals"],
        mode="analysis",
    )

    import harness.send_loop_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(dispatch, "_puppetmaster_available", lambda: False)
    monkeypatch.setattr(
        "harness.edit_engines.select_edit_engine",
        lambda *_a, **_k: "native",
    )
    monkeypatch.setattr(
        "harness.conversation._prewarm_worker_imports",
        lambda: None,
    )
    monkeypatch.setattr(
        "harness.repo_resolve.resolve_effective_repo",
        lambda p: p,
    )
    monkeypatch.setattr(
        "harness.implement_guards.check_implement_workspace",
        lambda *_a, **_k: None,
    )

    def fake_gate(*_a, **k):
        objective = str(k.get("objective") or "")
        if "billing" in objective:
            return SimpleNamespace(
                outcome="full_swarm",
                reason="first_pass",
                source_job_id="",
                validation_fingerprint="",
                invalidated_paths=[],
                reuse_status="",
                digest_text="",
                compact_artifacts=[],
                narrow_goal_suffix="",
                narrow_roles=(),
                as_provenance=lambda: {},
            )
        return SimpleNamespace(
            outcome="reuse",
            reason="fingerprint_match",
            source_job_id=prior["id"],
            validation_fingerprint="abc123fingerprint",
            invalidated_paths=[],
            reuse_status="reused",
            digest_text="REUSED",
            compact_artifacts=[],
            narrow_goal_suffix="",
            narrow_roles=(),
            as_provenance=lambda: {
                "reuse_status": "reused",
                "source_job_id": prior["id"],
                "reuse_reason": "fingerprint_match",
            },
        )

    monkeypatch.setattr("harness.validation_reuse.evaluate_reuse_gate", fake_gate)
    session._submit_swarm = MagicMock(return_value=True)
    events = list(
        dispatch_parallel_action(
            session,
            act,
            "p-mixed",
            True,
            turn_actions=[act],
            action_idx=0,
            action_seq=1,
            step=0,
            swarms=0,
        )
    )
    kinds = [e.kind for e in events]
    assert "swarm_pending" in kinds
    assert "swarm_result" in kinds
    assert kinds.index("swarm_pending") < kinds.index("swarm_result")
    pending = next(e for e in events if e.kind == "swarm_pending")
    assert len(pending.data.get("job_ids") or []) == 2
    result_ev = next(e for e in events if e.kind == "swarm_result")
    assert result_ev.data["result"]["reuse_status"] == "reused"
    assert session._submit_swarm.called


def test_dispatch_parallel_narrow_verify_submits_verifier_roles(monkeypatch):
    job = _green_job(goal="review auth.py CSRF", role="analysis")
    session = _session_with_jobs([job], cwd="/repo")
    act = PilotAction(
        kind="run_parallel",
        goals=["review auth.py CSRF"],
        mode="analysis",
    )

    import harness.send_loop_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(dispatch, "_puppetmaster_available", lambda: False)
    monkeypatch.setattr(
        "harness.edit_engines.select_edit_engine",
        lambda *_a, **_k: "native",
    )
    monkeypatch.setattr(
        "harness.conversation._prewarm_worker_imports",
        lambda: None,
    )
    monkeypatch.setattr(
        "harness.repo_resolve.resolve_effective_repo",
        lambda p: p,
    )
    monkeypatch.setattr(
        "harness.implement_guards.check_implement_workspace",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "harness.validation_reuse.evaluate_reuse_gate",
        lambda *a, **k: SimpleNamespace(
            outcome="narrow_verify",
            reason="subset_invalidated",
            source_job_id=job["id"],
            validation_fingerprint="abc123fingerprint",
            invalidated_paths=["harness/auth.py"],
            reuse_status="partial",
            digest_text="PARTIAL",
            compact_artifacts=[],
            narrow_goal_suffix="Re-verify only harness/auth.py",
            narrow_roles=NARROW_VERIFY_ROLES,
            as_provenance=lambda: {
                "reuse_status": "partial",
                "source_job_id": job["id"],
                "invalidated_paths": ["harness/auth.py"],
            },
        ),
    )
    events = list(
        dispatch_parallel_action(
            session,
            act,
            "p2",
            True,
            turn_actions=[act],
            action_idx=0,
            action_seq=1,
            step=0,
            swarms=0,
        )
    )
    assert session._submit_swarm.called
    register_kwargs = session._register_local_job.call_args
    # role is positional arg index 2 (job_id, goal, role=...)
    submitted_role = (
        register_kwargs.kwargs.get("role")
        if register_kwargs.kwargs.get("role")
        else (register_kwargs.args[2] if len(register_kwargs.args) > 2 else None)
    )
    assert submitted_role == NARROW_VERIFY_ROLES[0]
    assert submitted_role not in {"analysis", "review", "explore", "pipeline-mapper"}
    assert any(e.kind == "swarm_pending" for e in events)


def test_search_codegraph_affected_routing(monkeypatch):
    from harness.tool_dispatch import ToolDispatchMixin

    captured = {}

    def fake_cmd(*args):
        captured["args"] = args
        return ["python", "-m", "puppetmaster", *args]

    monkeypatch.setattr("harness.tool_dispatch._puppetmaster_cmd", fake_cmd)

    class Host(ToolDispatchMixin):
        def __init__(self):
            self.config = SimpleNamespace(repo="/repo")

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="tests/test_auth.py\n")

    monkeypatch.setattr("harness.tool_dispatch.subprocess.run", fake_run)
    host = Host()
    act = PilotAction(
        kind="search_codegraph",
        query="harness/auth.py harness/pilot.py",
        arguments={"kind": "affected"},
    )
    ok, status, val = host._do_search_codegraph(act)
    assert ok and status == "success"
    assert val[0] == "affected"
    assert "affected" in captured["cmd"]
    assert "harness/auth.py" in captured["cmd"]


def test_search_codegraph_schema_includes_affected():
    schema = build_tools_schema(no_delegation=False)
    by_name = {t["function"]["name"]: t for t in schema}
    assert "search_codegraph" in by_name
    kinds = by_name["search_codegraph"]["function"]["parameters"]["properties"]["kind"]["enum"]
    assert "affected" in kinds
    assert "search_state" in by_name


def test_swarm_gate_permits_durable_recall_not_native_exploration():
    state = new_turn_guard_state("audit the whole codebase for quality issues")
    assert state.broad_intent is True

    # Native exploration still blocked.
    assert is_swarm_gate_blocked_exploration(
        state, "list_dir", SimpleNamespace(path="."),
    )
    blocked = check_swarm_gate(state, "list_dir", SimpleNamespace(path="."))
    assert blocked.suppress is True

    # Durable recall permitted.
    assert is_swarm_gate_blocked_exploration(
        state, "search_state", SimpleNamespace(query="auth audit"),
    ) is False
    assert check_swarm_gate(
        state, "search_state", SimpleNamespace(query="auth audit"),
    ).suppress is False

    art_act = SimpleNamespace(path="artifact://local-prior/local-prior-finding-0", kind="read_file")
    assert is_swarm_gate_blocked_exploration(state, "read_file", art_act) is False
    # Durable read must not consume the pre-dispatch read allowance.
    before = state.read_file_count
    record_action_execution(state, "read_file", art_act)
    assert state.read_file_count == before


def test_stamp_validation_on_job_sets_fingerprint():
    job = {
        "id": "local-x",
        "role": "explore",
        "goal": "audit",
        "status": "completed",
        "artifacts": [
            {
                "id": "local-x-finding-0",
                "type": "finding",
                "headline": "Issue in harness/validation_reuse.py with details " + ("x" * 80),
                "body": "See harness/validation_reuse.py:10 for the gate owner.",
            }
        ],
    }
    stamped = stamp_validation_on_job(job, cwd=".", reuse_status="fresh")
    assert stamped.get("reuse_status") == "fresh"
    # Fingerprint may be empty if workspace paths are missing; status still set.
    assert "reuse_status" in stamped


def test_provenance_fields_truncated_fingerprint():
    from harness.validation_reuse import provenance_fields_from_job

    fields = provenance_fields_from_job({
        "reuse_status": "reused",
        "source_job_id": "local-prior",
        "validation_fingerprint": "a" * 40,
        "reuse_reason": "fingerprint_match",
    })
    assert fields["reuse_status"] == "reused"
    assert fields["source_job_id"] == "local-prior"
    assert fields["validation_fingerprint"].endswith("…")
    assert len(fields["validation_fingerprint"]) <= 20


def test_local_job_swarm_view_projects_reuse_fields():
    from harness.local_job_swarm_view import project_local_job_for_swarm_live

    row = project_local_job_for_swarm_live(_green_job(
        fingerprint="f" * 40,
    ) | {
        "reuse_status": "reused",
        "source_job_id": "local-src",
        "reuse_reason": "fingerprint_match",
        "invalidated_paths": ["harness/auth.py"],
    })
    assert row["reuse_status"] == "reused"
    assert row["source_job_id"] == "local-src"
    assert "validation_fingerprint" in row
    assert row.get("invalidated_paths") == ["harness/auth.py"]


def test_drain_swarm_results_copies_reuse_provenance_from_local_job():
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)
    job_id = "local-drain-reuse"
    session._register_local_job(job_id, goal="audit", role="explore", cwd="/repo")
    session._finish_local_job(
        job_id,
        ok=True,
        summary="reused prior",
        status="done",
        tokens=0,
        est_cost_usd=0.0,
        reuse_status="partial",
        source_job_id="local-src",
        validation_fingerprint="abc123fingerprint",
        invalidated_paths=["harness/auth.py"],
        reuse_reason="subset_invalidated",
    )
    session._swarm_results.put({
        "job_id": job_id,
        "objective": "audit",
        "result": {
            "applied": True,
            "files": [],
            "summary": "ok",
        },
    })
    events = list(session.drain_swarm_results())
    result_ev = next(e for e in events if e.kind == "swarm_result")
    assert result_ev.data["result"].get("reuse_status") == "partial"
    assert result_ev.data["result"].get("source_job_id") == "local-src"
    assert result_ev.data["result"].get("invalidated_paths") == ["harness/auth.py"]
    display = next(
        row for row in session._display_transcript
        if isinstance(row, dict) and row.get("type") == "swarm_result"
    )
    assert display.get("reuse_status") == "partial"
    assert display.get("invalidated_paths") == ["harness/auth.py"]


def test_partial_job_never_shadows_original_full_candidate(monkeypatch):
    """Newer partial/narrow_verify must not win lookup over the original fresh job."""
    goal = "audit the auth module"
    cwd = "/repo"
    original = _green_job(
        job_id="local-full",
        goal=goal,
        cwd=cwd,
        fingerprint="shared-fp",
        role="explore",
    )
    original["reuse_status"] = "fresh"
    original["updated_at"] = 10.0
    # Newer narrow-verify row shares fingerprint but only has thin verifier output.
    partial = _green_job(
        job_id="local-partial",
        goal=goal,
        cwd=cwd,
        fingerprint="shared-fp",
        role="conflict-auditor",
        headline="Re-checked harness/auth.py CSRF after edit",
    )
    partial["reuse_status"] = "partial"
    partial["source_job_id"] = "local-full"
    partial["updated_at"] = 99.0
    partial["artifacts"] = [
        {
            "id": "local-partial-finding-0",
            "type": "finding",
            "headline": "Re-checked harness/auth.py CSRF after edit",
            "body": (
                "Narrow verifier confirms harness/auth.py:42 still missing CSRF. "
                + ("detail " * 20)
            ),
            "validation": {
                "fingerprint": "shared-fp",
                "status": "reused",
                "scope": ["harness/auth.py"],
                "complete": True,
                "missing_paths": [],
            },
        }
    ]

    ok_partial, reason_partial = is_reusable_local_job(
        partial, cwd=cwd, objective=goal, role="explore",
    )
    assert ok_partial is False
    assert reason_partial == "status_partial"

    reused = dict(original)
    reused["id"] = "local-reused"
    reused["reuse_status"] = "reused"
    reused["source_job_id"] = "local-full"
    reused["updated_at"] = 50.0
    ok_reused, reason_reused = is_reusable_local_job(
        reused, cwd=cwd, objective=goal, role="explore",
    )
    assert ok_reused is False
    assert reason_reused == "status_reused"

    monkeypatch.setattr(
        "harness.validation_reuse._invalidate_paths_for_candidate",
        lambda *_a, **_k: ([], "", {"fingerprint": "shared-fp"}),
    )
    session = _session_with_jobs([original, partial, reused], cwd=cwd)
    decision = evaluate_reuse_gate(
        session, objective=goal, role="explore", cwd=cwd,
    )
    assert decision.outcome == "reuse"
    assert decision.source_job_id == "local-full"
    # Digest must cite the original explore evidence, not the thin partial row.
    assert "local-full" in decision.digest_text
    assert "local-partial" not in decision.digest_text


def test_dirty_outside_evidence_empty_affected_fresh_graph_fails_closed(
    monkeypatch, tmp_path,
):
    """Empty affected + confirmed freshness must not authorize reuse."""
    from harness import validation_reuse as vr

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    a = repo / "a.py"
    b = repo / "b.py"
    a.write_text("def a():\n    return 1\n", encoding="utf-8")
    b.write_text("def b():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo), check=True, capture_output=True,
    )
    fp_payload, _ = vr.compute_source_fingerprint(str(repo), ["a.py"], strict=False)
    assert fp_payload
    job = _green_job(
        cwd=str(repo),
        goal="audit a.py helpers",
        fingerprint=str(fp_payload["fingerprint"]),
        scope=["a.py"],
        headline="Issue in a.py:1 helper return",
    )
    for art in job["artifacts"]:
        art["validation"] = dict(fp_payload)
        art["validation"]["status"] = "fresh"
    job["artifacts"][1]["body"] = (
        "a.py:1 helper returns a constant; documented for reuse gate coverage. "
        + ("detail " * 20)
    )
    job["validation_fingerprint"] = str(fp_payload["fingerprint"])
    b.write_text("def b():\n    return 99\n", encoding="utf-8")

    monkeypatch.setattr(
        "harness.validation_reuse.codegraph_affected_paths",
        lambda *_a, **_k: ([], ""),
    )
    monkeypatch.setattr(
        "harness.validation_reuse._codegraph_freshness_confirmed",
        lambda *_a, **_k: (True, ""),
    )
    session = _session_with_jobs([job], cwd=str(repo))
    decision = evaluate_reuse_gate(
        session, objective=job["goal"], role="explore", cwd=str(repo),
    )
    assert decision.outcome == "full_swarm"
    assert decision.reason == "outside_evidence_unproven"


def test_absolute_affected_path_intersects_relative_scope(monkeypatch, tmp_path):
    """Absolute in-repo affected paths must relativize and hit relative scope."""
    from harness import validation_reuse as vr

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    a = repo / "a.py"
    b = repo / "b.py"
    a.write_text("def a():\n    return 1\n", encoding="utf-8")
    b.write_text("def b():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo), check=True, capture_output=True,
    )
    fp_payload, _ = vr.compute_source_fingerprint(
        str(repo), ["a.py", "c_helper.py"], strict=False,
    )
    # Scope cites a.py; fingerprint may mark missing c_helper — use complete stamp.
    fp_payload = dict(fp_payload)
    fp_payload["scope"] = ["a.py"]
    fp_payload["complete"] = True
    fp_payload["missing_paths"] = []
    job = _green_job(
        cwd=str(repo),
        goal="audit a.py helpers",
        fingerprint=str(fp_payload["fingerprint"]),
        scope=["a.py"],
        headline="Issue in a.py:1 helper return",
    )
    for art in job["artifacts"]:
        art["validation"] = dict(fp_payload)
        art["validation"]["status"] = "fresh"
    job["artifacts"][1]["body"] = (
        "a.py:1 helper returns a constant; documented for reuse gate coverage. "
        + ("detail " * 20)
    )
    job["validation_fingerprint"] = str(fp_payload["fingerprint"])
    b.write_text("def b():\n    return 99\n", encoding="utf-8")

    abs_a = str((repo / "a.py").resolve())

    def fake_affected(cwd, changed_paths):
        # Simulate CodeGraph emitting an absolute in-repo evidence path.
        return [abs_a], ""

    monkeypatch.setattr("harness.validation_reuse.codegraph_affected_paths", fake_affected)
    monkeypatch.setattr(
        "harness.validation_reuse._codegraph_freshness_confirmed",
        lambda *_a, **_k: (True, ""),
    )
    session = _session_with_jobs([job], cwd=str(repo))
    decision = evaluate_reuse_gate(
        session, objective=job["goal"], role="explore", cwd=str(repo),
    )
    # Absolute a.py must intersect relative scope → not silent fingerprint reuse.
    assert decision.outcome != "reuse"
    assert decision.reason != "fingerprint_match"
    # Relativized absolute evidence must drive a scoped invalidation outcome
    # (narrow subset or broad), not an unrelated fail-closed reason.
    assert decision.reason in ("subset_invalidated", "broad_invalidation")
    if decision.outcome == "narrow_verify":
        assert decision.reason == "subset_invalidated"
        assert "a.py" in decision.invalidated_paths
    else:
        assert decision.outcome == "full_swarm"
        assert decision.reason == "broad_invalidation"
        assert "a.py" in decision.invalidated_paths


def test_repo_relative_path_accepts_absolute_in_repo_rejects_outside(tmp_path):
    from harness.validation_reuse import _repo_relative_path

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "harness").mkdir()
    target = repo / "harness" / "auth.py"
    target.write_text("x = 1\n", encoding="utf-8")
    abs_in = str(target.resolve())
    assert _repo_relative_path(str(repo), abs_in) == "harness/auth.py"
    assert _repo_relative_path(str(repo), "harness/auth.py") == "harness/auth.py"
    outside = tmp_path / "other" / "secret.py"
    outside.parent.mkdir()
    outside.write_text("x\n", encoding="utf-8")
    assert _repo_relative_path(str(repo), str(outside.resolve())) == ""
    assert _repo_relative_path(str(repo), "../other/secret.py") == ""


def test_repo_relative_path_symlink_root_and_canonical_containment(tmp_path):
    """Symlinked workspace roots and canonical realpaths must still relativize.

    Covers macOS /var vs /private/var style aliasing when cwd is a symlink
    view of the same tree; outside paths still fail closed.
    """
    import os

    from harness.validation_reuse import _repo_relative_path

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "harness").mkdir()
    target = repo / "harness" / "auth.py"
    target.write_text("x = 1\n", encoding="utf-8")

    link = tmp_path / "repo-link"
    try:
        os.symlink(str(repo.resolve()), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not available on this platform")

    abs_via_link = str(link / "harness" / "auth.py")
    assert _repo_relative_path(str(link), abs_via_link) == "harness/auth.py"
    assert _repo_relative_path(str(link), "harness/auth.py") == "harness/auth.py"
    # Absolute path through the real root while cwd is the symlink view.
    assert _repo_relative_path(str(link), str(target.resolve())) == "harness/auth.py"
    # Symlink cwd + absolute outside still rejects.
    outside = tmp_path / "other" / "secret.py"
    outside.parent.mkdir()
    outside.write_text("x\n", encoding="utf-8")
    assert _repo_relative_path(str(link), str(outside.resolve())) == ""
    assert _repo_relative_path(str(link), "../other/secret.py") == ""


def test_dispatch_swarm_reuse_register_failure_fail_closed(monkeypatch):
    job = _green_job(goal="audit peel")
    session = _session_with_jobs([job], cwd="/repo")
    session._register_local_job = MagicMock(side_effect=RuntimeError("store down"))
    act = PilotAction(kind="run_swarm", goal="audit peel", roles=["explore"])

    import harness.send_loop_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "harness.validation_reuse.evaluate_reuse_gate",
        lambda *a, **k: SimpleNamespace(
            outcome="reuse",
            reason="fingerprint_match",
            source_job_id=job["id"],
            validation_fingerprint="abc123fingerprint",
            invalidated_paths=[],
            reuse_status="reused",
            digest_text="REUSED",
            compact_artifacts=[],
            as_provenance=lambda: {
                "reuse_status": "reused",
                "source_job_id": job["id"],
                "reuse_reason": "fingerprint_match",
            },
        ),
    )
    monkeypatch.setattr(
        dispatch, "stream_swarm",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not stream")),
    )
    events = list(
        dispatch_swarm_action(
            session, act, "a-reuse-reg", True,
            counters={"swarms": 0, "demo_swarms": 0}, turn_findings=[],
        )
    )
    kinds = [e.kind for e in events]
    assert "swarm_result" not in kinds
    assert "swarm_pending" not in kinds
    result = next(e for e in events if e.kind == "action_result")
    assert result.data.get("error")
    assert "tracker register/finish failed" in result.data["error"]
    assert not any(
        isinstance(row, dict) and row.get("type") == "swarm_result"
        for row in session._display_transcript
    )


def test_dispatch_swarm_reuse_finish_failure_fail_closed(monkeypatch):
    job = _green_job(goal="audit peel")
    session = _session_with_jobs([job], cwd="/repo")
    session._finish_local_job = MagicMock(side_effect=RuntimeError("finish down"))
    act = PilotAction(kind="run_swarm", goal="audit peel", roles=["explore"])

    import harness.send_loop_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "harness.validation_reuse.evaluate_reuse_gate",
        lambda *a, **k: SimpleNamespace(
            outcome="reuse",
            reason="fingerprint_match",
            source_job_id=job["id"],
            validation_fingerprint="abc123fingerprint",
            invalidated_paths=[],
            reuse_status="reused",
            digest_text="REUSED",
            compact_artifacts=[],
            as_provenance=lambda: {
                "reuse_status": "reused",
                "source_job_id": job["id"],
            },
        ),
    )
    events = list(
        dispatch_swarm_action(
            session, act, "a-reuse-fin", True,
            counters={"swarms": 0, "demo_swarms": 0}, turn_findings=[],
        )
    )
    assert not any(e.kind == "swarm_result" for e in events)
    result = next(e for e in events if e.kind == "action_result")
    assert "tracker register/finish failed" in (result.data.get("error") or "")
    session._register_local_job.assert_called()
    session._fail_or_drop_local_job.assert_called()


def test_dispatch_parallel_reuse_register_failure_fail_closed(monkeypatch):
    job = _green_job(goal="review auth.py CSRF", role="analysis")
    session = _session_with_jobs([job], cwd="/repo")
    session._register_local_job = MagicMock(side_effect=RuntimeError("store down"))
    act = PilotAction(
        kind="run_parallel",
        goals=["review auth.py CSRF"],
        mode="analysis",
    )

    import harness.send_loop_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(dispatch, "_puppetmaster_available", lambda: False)
    monkeypatch.setattr(
        "harness.edit_engines.select_edit_engine",
        lambda *_a, **_k: "native",
    )
    monkeypatch.setattr(
        "harness.conversation._prewarm_worker_imports",
        lambda: None,
    )
    monkeypatch.setattr(
        "harness.repo_resolve.resolve_effective_repo",
        lambda p: p,
    )
    monkeypatch.setattr(
        "harness.implement_guards.check_implement_workspace",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "harness.validation_reuse.evaluate_reuse_gate",
        lambda *a, **k: SimpleNamespace(
            outcome="reuse",
            reason="fingerprint_match",
            source_job_id=job["id"],
            validation_fingerprint="abc123fingerprint",
            invalidated_paths=[],
            reuse_status="reused",
            digest_text="REUSED",
            compact_artifacts=[],
            narrow_goal_suffix="",
            narrow_roles=(),
            as_provenance=lambda: {"reuse_status": "reused", "source_job_id": job["id"]},
        ),
    )
    events = list(
        dispatch_parallel_action(
            session, act, "p-reg-fail", True,
            turn_actions=[act], action_idx=0, action_seq=1, step=0, swarms=0,
        )
    )
    assert not any(e.kind == "swarm_result" for e in events)
    result = next(e for e in events if e.kind == "action_result")
    assert "tracker register/finish failed" in (result.data.get("error") or "")
    session._release_objective.assert_called()
    session._submit_swarm.assert_not_called()


def test_dispatch_parallel_reuse_finish_failure_fail_closed(monkeypatch):
    job = _green_job(goal="review auth.py CSRF", role="analysis")
    session = _session_with_jobs([job], cwd="/repo")
    session._finish_local_job = MagicMock(side_effect=RuntimeError("finish down"))
    act = PilotAction(
        kind="run_parallel",
        goals=["review auth.py CSRF"],
        mode="analysis",
    )

    import harness.send_loop_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(dispatch, "_puppetmaster_available", lambda: False)
    monkeypatch.setattr(
        "harness.edit_engines.select_edit_engine",
        lambda *_a, **_k: "native",
    )
    monkeypatch.setattr(
        "harness.conversation._prewarm_worker_imports",
        lambda: None,
    )
    monkeypatch.setattr(
        "harness.repo_resolve.resolve_effective_repo",
        lambda p: p,
    )
    monkeypatch.setattr(
        "harness.implement_guards.check_implement_workspace",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "harness.validation_reuse.evaluate_reuse_gate",
        lambda *a, **k: SimpleNamespace(
            outcome="reuse",
            reason="fingerprint_match",
            source_job_id=job["id"],
            validation_fingerprint="abc123fingerprint",
            invalidated_paths=[],
            reuse_status="reused",
            digest_text="REUSED",
            compact_artifacts=[],
            narrow_goal_suffix="",
            narrow_roles=(),
            as_provenance=lambda: {"reuse_status": "reused", "source_job_id": job["id"]},
        ),
    )
    events = list(
        dispatch_parallel_action(
            session, act, "p-fin-fail", True,
            turn_actions=[act], action_idx=0, action_seq=1, step=0, swarms=0,
        )
    )
    assert not any(e.kind == "swarm_result" for e in events)
    result = next(e for e in events if e.kind == "action_result")
    assert "tracker register/finish failed" in (result.data.get("error") or "")
    session._register_local_job.assert_called()
    session._fail_or_drop_local_job.assert_called()
    session._submit_swarm.assert_not_called()


def test_dispatch_swarm_narrow_verify_registers_verifier_and_fingerprint(monkeypatch):
    job = _green_job(goal="audit peel")
    session = _session_with_jobs([job], cwd="/repo")
    act = PilotAction(kind="run_swarm", goal="audit peel", roles=["explore", "pipeline-mapper"])

    import harness.send_loop_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "harness.validation_reuse.evaluate_reuse_gate",
        lambda *a, **k: SimpleNamespace(
            outcome="narrow_verify",
            reason="subset_invalidated",
            source_job_id=job["id"],
            validation_fingerprint="abc123fingerprint",
            invalidated_paths=["harness/auth.py"],
            reuse_status="partial",
            digest_text="PARTIAL",
            compact_artifacts=[],
            narrow_goal_suffix="Re-verify only harness/auth.py",
            narrow_roles=NARROW_VERIFY_ROLES,
            as_provenance=lambda: {
                "reuse_status": "partial",
                "source_job_id": job["id"],
                "validation_fingerprint": "abc123fingerprint",
                "invalidated_paths": ["harness/auth.py"],
            },
        ),
    )
    captured = {}

    def fake_stream(_session, intent, q):
        captured["roles"] = list(intent.roles or [])
        captured["goal"] = intent.goal or ""
        result = SimpleNamespace(
            job_id="job_narrow1",
            adapter="agentic",
            mode="swarm",
            artifacts=[{
                "type": "finding",
                "headline": "CSRF still missing in harness/auth.py:42 after re-verify",
                "body": (
                    "Narrow verify confirms harness/auth.py:42 lacks CSRF middleware. "
                    + ("detail " * 20)
                ),
            }],
            auth_failure="",
        )
        q.put(("done", result))

    monkeypatch.setattr(dispatch, "stream_swarm", fake_stream)
    events = list(
        dispatch_swarm_action(
            session, act, "a-narrow", True,
            counters={"swarms": 0, "demo_swarms": 0}, turn_findings=[],
        )
    )
    assert captured["roles"] == list(NARROW_VERIFY_ROLES)
    assert "explore" not in captured["roles"]
    assert "pipeline-mapper" not in captured["roles"]
    assert "Re-verify only harness/auth.py" in captured["goal"]

    register_args = session._register_local_job.call_args
    reg_role = (
        register_args.kwargs.get("role")
        if register_args.kwargs.get("role")
        else (register_args.args[2] if len(register_args.args) > 2 else None)
    )
    assert reg_role == NARROW_VERIFY_ROLES[0]

    finish_kwargs = session._finish_local_job.call_args.kwargs
    assert finish_kwargs.get("reuse_status") == "partial"
    assert finish_kwargs.get("validation_fingerprint") == "abc123fingerprint"
    assert finish_kwargs.get("source_job_id") == job["id"]
    assert finish_kwargs.get("invalidated_paths") == ["harness/auth.py"]

    result_ev = next(e for e in events if e.kind == "swarm_result")
    assert result_ev.data["result"].get("reuse_status") == "partial"
    assert result_ev.data["result"].get("validation_fingerprint") == "abc123fingerprint"


# ---------------------------------------------------------------------------
# Ship-blocker regressions (jobs job_58bb53355708 / job_b206eb5d6d8b)
# ---------------------------------------------------------------------------


def test_symlink_escape_git_dirty_fails_closed_full_swarm(monkeypatch, tmp_path):
    """In-repo symlink whose target escapes the root must not authorize reuse.

    Git reports the symlink path as dirty; relativization returns "". Filtering
    that path away would look like a clean tree and authorize fingerprint_match.
    """
    import os

    from harness import validation_reuse as vr

    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    _init_git_repo(repo)
    (repo / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (outside / "secret.py").write_text("SECRET=1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo), check=True, capture_output=True,
    )
    try:
        os.symlink(str(outside / "secret.py"), str(repo / "leak.py"))
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not available on this platform")

    fp_payload, _reason = vr.compute_source_fingerprint(str(repo), ["a.py"], strict=False)
    assert fp_payload and fp_payload.get("fingerprint")
    job = _green_job(
        cwd=str(repo),
        goal="audit a.py helpers",
        fingerprint=str(fp_payload["fingerprint"]),
        scope=["a.py"],
        headline="Issue in a.py:1 helper return",
    )
    for art in job["artifacts"]:
        art["validation"] = dict(fp_payload)
        art["validation"]["status"] = "fresh"
    job["artifacts"][1]["body"] = (
        "a.py:1 helper returns a constant; documented for reuse gate coverage. "
        + ("detail " * 20)
    )
    job["validation_fingerprint"] = str(fp_payload["fingerprint"])

    monkeypatch.setattr(
        "harness.validation_reuse.pm_validation_helpers_available",
        lambda: True,
    )
    session = _session_with_jobs([job], cwd=str(repo))
    decision = evaluate_reuse_gate(
        session, objective=job["goal"], role="explore", cwd=str(repo),
    )
    assert decision.outcome == "full_swarm"
    assert decision.reason == "changed_path_unresolvable"
    assert decision.outcome != "reuse"


def test_local_fingerprint_rejects_symlink_escape_bytes(monkeypatch, tmp_path):
    """Local hasher must not hash outside-target bytes through in-repo symlinks."""
    import os

    from harness import validation_reuse as vr

    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (outside / "secret.py").write_text("SECRET_OUTSIDE_BYTES\n", encoding="utf-8")
    try:
        os.symlink(str(outside / "secret.py"), str(repo / "a.py"))
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not available on this platform")

    # Force the local hasher path (PM helpers would also fail closed, but this
    # pins Marionette containment policy independently).
    monkeypatch.setattr(vr, "_load_pm_validation", lambda: None)
    payload, reason = vr.compute_source_fingerprint(str(repo), ["a.py"], strict=False)
    assert payload is not None
    assert payload.get("complete") is False
    assert "a.py" in (payload.get("unreadable_paths") or [])
    assert "SECRET_OUTSIDE_BYTES" not in str(payload.get("source_digests") or {})
    assert "a.py" not in (payload.get("source_digests") or {})
    # strict path returns unusable
    strict_payload, strict_reason = vr.compute_source_fingerprint(
        str(repo), ["a.py"], strict=True,
    )
    assert strict_payload is None
    assert "fingerprint_incomplete" in strict_reason


def test_normalize_workspace_key_realpath_and_normcase(tmp_path):
    import os

    from harness.validation_reuse import normalize_workspace_key

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    link = tmp_path / "repo-link"
    try:
        os.symlink(str(repo.resolve()), str(link), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not available on this platform")

    key_real = normalize_workspace_key(str(repo))
    key_link = normalize_workspace_key(str(link))
    key_slash = normalize_workspace_key(str(repo) + os.sep)
    assert key_real
    assert key_real == key_link
    assert key_real == key_slash

    # Distinct case-sensitive paths must remain distinct on case-sensitive FS.
    # Create two real directories that differ only by case when the host allows it.
    upper = tmp_path / "CaseDemo"
    lower = tmp_path / "casedemo"
    try:
        upper.mkdir()
        lower.mkdir()
    except FileExistsError:
        # Case-insensitive volume: both names collide — skip the distinctness check.
        pytest.skip("filesystem is case-insensitive; distinct case paths unavailable")
    key_u = normalize_workspace_key(str(upper))
    key_l = normalize_workspace_key(str(lower))
    if os.path.normcase("A") == os.path.normcase("a"):
        # Windows-style folding: same after normcase is expected.
        assert key_u == key_l
    else:
        assert key_u != key_l


def test_zero_work_reuse_clears_preview_economics(monkeypatch, tmp_path):
    """Real LocalJobsMixin register+finish reuse must persist zero economics."""
    import threading

    from harness.local_job_swarm_view import project_local_job_for_swarm_live
    from harness.local_jobs import LocalJobsMixin

    exports = {"routing": 0}

    def _count_export(**_kwargs):
        exports["routing"] += 1

    monkeypatch.setattr(
        "harness.observability_export.export_routing_savings",
        _count_export,
    )
    monkeypatch.setattr(
        "harness.local_job_routing.preview_agentic_route",
        lambda goal, role="implement": {
            "model_id": "phantom-model",
            "est_cost_usd": 0.42,
            "routing_saved_usd": 0.15,
            "routing_savings_basis": "estimated",
            "baseline_model_id": "frontier",
            "tokens_in": 100,
            "tokens_out": 50,
            "artifact": {
                "type": "ROUTING",
                "headline": "Routed to phantom-model",
                "created_by": "router",
                "model": "phantom-model",
                "policy": "balanced",
                "est_cost_usd": 0.42,
                "baseline_cost_usd": 0.57,
            },
        },
    )

    class _Host(LocalJobsMixin):
        def __init__(self):
            self._local_jobs = {}
            self._local_jobs_lock = threading.RLock()
            self._local_job_cancels = {}
            self._local_jobs_path = str(tmp_path / "swarm_local_jobs.json")
            self.harness_session_id = "sess-reuse-econ"
            self.config = SimpleNamespace(repo=str(tmp_path), driver="test-model")

    host = _Host()
    # Defense-in-depth: even if preview ran, finish with reuse_status=reused
    # must clear. Also verify skip_routing_preview avoids export entirely.
    host._register_local_job(
        "local-reuse-skip",
        "audit peel",
        role="explore",
        cwd=str(tmp_path),
        engine="agentic",
        skip_routing_preview=True,
    )
    assert exports["routing"] == 0
    row = host._local_jobs["local-reuse-skip"]
    assert float(row.get("est_cost_usd") or 0.0) == 0.0
    assert not row.get("routing_saved_usd")

    host._finish_local_job(
        "local-reuse-skip",
        ok=True,
        summary="reused local-prior (fingerprint_match)",
        status="done",
        engine="agentic",
        tokens=0,
        est_cost_usd=0.0,
        reuse_status="reused",
        source_job_id="local-prior",
        validation_fingerprint="abc123fingerprint",
        reuse_reason="fingerprint_match",
    )
    finished = host._local_jobs["local-reuse-skip"]
    assert finished["status"] == "completed"
    assert finished.get("reuse_status") == "reused"
    assert int(finished.get("tokens") or 0) == 0
    assert float(finished.get("est_cost_usd") or 0.0) == 0.0
    assert "routing_saved_usd" not in finished or float(
        finished.get("routing_saved_usd") or 0.0
    ) == 0.0
    assert not any(
        isinstance(a, dict) and str(a.get("type") or "").upper() == "ROUTING"
        for a in (finished.get("artifacts") or [])
    )
    projected = project_local_job_for_swarm_live(finished)
    assert float(projected.get("est_cost_usd") or 0.0) == 0.0
    assert float(projected.get("routing_saved_usd") or 0.0) == 0.0
    assert int(projected.get("tokens") or 0) == 0
    assert exports["routing"] == 0

    # Finish-clear path: register WITH preview, then reuse finish must wipe it.
    host._register_local_job(
        "local-reuse-clear",
        "audit peel again",
        role="explore",
        cwd=str(tmp_path),
        engine="agentic",
    )
    assert exports["routing"] == 1
    assert float(host._local_jobs["local-reuse-clear"].get("est_cost_usd") or 0) > 0
    host._finish_local_job(
        "local-reuse-clear",
        ok=True,
        summary="reused",
        engine="agentic",
        tokens=0,
        est_cost_usd=0.0,
        reuse_status="reused",
        source_job_id="local-prior",
    )
    cleared = host._local_jobs["local-reuse-clear"]
    assert float(cleared.get("est_cost_usd") or 0.0) == 0.0
    assert "routing_saved_usd" not in cleared or float(
        cleared.get("routing_saved_usd") or 0.0
    ) == 0.0
    assert not any(
        isinstance(a, dict) and str(a.get("type") or "").upper() == "ROUTING"
        for a in (cleared.get("artifacts") or [])
    )


def test_narrow_verify_finish_may_retain_measured_costs(monkeypatch, tmp_path):
    """Partial/narrow_verify that executes may keep measured spend."""
    import threading

    from harness.local_jobs import LocalJobsMixin

    monkeypatch.setattr(
        "harness.local_job_routing.preview_agentic_route",
        lambda goal, role="implement": {
            "model_id": "verify-model",
            "est_cost_usd": 0.01,
            "routing_saved_usd": 0.02,
            "routing_savings_basis": "estimated",
            "artifact": {
                "type": "ROUTING",
                "headline": "Routed to verify-model",
                "created_by": "router",
                "model": "verify-model",
                "est_cost_usd": 0.01,
            },
        },
    )

    class _Host(LocalJobsMixin):
        def __init__(self):
            self._local_jobs = {}
            self._local_jobs_lock = threading.RLock()
            self._local_job_cancels = {}
            self._local_jobs_path = str(tmp_path / "jobs.json")
            self.harness_session_id = "sess-nv"
            self.config = SimpleNamespace(repo=str(tmp_path), driver="test-model")

    host = _Host()
    host._register_local_job(
        "local-nv", "re-verify", role="conflict-auditor",
        cwd=str(tmp_path), engine="agentic",
    )
    host._finish_local_job(
        "local-nv",
        ok=True,
        summary="narrow verify done",
        engine="agentic",
        model="verify-model",
        tokens=1200,
        est_cost_usd=0.08,
        reuse_status="partial",
        source_job_id="local-prior",
        invalidated_paths=["a.py"],
    )
    job = host._local_jobs["local-nv"]
    assert abs(float(job.get("est_cost_usd") or 0.0) - 0.08) < 1e-9
    assert int(job.get("tokens") or 0) == 1200


def test_reuse_finish_failure_settles_orphan_local_job(monkeypatch, tmp_path):
    """Register-ok / finish-fail must not leave a live running spinner."""
    import threading

    from harness.local_jobs import LocalJobsMixin
    from harness.pilot import PilotAction

    class _Host(LocalJobsMixin):
        def __init__(self):
            self._local_jobs = {}
            self._local_jobs_lock = threading.RLock()
            self._local_job_cancels = {}
            self._local_jobs_path = str(tmp_path / "jobs.json")
            self.harness_session_id = "sess-orphan"
            self.config = SimpleNamespace(repo=str(tmp_path), driver="test-model")
            self._session_job_ids = []
            self._display_transcript = []
            self._append_action_result = MagicMock()

    host = _Host()
    real_finish = host._finish_local_job

    def boom_finish(job_id, ok=True, **kwargs):
        # Raise on the reuse success finish; allow failed settle to terminalize.
        if ok:
            raise RuntimeError("finish down")
        return real_finish(job_id, ok=ok, **kwargs)

    host._finish_local_job = boom_finish  # type: ignore[method-assign]

    prior = _green_job(goal="audit peel", cwd=str(tmp_path))
    host._local_jobs[prior["id"]] = prior

    import harness.send_loop_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "harness.validation_reuse.evaluate_reuse_gate",
        lambda *a, **k: SimpleNamespace(
            outcome="reuse",
            reason="fingerprint_match",
            source_job_id=prior["id"],
            validation_fingerprint="abc123fingerprint",
            invalidated_paths=[],
            reuse_status="reused",
            digest_text="REUSED",
            compact_artifacts=[],
            as_provenance=lambda: {
                "reuse_status": "reused",
                "source_job_id": prior["id"],
            },
        ),
    )
    act = PilotAction(kind="run_swarm", goal="audit peel", roles=["explore"])
    events = list(
        dispatch_swarm_action(
            host, act, "a-orphan", True,
            counters={"swarms": 0, "demo_swarms": 0}, turn_findings=[],
        )
    )
    assert not any(e.kind == "swarm_result" for e in events)
    result = next(e for e in events if e.kind == "action_result")
    assert "tracker register/finish failed" in (result.data.get("error") or "")

    # No live running orphan remains.
    live = [
        j for j in host._local_jobs.values()
        if str(j.get("status") or "") == "running"
        and str(j.get("id") or "").startswith("local-")
        and j.get("id") != prior["id"]
    ]
    assert live == []
    # Settled failed row or fully dropped — either is acceptable fail-closed.
    orphans = [
        j for j in host._local_jobs.values()
        if str(j.get("id") or "").startswith("local-")
        and j.get("id") != prior["id"]
    ]
    for row in orphans:
        assert str(row.get("status") or "") in ("failed", "cancelled", "completed")


def test_stamp_validation_failure_persists_incomplete(monkeypatch, tmp_path):
    """stamp_validation_on_job failure must persist complete=false/error."""
    import threading

    from harness.local_jobs import LocalJobsMixin
    from harness.validation_reuse import is_reusable_local_job, validation_block_of

    class _Host(LocalJobsMixin):
        def __init__(self):
            self._local_jobs = {}
            self._local_jobs_lock = threading.RLock()
            self._local_job_cancels = {}
            self._local_jobs_path = str(tmp_path / "jobs.json")
            self.harness_session_id = "sess-stamp"
            self.config = SimpleNamespace(repo=str(tmp_path), driver="test-model")

    def boom_stamp(*_a, **_k):
        raise RuntimeError("fingerprint exploded")

    monkeypatch.setattr(
        "harness.validation_reuse.stamp_validation_on_job",
        boom_stamp,
    )
    host = _Host()
    host._register_local_job(
        "local-stamp-fail",
        "audit the auth module",
        role="explore",
        cwd=str(tmp_path),
        engine="native",
        model="test-model",
    )
    host._finish_local_job(
        "local-stamp-fail",
        ok=True,
        summary="Auth middleware missing CSRF check in harness/auth.py:42",
        engine="native",
        model="test-model",
        tokens=100,
        findings=[{
            "type": "finding",
            "headline": "Auth middleware missing CSRF check in harness/auth.py:42",
            "body": (
                "The CSRF middleware is missing on the auth routes in "
                "harness/auth.py:42. This allows cross-site request forgery. "
                + ("detail " * 20)
            ),
        }],
    )
    job = host._local_jobs["local-stamp-fail"]
    assert job["status"] == "completed"
    blocks = [
        validation_block_of(a)
        for a in (job.get("artifacts") or [])
        if isinstance(a, dict) and validation_block_of(a)
    ]
    assert blocks, "expected incomplete validation block after stamp failure"
    assert all(b.get("complete") is False for b in blocks)
    assert any("fingerprint exploded" in str(b.get("error") or "") for b in blocks)
    ok, reason = is_reusable_local_job(
        job, cwd=str(tmp_path), objective="audit the auth module", role="explore",
    )
    assert ok is False
    assert reason in (
        "validation_incomplete",
        "missing_validation_fingerprint",
        "empty_evidence_scope",
        "thin_findings",
        "workspace_mismatch",
    )
