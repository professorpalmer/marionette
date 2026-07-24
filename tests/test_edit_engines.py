"""Hermetic unit tests for harness.edit_engines — engine selection, payload
construction, error paths, and pure helpers. No real Puppetmaster workers or
network calls."""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from unittest.mock import MagicMock

import pytest

from harness.config import HarnessConfig
from harness.edit_engines import (
    AGENTIC_ERROR,
    AGENTIC_ROUTE_FAILED,
    AGENTIC_UNAVAILABLE,
    agentic_available,
    agentic_events_from_store,
    finalize_worktree_patch,
    managed_worktree,
    run_agentic_edit,
    run_edit_worker,
    run_native_edit,
    select_edit_engine,
    _summarize_agentic_result,
)
from harness.worker import ProviderWorker, WorkerResult
from pmharness.bridge import _router_supports_max_capability

ROUTER_HAS_CEILING = _router_supports_max_capability()
EXPECTED_CAP_KEY = "max_capability" if ROUTER_HAS_CEILING else "min_capability"


def create_temp_git_repo():
    repo_dir = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, capture_output=True)
    with open(os.path.join(repo_dir, "test.txt"), "w", encoding="utf-8") as f:
        f.write("hello\n")
    subprocess.run(["git", "add", "test.txt"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, capture_output=True)
    return repo_dir


def _cfg(repo_dir: str) -> HarnessConfig:
    cfg = HarnessConfig()
    cfg.repo = repo_dir
    return cfg


def _fake_artifact(**payload):
    art = MagicMock()
    art.payload = payload
    return art


def _fake_pm_result(artifacts=None):
    result = MagicMock()
    result.artifacts = artifacts or []
    return result


@contextlib.contextmanager
def _fake_managed_worktree(repo: str, base: str = "HEAD"):
    wt = tempfile.mkdtemp()
    try:
        yield wt
    finally:
        shutil.rmtree(wt, ignore_errors=True)


def _install_agentic_mocks(
    monkeypatch, *, orchestrator_result=None, capture_payload=None,
    capture_specs=None,
):
    """Patch Puppetmaster + worktree so run_agentic_edit stays hermetic."""
    monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)
    monkeypatch.setattr("harness.edit_engines.managed_worktree", _fake_managed_worktree)
    monkeypatch.setattr("harness.edit_engines.managed_worktree_for_goal", _fake_managed_worktree)

    storage: list[dict] = capture_payload if capture_payload is not None else []
    specs_out: list[dict] = capture_specs if capture_specs is not None else []

    class _CapturingWorkerSpec:
        def __init__(self, role, instruction, adapter, payload):
            storage.append(payload)
            specs_out.append({
                "role": role,
                "instruction": instruction,
                "adapter": adapter,
                "payload": payload,
            })
            self.role = role
            self.instruction = instruction
            self.adapter = adapter
            self.payload = payload

    class _FakeOrchestrator:
        def __init__(self, store):
            self.store = store

        def run(self, goal, specs=None, worker_mode="inline", **kwargs):
            return orchestrator_result or _fake_pm_result()

    monkeypatch.setattr("puppetmaster.workers.WorkerSpec", _CapturingWorkerSpec)
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr("puppetmaster.store_factory.create_store", lambda *a, **k: MagicMock())

    return storage


# --- pure helpers: _summarize_agentic_result ---


def test_summarize_agentic_result_sums_tokens_and_picks_failure():
    result = _fake_pm_result([
        _fake_artifact(tokens_out=100, tokens_in=40),
        _fake_artifact(tokens_out=50, tokens_in=10, failure="route_failed", stdout="routing failed"),
    ])
    out, inn, failure, text = _summarize_agentic_result(result)
    assert out == 150
    assert inn == 50
    assert failure == "route_failed"
    assert text == "routing failed"


