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


def _install_agentic_mocks(monkeypatch, *, orchestrator_result=None, capture_payload=None):
    """Patch Puppetmaster + worktree so run_agentic_edit stays hermetic."""
    monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)
    monkeypatch.setattr("harness.edit_engines.managed_worktree", _fake_managed_worktree)

    storage: list[dict] = capture_payload if capture_payload is not None else []

    class _CapturingWorkerSpec:
        def __init__(self, role, instruction, adapter, payload):
            storage.append(payload)
            self.role = role
            self.instruction = instruction
            self.adapter = adapter
            self.payload = payload

    class _FakeOrchestrator:
        def __init__(self, store):
            self.store = store

        def run(self, goal, specs=None, worker_mode="inline"):
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
        assert len(captured) == 1
        payload = captured[0]
        assert payload["mode"] == "implement"
        assert payload["routing_policy"] == "balanced"
        assert payload["auto_route"] is True
        assert EXPECTED_CAP_KEY in payload
        assert payload[EXPECTED_CAP_KEY] == 86
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

        def fake_native(config, goal, job_id=""):
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
