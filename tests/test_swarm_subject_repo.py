"""run_swarm auditing an explicit read-only subject repo.

The swarm may analyze a DIFFERENT git checkout than the open workspace, so this
covers the whole route the subject path travels: wire schema -> PilotAction ->
DriverIntent -> stream_swarm/execute_intent -> reuse fingerprint -> local job
cwd -> worker brief. Two invariants are load-bearing and asserted explicitly:

* fail closed — a missing/non-git subject never silently falls back to the
  workspace (that is how a swarm ends up auditing the wrong tree);
* write confinement — the subject pins reads only. ``config.repo`` and the
  process-global ``HARNESS_REPO`` pointer must survive the dispatch unchanged,
  because every pilot write/edit/command resolves through them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness.pilot import PilotAction, build_tools_schema, from_wire
from harness.send_loop_dispatch import dispatch_swarm_action
from pmharness.intent import DriverIntent, validate_intent

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available",
)


def _git_repo(path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init"], cwd=str(path), check=True, capture_output=True, text=True,
    )
    return str(path)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _tool(schema, name):
    for entry in schema:
        fn = entry.get("function") or {}
        if fn.get("name") == name:
            return fn
    return None


def _session(workspace: str):
    """Session double whose repo validation mirrors the real mixin."""
    from harness.adapter_resolve import AdapterResolveMixin

    session = SimpleNamespace(
        config=SimpleNamespace(repo=workspace, driver="test-model"),
        state_dir="/state/root",
        _local_jobs={},
        _local_jobs_lock=threading.Lock(),
        _session_job_ids=[],
        _register_local_job=MagicMock(),
        _finish_local_job=MagicMock(),
        _fail_or_drop_local_job=MagicMock(),
        _append_action_result=MagicMock(),
        _display_transcript=[],
    )
    session._validate_target_repo = AdapterResolveMixin._validate_target_repo.__get__(
        session, SimpleNamespace,
    )
    return session


def _run(monkeypatch, session, act, *, result=None, capture=None):
    """Drive dispatch_swarm_action with the bridge stubbed out."""
    import harness.send_loop_dispatch as dispatch

    bridge_result = result or SimpleNamespace(
        job_id="job-subject",
        adapter="agentic",
        mode="swarm",
        num_artifacts=1,
        artifact_types=["finding"],
        artifacts=[{
            "type": "finding",
            "headline": "harness/router.py:10 drops the rejected alternative",
            "body": "The router discards alternatives before the receipt is written.",
            "execution_ref": {"job_id": "job-subject"},
        }],
        auth_failure="",
        summary="one finding",
    )

    def fake_stream(session_arg, intent, queue):
        if capture is not None:
            capture.append(intent)
        queue.put(("done", bridge_result))

    monkeypatch.delenv("HARNESS_ALLOW_DEMO_SWARM", raising=False)
    monkeypatch.setattr(dispatch, "stream_swarm", fake_stream)
    events = list(dispatch_swarm_action(
        session, act, "a-1", True,
        counters={"swarms": 0, "demo_swarms": 0},
        turn_findings=[],
    ))
    return events


class TestWireAndSchema:
    def test_run_swarm_exposes_optional_repo(self):
        fn = _tool(build_tools_schema(), "run_swarm")
        assert fn is not None
        params = fn.get("parameters") or {}
        props = params.get("properties") or {}
        assert "repo" in props
        assert props["repo"].get("type") == "string"
        description = (props["repo"].get("description") or "").lower()
        assert "different" in description
        assert "git" in description
        # Optional, and the goal stays the only required field.
        assert "repo" not in (params.get("required") or [])
        assert params.get("required") == ["goal"]

    def test_schema_stays_json_serializable(self):
        json.dumps(build_tools_schema())

    def test_from_wire_maps_repo_and_target_dir_alias(self):
        act = from_wire("run_swarm", {"goal": "audit", "repo": "/tmp/subject"})
        assert act.repo == "/tmp/subject"
        alias = from_wire("run_swarm", {"goal": "audit", "target_dir": "/tmp/alias"})
        assert alias.repo == "/tmp/alias"
        nested = from_wire(
            "run_swarm",
            {"goal": "audit", "arguments": {"repo": "/tmp/nested"}},
        )
        assert nested.repo == "/tmp/nested"

    def test_from_wire_without_repo_stays_empty(self):
        assert from_wire("run_swarm", {"goal": "audit"}).repo == ""

    def test_driver_intent_carries_subject_repo(self):
        assert DriverIntent(action="run_swarm", goal="x").repo is None
        intent = validate_intent({
            "action": "run_swarm", "goal": "audit", "repo": "/tmp/subject",
        })
        assert intent.repo == "/tmp/subject"
        assert intent.to_dict()["repo"] == "/tmp/subject"
        assert validate_intent({"action": "run_swarm", "goal": "audit"}).repo is None


@requires_git
class TestSubjectRouting:
    def test_valid_subject_routes_cwd_everywhere(self, monkeypatch, tmp_path):
        workspace = _git_repo(tmp_path / "workspace")
        subject = _git_repo(tmp_path / "subject")
        session = _session(workspace)
        reuse_calls: list = []

        import harness.validation_reuse as reuse

        def fake_gate(_session, **kwargs):
            reuse_calls.append(kwargs)
            return None

        monkeypatch.setattr(reuse, "evaluate_reuse_gate", fake_gate)

        captured: list = []
        _run(
            monkeypatch, session,
            PilotAction(kind="run_swarm", goal="audit the subject", repo=subject),
            capture=captured,
        )

        # Intent (and therefore stream_swarm -> execute_intent -> worker brief).
        assert captured and _norm(captured[0].repo or "") == _norm(subject)
        # Validation-reuse fingerprint identity.
        assert reuse_calls and _norm(reuse_calls[0]["cwd"]) == _norm(subject)
        # Local job cwd, for both register calls.
        cwds = {
            _norm(call.kwargs["cwd"])
            for call in session._register_local_job.call_args_list
        }
        assert cwds == {_norm(subject)}

    def test_pilot_writes_stay_confined_to_the_session_workspace(
        self, monkeypatch, tmp_path,
    ):
        workspace = _git_repo(tmp_path / "workspace")
        subject = _git_repo(tmp_path / "subject")
        monkeypatch.setenv("HARNESS_REPO", workspace)
        session = _session(workspace)

        _run(
            monkeypatch, session,
            PilotAction(kind="run_swarm", goal="audit the subject", repo=subject),
        )

        assert _norm(session.config.repo) == _norm(workspace)
        assert _norm(os.environ["HARNESS_REPO"]) == _norm(workspace)

    def test_invalid_subject_fails_closed(self, monkeypatch, tmp_path):
        workspace = _git_repo(tmp_path / "workspace")
        plain_folder = tmp_path / "not-a-repo"
        plain_folder.mkdir()
        session = _session(workspace)
        captured: list = []

        events = _run(
            monkeypatch, session,
            PilotAction(kind="run_swarm", goal="audit", repo=str(plain_folder)),
            capture=captured,
        )

        assert not captured, "a non-git subject must not reach the bridge"
        errors = [
            event.data["error"] for event in events
            if event.kind == "action_result" and event.data.get("error")
        ]
        assert errors and "not a valid git repository" in errors[0]
        assert session._register_local_job.call_args_list == []

    def test_missing_subject_directory_fails_closed(self, monkeypatch, tmp_path):
        workspace = _git_repo(tmp_path / "workspace")
        session = _session(workspace)
        captured: list = []

        events = _run(
            monkeypatch, session,
            PilotAction(
                kind="run_swarm", goal="audit",
                repo=str(tmp_path / "does-not-exist"),
            ),
            capture=captured,
        )

        assert not captured
        assert any(
            event.kind == "action_result" and event.data.get("error")
            for event in events
        )

    def test_without_repo_the_workspace_is_still_the_subject(
        self, monkeypatch, tmp_path,
    ):
        workspace = _git_repo(tmp_path / "workspace")
        session = _session(workspace)
        captured: list = []

        _run(
            monkeypatch, session,
            PilotAction(kind="run_swarm", goal="audit the workspace"),
            capture=captured,
        )

        assert captured and _norm(captured[0].repo or "") == _norm(workspace)
        cwds = {
            _norm(call.kwargs["cwd"])
            for call in session._register_local_job.call_args_list
        }
        assert cwds == {_norm(workspace)}


class TestStreamSwarmSubject:
    def test_stream_swarm_prefers_the_intent_subject(self, monkeypatch, tmp_path):
        import harness.send_loop_phases as phases

        workspace = str(tmp_path / "workspace")
        subject = str(tmp_path / "subject")
        os.makedirs(workspace)
        os.makedirs(subject)
        calls: list = []

        def fake_execute_intent(intent, **kwargs):
            calls.append(kwargs)
            return "result"

        monkeypatch.setattr(phases, "execute_intent", fake_execute_intent)
        session = SimpleNamespace(
            config=SimpleNamespace(repo=workspace),
            state_dir="/state",
            harness_session_id="sess-1",
        )
        import queue

        delta_q: queue.Queue = queue.Queue()
        phases.stream_swarm(
            session,
            DriverIntent(action="run_swarm", goal="audit", repo=subject),
            delta_q,
        )

        assert delta_q.get() == ("done", "result")
        assert calls and _norm(calls[0]["cwd"]) == _norm(subject)
        assert _norm(calls[0]["repo"]) == _norm(subject)

    def test_stream_swarm_falls_back_to_the_workspace(self, monkeypatch, tmp_path):
        import harness.send_loop_phases as phases

        workspace = str(tmp_path / "workspace")
        os.makedirs(workspace)
        calls: list = []
        monkeypatch.setattr(
            phases, "execute_intent",
            lambda intent, **kwargs: calls.append(kwargs) or "result",
        )
        session = SimpleNamespace(
            config=SimpleNamespace(repo=workspace),
            state_dir="/state",
            harness_session_id="",
        )
        import queue

        delta_q: queue.Queue = queue.Queue()
        phases.stream_swarm(
            session, DriverIntent(action="run_swarm", goal="audit"), delta_q,
        )

        assert calls and _norm(calls[0]["cwd"]) == _norm(workspace)


@requires_git
class TestExplicitSubjectLeavesTheEnvAlone:
    """``execute_intent`` pins workers by argument, never by process env.

    ``HARNESS_REPO`` is process-global. Republishing it per dispatch — even
    scoped and restored — means two concurrent swarms on different subjects can
    read each other's pointer, and an ``open_project`` force-publish landing
    mid-dispatch gets reverted by whichever swarm finishes last. Every live
    ``run_swarm`` / prewalk path already passes ``repo_cwd`` explicitly into
    ``WorkerSpec.payload["cwd"]`` / ``build_prewalk_specs``, so the env write
    bought nothing and cost correctness.
    """

    def _capture_specs(self, monkeypatch):
        """Run execute_intent against a stubbed Orchestrator, returning specs."""
        import pmharness.bridge as bridge

        seen: list = []

        class _FakeOrchestrator:
            def __init__(self, _store):
                pass

            def run(self, _goal, specs=None, worker_mode=None, label=None):
                seen.append(list(specs or []))
                return SimpleNamespace(
                    artifacts=[],
                    summary="none",
                    mode="swarm",
                    job=SimpleNamespace(id="job-x", status="completed"),
                )

        monkeypatch.setattr(
            "puppetmaster.orchestrator.Orchestrator", _FakeOrchestrator,
        )
        monkeypatch.setattr(
            "puppetmaster.store_factory.create_store", lambda *_a, **_k: object(),
        )
        monkeypatch.setattr(bridge, "_warn_if_unindexed", lambda *_a, **_k: None)
        monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "agentic")
        return seen

    def test_explicit_subject_never_writes_harness_repo(self, monkeypatch, tmp_path):
        from pmharness.bridge import execute_intent

        workspace = _git_repo(tmp_path / "workspace")
        subject = _git_repo(tmp_path / "subject")
        monkeypatch.setenv("HARNESS_REPO", workspace)
        seen = self._capture_specs(monkeypatch)

        execute_intent(
            DriverIntent(action="run_swarm", goal="audit", roles=["explore"]),
            state_dir=str(tmp_path / "state"),
            cwd=subject,
        )

        # The worker is pinned by argument...
        assert seen and _norm(seen[0][0].payload["cwd"]) == _norm(subject)
        # ...and the process-global pointer is untouched, during and after.
        assert _norm(os.environ["HARNESS_REPO"]) == _norm(workspace)

    def test_concurrent_explicit_subjects_leave_harness_repo_unchanged(
        self, monkeypatch, tmp_path,
    ):
        """Two overlapping audits must not see (or restore) each other's repo."""
        from pmharness.bridge import execute_intent

        workspace = _git_repo(tmp_path / "workspace")
        first = _git_repo(tmp_path / "subject-a")
        second = _git_repo(tmp_path / "subject-b")
        monkeypatch.setenv("HARNESS_REPO", workspace)
        seen = self._capture_specs(monkeypatch)

        observed_env: list = []
        barrier = threading.Barrier(2, timeout=10)

        def audit(subject: str) -> None:
            barrier.wait()
            execute_intent(
                DriverIntent(action="run_swarm", goal="audit", roles=["explore"]),
                state_dir=str(tmp_path / "state"),
                cwd=subject,
            )
            observed_env.append(os.environ.get("HARNESS_REPO"))

        threads = [
            threading.Thread(target=audit, args=(subject,))
            for subject in (first, second)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert {_norm(value or "") for value in observed_env} == {_norm(workspace)}
        assert _norm(os.environ["HARNESS_REPO"]) == _norm(workspace)
        # Each dispatch kept its own subject rather than adopting the rival's.
        assert {_norm(specs[0].payload["cwd"]) for specs in seen} == {
            _norm(first), _norm(second),
        }

    def test_without_an_explicit_repo_the_env_is_the_fallback(
        self, monkeypatch, tmp_path,
    ):
        from pmharness.bridge import execute_intent

        workspace = _git_repo(tmp_path / "workspace")
        monkeypatch.setenv("HARNESS_REPO", workspace)
        seen = self._capture_specs(monkeypatch)

        execute_intent(
            DriverIntent(action="run_swarm", goal="audit", roles=["explore"]),
            state_dir=str(tmp_path / "state"),
        )

        assert seen and _norm(seen[0][0].payload["cwd"]) == _norm(workspace)
        assert _norm(os.environ["HARNESS_REPO"]) == _norm(workspace)

    def test_worker_spec_payload_carries_acceptance_criteria(self, monkeypatch, tmp_path):
        from pmharness.bridge import execute_intent

        workspace = _git_repo(tmp_path / "workspace")
        seen = self._capture_specs(monkeypatch)
        criteria = ["pyright is clean", "tsc passes"]

        execute_intent(
            DriverIntent(
                action="run_swarm",
                goal="audit",
                roles=["explore"],
                acceptance_criteria=criteria,
            ),
            state_dir=str(tmp_path / "state"),
            cwd=workspace,
        )

        assert seen
        payload = seen[0][0].payload
        assert payload["acceptance_criteria"] == criteria
        assert "Acceptance criteria:" in seen[0][0].instruction
        assert "pyright is clean" in seen[0][0].instruction

    def test_worker_spec_payload_carries_acceptance_criteria(self, monkeypatch, tmp_path):
        from pmharness.bridge import execute_intent

        workspace = _git_repo(tmp_path / "workspace")
        seen = self._capture_specs(monkeypatch)
        criteria = ["pyright is clean", "tsc passes"]

        execute_intent(
            DriverIntent(
                action="run_swarm",
                goal="audit",
                roles=["explore"],
                acceptance_criteria=criteria,
            ),
            state_dir=str(tmp_path / "state"),
            cwd=workspace,
        )

        assert seen
        payload = seen[0][0].payload
        assert payload["acceptance_criteria"] == criteria
        assert "Acceptance criteria:" in seen[0][0].instruction
        assert "pyright is clean" in seen[0][0].instruction