def test_summarize_agentic_result_empty_and_malformed():
    out, inn, failure, text = _summarize_agentic_result(_fake_pm_result())
    assert (out, inn, failure, text) == (0, 0, "", "")

    bare = MagicMock()
    bare.artifacts = None
    out2, inn2, failure2, text2 = _summarize_agentic_result(bare)
    assert (out2, inn2, failure2, text2) == (0, 0, "", "")

    bad = _fake_pm_result([_fake_artifact(tokens_out="not-a-number")])
    with pytest.raises(ValueError):
        _summarize_agentic_result(bad)


def test_summarize_agentic_result_truncates_stdout():
    long_out = "x" * 3000
    result = _fake_pm_result([_fake_artifact(stdout=long_out)])
    _, _, _, text = _summarize_agentic_result(result)
    assert len(text) == 2000


# --- pure helpers: select_edit_engine / agentic_available ---


def test_select_edit_engine_explicit_adapter(monkeypatch):
    cfg = HarnessConfig()
    monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)
    assert select_edit_engine(cfg, "native") == "native"
    assert select_edit_engine(cfg, "provider") == "native"
    assert select_edit_engine(cfg, "agentic") == "agentic"

    monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: False)
    assert select_edit_engine(cfg, "agentic") == "native"


def test_select_edit_engine_env_override(monkeypatch):
    cfg = HarnessConfig()
    monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)
    monkeypatch.setenv("HARNESS_EDIT_ENGINE", "native")
    assert select_edit_engine(cfg) == "native"
    monkeypatch.setenv("HARNESS_EDIT_ENGINE", "agentic")
    assert select_edit_engine(cfg) == "agentic"

    monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: False)
    monkeypatch.setenv("HARNESS_EDIT_ENGINE", "agentic")
    assert select_edit_engine(cfg) == "native"


def test_select_edit_engine_defaults_to_agentic_when_key_present(monkeypatch):
    cfg = HarnessConfig()
    monkeypatch.delenv("HARNESS_EDIT_ENGINE", raising=False)
    monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)
    assert select_edit_engine(cfg) == "agentic"

    monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: False)
    assert select_edit_engine(cfg) == "native"


def test_agentic_available_from_providers(monkeypatch):
    monkeypatch.setattr(
        "puppetmaster.providers.available_providers",
        lambda: ["openai"],
    )
    assert agentic_available() is True

    monkeypatch.setattr(
        "puppetmaster.providers.available_providers",
        lambda: [],
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert agentic_available() is False


def test_agentic_available_falls_back_to_env_on_import_error(monkeypatch):
    def _boom():
        raise RuntimeError("no puppetmaster")

    monkeypatch.setattr("puppetmaster.providers.available_providers", _boom)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert agentic_available() is True


# --- worktree helpers (real git, no network) ---


def test_managed_worktree_creates_and_cleans_up():
    repo_dir = create_temp_git_repo()
    try:
        with managed_worktree(repo_dir) as wt_path:
            assert os.path.isdir(wt_path)
            assert wt_path != repo_dir
            assert os.path.isfile(os.path.join(wt_path, "test.txt"))
        assert not os.path.exists(wt_path)
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_finalize_worktree_patch_stages_changes_and_strips_artifacts():
    repo_dir = create_temp_git_repo()
    try:
        with managed_worktree(repo_dir) as wt_path:
            src = os.path.join(wt_path, "test.txt")
            with open(src, "w", encoding="utf-8") as f:
                f.write("hello\nworld\n")
            cache_dir = os.path.join(wt_path, "__pycache__")
            os.makedirs(cache_dir)
            with open(os.path.join(cache_dir, "junk.pyc"), "w", encoding="utf-8") as f:
                f.write("artifact")

            patch, files = finalize_worktree_patch(wt_path)
            assert "test.txt" in files
            assert "__pycache__" not in patch
            assert "+world" in patch
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_finalize_worktree_patch_empty_when_no_changes():
    repo_dir = create_temp_git_repo()
    try:
        with managed_worktree(repo_dir) as wt_path:
            patch, files = finalize_worktree_patch(wt_path)
            assert patch.strip() == ""
            assert files == []
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


# --- run_native_edit (ProviderWorker mocked) ---


def test_run_native_edit_delegates_to_provider_worker(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        sentinel = WorkerResult(ok=True, patch="p", summary="done")

        def fake_run(self):
            assert self.repo == os.path.abspath(repo_dir)
            assert self.goal == "edit foo"
            assert self.job_id == "job-1"
            return sentinel

        monkeypatch.setattr(ProviderWorker, "run", fake_run)
        result = run_native_edit(cfg, "edit foo", job_id="job-1")
        assert result is sentinel
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


# --- run_agentic_edit payload construction ---


def test_agentic_payload_capability_key_and_default_cap(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        captured: list[dict] = []
        _install_agentic_mocks(monkeypatch, capture_payload=captured)
        monkeypatch.delenv("HARNESS_IMPLEMENT_DEEP", raising=False)
        monkeypatch.delenv("HARNESS_IMPLEMENT_MAX_CAPABILITY", raising=False)
        monkeypatch.delenv("HARNESS_IMPLEMENT_PROVIDER", raising=False)
        monkeypatch.delenv("HARNESS_IMPLEMENT_MODEL", raising=False)

        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("diff content", ["test.txt"]),
        )

        result = run_agentic_edit(cfg, "make a change")
        assert result.ok is True
        assert result.engine == "agentic"
        assert len(captured) == 1
        payload = captured[0]
        assert payload["mode"] == "implement"
        assert payload["routing_policy"] == "balanced"
        assert payload["auto_route"] is True
        assert payload["token_budget"] == 250000
        assert EXPECTED_CAP_KEY in payload
        assert payload[EXPECTED_CAP_KEY] == 86
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_payload_token_budget_from_env(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        captured: list[dict] = []
        _install_agentic_mocks(monkeypatch, capture_payload=captured)
        monkeypatch.setenv("HARNESS_WORKER_TOKEN_BUDGET", "7777")
        monkeypatch.delenv("HARNESS_IMPLEMENT_PROVIDER", raising=False)
        monkeypatch.delenv("HARNESS_IMPLEMENT_MODEL", raising=False)

        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("diff content", ["test.txt"]),
        )

        result = run_agentic_edit(cfg, "make a change")
        assert result.ok is True
        assert captured[0]["token_budget"] == 7777
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_stamps_routed_model_from_routing_artifact(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        routing = _fake_artifact(model_id="z-ai/glm-5.2", estimated_cost_usd=0.004)
        routing.type = "routing"
        _install_agentic_mocks(
            monkeypatch,
            orchestrator_result=_fake_pm_result([
                routing,
                _fake_artifact(tokens_out=20, tokens_in=8, stdout="patched"),
            ]),
        )
        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("diff content", ["test.txt"]),
        )
        result = run_agentic_edit(cfg, "make a change")
        assert result.ok is True
        assert result.engine == "agentic"
        assert result.model == "z-ai/glm-5.2"
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_native_edit_stamps_engine_and_driver(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        cfg.driver = "stub-oracle-v2"
        sentinel = WorkerResult(ok=True, patch="p", summary="done")

        monkeypatch.setattr(ProviderWorker, "run", lambda self: sentinel)
        result = run_native_edit(cfg, "edit foo")
        assert result is sentinel
        assert result.engine == "native"
        assert result.model == "stub-oracle-v2"
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)



def test_agentic_payload_max_capability_env_override(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        captured: list[dict] = []
        _install_agentic_mocks(monkeypatch, capture_payload=captured)
        monkeypatch.setenv("HARNESS_IMPLEMENT_MAX_CAPABILITY", "70")
        monkeypatch.delenv("HARNESS_IMPLEMENT_DEEP", raising=False)

        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("patch", ["a.txt"]),
        )

        run_agentic_edit(cfg, "goal")
        assert captured[0][EXPECTED_CAP_KEY] == 70
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_payload_deep_mode_omits_capability_cap(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        captured: list[dict] = []
        _install_agentic_mocks(monkeypatch, capture_payload=captured)
        monkeypatch.setenv("HARNESS_IMPLEMENT_DEEP", "1")

        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("patch", ["a.txt"]),
        )

        run_agentic_edit(cfg, "goal")
        payload = captured[0]
        assert "max_capability" not in payload
        assert "min_capability" not in payload
        assert payload["routing_policy"] == "balanced"
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_payload_uses_min_capability_when_router_lacks_ceiling(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        captured: list[dict] = []
        _install_agentic_mocks(monkeypatch, capture_payload=captured)
        monkeypatch.setattr("pmharness.bridge._router_supports_max_capability", lambda: False)
        monkeypatch.delenv("HARNESS_IMPLEMENT_DEEP", raising=False)

        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("patch", ["a.txt"]),
        )

        run_agentic_edit(cfg, "goal")
        assert "min_capability" in captured[0]
        assert "max_capability" not in captured[0]
        assert captured[0]["min_capability"] == 86
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_payload_explicit_provider_and_model(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        captured: list[dict] = []
        _install_agentic_mocks(monkeypatch, capture_payload=captured)
        monkeypatch.setenv("HARNESS_IMPLEMENT_PROVIDER", "openai")
        monkeypatch.setenv("HARNESS_IMPLEMENT_MODEL", "gpt-4o")

        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("patch", ["a.txt"]),
        )

        run_agentic_edit(cfg, "goal")
        payload = captured[0]
        assert payload["provider"] == "openai"
        assert payload["model"] == "gpt-4o"
        assert payload["auto_route"] is False
        assert "max_capability" not in payload
        assert "min_capability" not in payload
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_analysis_uses_analyze_payload_not_implement(monkeypatch):
    """expects_diff=False must not stamp mode=implement (avoids 900s edit loop)."""
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        captured: list[dict] = []
        specs: list[dict] = []
        finding = _fake_artifact(
            stdout=(
                "FINDING: harness/edit_engines.py:330 analysis must use "
                "read-only analyze mode."
            ),
            tokens_out=10,
            tokens_in=5,
        )
        finding.type = "finding"
        _install_agentic_mocks(
            monkeypatch,
            orchestrator_result=_fake_pm_result([finding]),
            capture_payload=captured,
            capture_specs=specs,
        )
        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("", []),
        )
        result = run_agentic_edit(cfg, "audit seed baseline", expects_diff=False)
        assert result.ok is True
        assert len(captured) == 1
        payload = captured[0]
        assert payload.get("mode") != "implement"
        assert "mode" not in payload or payload.get("mode") != "implement"
        assert payload.get("read_only") is True
        assert payload.get("no_edit") is True
        assert payload.get("max_turns", 0) >= 16
        assert specs[0]["role"] == "explore"
        assert "submit_findings" in (specs[0]["instruction"] or "")
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_analysis_empty_result_fails_structured_gate(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        _install_agentic_mocks(
            monkeypatch,
            orchestrator_result=_fake_pm_result([
                _fake_artifact(
                    stdout="Now let me look at the modules more carefully...",
                    tokens_out=3,
                    tokens_in=2,
                    failure="empty_or_unstructured_agentic_result",
                ),
            ]),
        )
        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("", []),
        )
        result = run_agentic_edit(cfg, "audit auth", expects_diff=False)
        assert result.ok is False
        assert "no structured findings" in (result.error or result.summary or "")
        assert "Now let me look" in (result.summary or "")
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


# --- run_agentic_edit error paths ---


def test_agentic_edit_unavailable_without_key(monkeypatch):
    cfg = HarnessConfig()
    monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: False)
    result = run_agentic_edit(cfg, "goal")
    assert result.ok is False
    assert result.error == AGENTIC_UNAVAILABLE


def test_agentic_edit_unavailable_on_import_failure(monkeypatch):
    cfg = HarnessConfig()
    monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)

    import builtins
    real_import = builtins.__import__

    def _guard_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "puppetmaster.orchestrator":
            raise ImportError("missing puppetmaster")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _guard_import)
    result = run_agentic_edit(cfg, "goal")
    assert result.ok is False
    assert result.error == AGENTIC_UNAVAILABLE


def test_agentic_edit_empty_diff_no_fallback_error(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        _install_agentic_mocks(monkeypatch, orchestrator_result=_fake_pm_result([
            _fake_artifact(tokens_out=10, tokens_in=5, stdout="done but no edits"),
        ]))
        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("", []),
        )

        result = run_agentic_edit(cfg, "goal")
        assert result.ok is False
        assert result.error == ""
        assert result.summary == "done but no edits"
        assert result.tokens_out == 10
        assert result.tokens_in == 5
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_empty_diff_route_failure(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        _install_agentic_mocks(monkeypatch, orchestrator_result=_fake_pm_result([
            _fake_artifact(failure="no_model", stdout="could not route"),
        ]))
        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("", []),
        )

        result = run_agentic_edit(cfg, "goal")
        assert result.ok is False
        assert result.error == AGENTIC_ROUTE_FAILED
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_runtime_exception_returns_agentic_error(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)

        @contextlib.contextmanager
        def _boom_worktree(repo, base="HEAD"):
            raise RuntimeError("worktree blew up")
            yield ""  # pragma: no cover

        monkeypatch.setattr("harness.edit_engines.managed_worktree", _boom_worktree)
        result = run_agentic_edit(cfg, "goal")
        assert result.ok is False
        assert result.error == AGENTIC_ERROR
        assert "worktree blew up" in result.summary
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_events_from_store_maps_tool_shaped_only():
    class _Store:
        def read_events(self, job_id):
            assert job_id == "job-1"
            return [
                {"event": "task.saved", "payload": {"task_id": "t1", "role": "implement"}},
                {"event": "artifact.saved", "payload": {
                    "stdout": "SECRET_LOG", "files": ["a.py"],
                }},
                {"event": "tool.started", "payload": {
                    "id": "tc1", "tool_name": "read_file", "path": "a.py",
                    "command": "should-not-leak",
                }},
                {"event": "tool.finished", "payload": {
                    "id": "tc1", "tool_name": "read_file", "path": "a.py",
                    "duration_ms": 12, "stdout": "FILE_BODY",
                }},
            ]

    events = agentic_events_from_store(_Store(), "job-1")
    assert [e.kind for e in events] == ["action_start", "action_result"]
    assert events[0].data["id"] == "tc1"
    assert events[0].data["kind"] == "read_file"
    assert events[0].data["goal"] == "a.py"
    assert "command" not in events[0].data
    assert events[1].data["status"] == "complete"
    assert "stdout" not in events[1].data
    assert "SECRET_LOG" not in str(events)
    assert "FILE_BODY" not in str(events)


def test_agentic_events_from_store_empty_without_tool_shape():
    class _Store:
        def read_events(self, _job_id):
            return [
                {"event": "worker.completed_task", "payload": {"task_id": "t1"}},
                {"event": "job.status", "payload": {"status": "complete"}},
            ]

    assert agentic_events_from_store(_Store(), "job-1") == []
    assert agentic_events_from_store(None, "job-1") == []
    assert agentic_events_from_store(_Store(), "") == []


def test_agentic_edit_maps_store_tool_events_onto_worker_result(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)

        class _ToolStore:
            def read_events(self, job_id):
                return [{
                    "event": "tool.started",
                    "payload": {"id": "e1", "kind": "edit_file", "goal": "x.py"},
                }, {
                    "event": "tool.finished",
                    "payload": {"id": "e1", "kind": "edit_file", "goal": "x.py"},
                }]

        job = MagicMock()
        job.id = "pm-job"
        pm_result = _fake_pm_result([
            _fake_artifact(tokens_out=10, tokens_in=5, stdout="ok"),
        ])
        pm_result.job = job

        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)
        monkeypatch.setattr("harness.edit_engines.managed_worktree", _fake_managed_worktree)
        monkeypatch.setattr("harness.edit_engines.managed_worktree_for_goal", _fake_managed_worktree)
        monkeypatch.setattr("puppetmaster.workers.WorkerSpec", MagicMock)
        monkeypatch.setattr(
            "puppetmaster.orchestrator.Orchestrator",
            lambda store: MagicMock(run=MagicMock(return_value=pm_result)),
        )
        monkeypatch.setattr(
            "puppetmaster.store_factory.create_store",
            lambda *a, **k: _ToolStore(),
        )
        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("diff --git a/x b/x\n+line", ["x.py"]),
        )

        result = run_agentic_edit(cfg, "goal")
        assert result.ok is True
        assert [e.kind for e in result.events] == ["action_start", "action_result"]
        assert result.events[0].data["id"] == "e1"
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_success_with_patch(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        _install_agentic_mocks(monkeypatch, orchestrator_result=_fake_pm_result([
            _fake_artifact(tokens_out=200, tokens_in=80, stdout="edited files"),
        ]))
        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("diff --git a/x b/x\n+line", ["x.py"]),
        )

        result = run_agentic_edit(cfg, "goal")
        assert result.ok is True
        assert result.patch.startswith("diff")
        assert result.files_changed == ["x.py"]
        assert result.tokens_out == 200
        assert result.tokens_in == 80
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


# --- run_edit_worker dispatch and fallback ---


def test_run_edit_worker_dispatches_native(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.select_edit_engine", lambda *a, **k: "native")
        native_called = []

        def fake_native(config, goal, job_id="", **kwargs):
            native_called.append((goal, job_id))
            return WorkerResult(ok=True, summary="native ran")

        monkeypatch.setattr("harness.edit_engines.run_native_edit", fake_native)
        result = run_edit_worker(cfg, "do it", job_id="j1")
        assert result.summary == "native ran"
        assert native_called == [("do it", "j1")]
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_run_edit_worker_agentic_success_no_fallback(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.select_edit_engine", lambda *a, **k: "agentic")
        agentic_result = WorkerResult(ok=True, patch="p", summary="agentic ok")
        monkeypatch.setattr("harness.edit_engines.run_agentic_edit", lambda *a, **k: agentic_result)

        native_called = []
        monkeypatch.setattr(
            "harness.edit_engines.run_native_edit",
            lambda *a, **k: native_called.append(True) or WorkerResult(ok=False),
        )

        result = run_edit_worker(cfg, "goal")
        assert result is agentic_result
        assert native_called == []
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_run_edit_worker_falls_back_on_agentic_unavailable(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.select_edit_engine", lambda *a, **k: "agentic")
        monkeypatch.setattr(
            "harness.edit_engines.run_agentic_edit",
            lambda *a, **k: WorkerResult(ok=False, error=AGENTIC_UNAVAILABLE, summary="no key"),
        )
        native_sentinel = WorkerResult(ok=True, summary="native fallback")
        monkeypatch.setattr("harness.edit_engines.run_native_edit", lambda *a, **k: native_sentinel)

        result = run_edit_worker(cfg, "goal", job_id="jid")
        assert result is native_sentinel
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_run_edit_worker_no_fallback_on_empty_agentic_result(monkeypatch):
    """Empty diff is not a fallback reason — native must NOT run."""
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.select_edit_engine", lambda *a, **k: "agentic")
        empty = WorkerResult(ok=False, summary="no changes produced")
        monkeypatch.setattr("harness.edit_engines.run_agentic_edit", lambda *a, **k: empty)

        native_called = []
        monkeypatch.setattr(
            "harness.edit_engines.run_native_edit",
            lambda *a, **k: native_called.append(True) or WorkerResult(ok=False),
        )

        result = run_edit_worker(cfg, "goal")
        assert result is empty
        assert native_called == []
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_run_edit_worker_falls_back_on_route_and_runtime_errors(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.select_edit_engine", lambda *a, **k: "agentic")
        native_sentinel = WorkerResult(ok=True, summary="native")

        for err in (AGENTIC_ROUTE_FAILED, AGENTIC_ERROR):
            monkeypatch.setattr(
                "harness.edit_engines.run_agentic_edit",
                lambda *a, err=err, **k: WorkerResult(ok=False, error=err),
            )
            monkeypatch.setattr("harness.edit_engines.run_native_edit", lambda *a, **k: native_sentinel)
            assert run_edit_worker(cfg, "goal") is native_sentinel
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)
