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
    AGENTIC_ORCHESTRATOR_FAILED,
    AGENTIC_PROVIDER_RATE_LIMITED,
    AGENTIC_ROUTE_FAILED,
    AGENTIC_TIMEOUT,
    AGENTIC_UNAVAILABLE,
    PATCH_CAPTURE_FAILED,
    WORKER_CLEANUP_FAILED,
    WORKTREE_CREATE_FAILED,
    agentic_available,
    agentic_events_from_store,
    finalize_worktree_patch,
    managed_worktree,
    pilot_keys_ready,
    workers_ready,
    run_agentic_edit,
    run_edit_worker,
    run_native_edit,
    run_parallel,
    select_edit_engine,
    _agentic_store_failure_snapshot,
    _format_agentic_engine_error,
    _summarize_agentic_result,
    failure_is_retryable,
)
from harness.worker import ProviderWorker, WorkerResult
from pmharness.bridge import _router_supports_max_capability

ROUTER_HAS_CEILING = _router_supports_max_capability()
EXPECTED_CAP_KEY = "max_capability" if ROUTER_HAS_CEILING else "min_capability"


def create_temp_git_repo():
    # Unique parent so xdist workers do not share /tmp/.pmharness-worktrees.
    root = tempfile.mkdtemp()
    repo_dir = os.path.join(root, "repo")
    os.mkdir(repo_dir)
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
def _fake_managed_worktree(*_args, **_kwargs):
    wt = tempfile.mkdtemp()
    try:
        yield wt
    finally:
        shutil.rmtree(wt, ignore_errors=True)


def _install_agentic_mocks(
    monkeypatch, *, orchestrator_result=None, capture_payload=None,
    capture_specs=None, capture_run=None,
):
    """Patch Puppetmaster + worktree so run_agentic_edit stays hermetic."""
    monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)
    monkeypatch.setattr("harness.edit_engines.managed_worktree", _fake_managed_worktree)
    monkeypatch.setattr("harness.edit_engines.managed_worktree_for_goal", _fake_managed_worktree)

    storage: list[dict] = capture_payload if capture_payload is not None else []
    specs_out: list[dict] = capture_specs if capture_specs is not None else []
    runs_out: list[dict] = capture_run if capture_run is not None else []

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
            runs_out.append({"goal": goal, "worker_mode": worker_mode, **kwargs})
            return orchestrator_result or _fake_pm_result()

    monkeypatch.setattr("puppetmaster.workers.WorkerSpec", _CapturingWorkerSpec)
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr("puppetmaster.store_factory.create_store", lambda *a, **k: MagicMock())

    return storage


# --- pure helpers: agentic fail-closed snapshot ---


def test_agentic_store_failure_snapshot_prefers_gate_reason():
    class _Store:
        def list_jobs(self):
            return [type("J", (), {"id": "job_1"})()]

        def list_tasks(self, job_id):
            assert job_id == "job_1"
            return [type("T", (), {"id": "t1", "role": "implement", "status": "failed"})()]

        def read_events(self, job_id):
            return [
                {"event": "worker.failed_task", "payload": {"error": "adapter boom"}},
                {
                    "event": "worker.gate_failed",
                    "payload": {"reason": "require_diff: no PATCH artifact"},
                },
            ]

        def list_artifacts(self, job_id):
            art = type("A", (), {})()
            art.payload = {"tokens_in": 40, "tokens_out": 12, "failure": "no_model"}
            return [art]

    snap = _agentic_store_failure_snapshot(_Store())
    assert snap["job_id"] == "job_1"
    assert snap["reason"] == "require_diff: no PATCH artifact"
    assert snap["task_statuses"] == ["implement=failed"]
    assert snap["tokens_in"] == 40
    assert snap["tokens_out"] == 12
    assert snap["usage_known"] is True
    assert snap["task_ids"] == ["t1"]
    summary = _format_agentic_engine_error(
        RuntimeError("swarm exited with incomplete tasks"),
        snap,
        ["src/lib/scoring/report.ts"],
    )
    assert "incomplete tasks" in summary
    assert "require_diff: no PATCH artifact" in summary
    assert "tasks: implement=failed" in summary
    assert "unapplied worktree files: src/lib/scoring/report.ts" in summary


def test_agentic_store_failure_snapshot_unknown_usage_is_not_measured_zero():
    class _Store:
        def list_jobs(self):
            return [type("J", (), {"id": "job_1"})()]

        def list_tasks(self, job_id):
            return [type("T", (), {"id": "t9", "role": "implement", "status": "failed"})()]

        def read_events(self, job_id):
            return []

        def list_artifacts(self, job_id):
            art = type("A", (), {})()
            art.payload = {"failure": "boom"}
            return [art]

    snap = _agentic_store_failure_snapshot(_Store())
    assert snap["usage_known"] is False
    assert snap["tokens_in"] == 0
    assert snap["tokens_out"] == 0
    assert snap["task_ids"] == ["t9"]


def test_agentic_store_failure_snapshot_captures_events_when_reason_empty():
    class _Store:
        def list_jobs(self):
            return [type("J", (), {"id": "job_1", "status": "failed"})()]

        def list_tasks(self, job_id):
            return [type("T", (), {
                "id": "t1",
                "role": "implement",
                "status": "failed",
                "error": "",
                "failure": "task attr boom",
            })()]

        def read_events(self, job_id):
            return [
                {"event": "worker.started", "payload": {}},
                {"event": "worker.progress", "payload": {}},
                {"event": "worker.tool_error", "payload": {"error": "adapter 500"}},
            ]

        def list_artifacts(self, job_id):
            art = type("A", (), {})()
            art.payload = {"tokens_in": 3}
            art.type = "PATCH"
            return [art]

    snap = _agentic_store_failure_snapshot(_Store())
    assert snap["reason"] == "adapter 500"
    assert snap["event_names"][-1] == "worker.tool_error"
    assert "worker.started" in snap["event_names"]
    assert snap["job_status"] == "failed"
    assert "PATCH" in snap["artifact_types"]
    assert snap["task_ids"] == ["t1"]
    summary = _format_agentic_engine_error(
        RuntimeError("swarm exited with incomplete tasks"),
        snap,
    )
    assert "incomplete tasks" in summary
    assert "adapter 500" in summary
    assert summary.strip() != "Agentic engine error: swarm exited with incomplete tasks"


def test_format_agentic_engine_error_includes_events_when_reason_empty():
    snap = {
        "reason": "",
        "event_names": ["worker.started", "worker.lease_check"],
        "task_statuses": ["implement=running"],
        "job_status": "running",
    }
    summary = _format_agentic_engine_error(
        RuntimeError("swarm exited with incomplete tasks"),
        snap,
    )
    assert "incomplete tasks" in summary
    assert "events: worker.started, worker.lease_check" in summary
    assert "tasks: implement=running" in summary
    assert summary.strip() != "Agentic engine error: swarm exited with incomplete tasks"


def test_failure_is_retryable_only_timeout_or_429():
    assert failure_is_retryable("agentic_timeout") is True
    assert failure_is_retryable("agentic_provider_rate_limited") is True
    assert failure_is_retryable("agentic_error", 429) is True
    assert failure_is_retryable("agentic_error") is False
    assert failure_is_retryable("agentic_orchestrator_failed", 500) is False


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
    monkeypatch.setattr("harness.edit_engines.cursor_platform_available", lambda: False)
    assert select_edit_engine(cfg, "agentic") == "native"


def test_select_edit_engine_explicit_agentic_fails_closed_not_cursor(monkeypatch):
    """Explicit agentic request must not silently demote to platform cursor."""
    cfg = HarnessConfig()
    monkeypatch.delenv("HARNESS_EDIT_ENGINE", raising=False)
    monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: False)
    monkeypatch.setattr("harness.edit_engines.cursor_platform_available", lambda: True)
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-test-key")
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
    monkeypatch.setattr("harness.edit_engines.cursor_platform_available", lambda: False)
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


def test_pilot_keys_ready_with_opencode_go_when_openrouter_disconnected(monkeypatch, tmp_path):
    """Keyed OpenCode Go dismisses the keyless banner even when OpenRouter is off.

    Must NOT pretend OpenCode Go can back Puppetmaster agentic workers
    (agentic_available stays False when the PM registry is empty).
    """
    from harness import keys as hkeys

    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-go-test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("puppetmaster.providers.available_providers", lambda: [])

    import importlib
    importlib.reload(hkeys)
    hkeys.mark_disconnected("openrouter")
    try:
        assert pilot_keys_ready() is True
        assert agentic_available() is False
    finally:
        hkeys.unmark_disconnected("openrouter")


def test_pilot_keys_ready_stored_opencode_go_and_codex_oauth(monkeypatch, tmp_path):
    """Exact production state: OpenRouter disconnected, state keys for OpenCode Go
    + ChatGPT Codex OAuth — ProviderKeyBanner must stay hidden.

    Stored keys (keys.json) must count the same as ``/api/providers`` has_key,
    even when the matching env vars are unset.
    """
    import json
    from harness import keys as hkeys

    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    for k in (
        "OPENROUTER_API_KEY", "OPENCODE_GO_API_KEY", "OPENAI_CODEX_TOKEN",
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
        "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("puppetmaster.providers.available_providers", lambda: [])

    import importlib
    importlib.reload(hkeys)
    # Write keys.json directly -- avoid set_api_key which mutates os.environ and
    # can leak into later tests via monkeypatch delenv undo.
    keys_path = tmp_path / "keys.json"
    keys_path.write_text(json.dumps({
        "opencode-go": "sk-go-stored-key",
        "openai-codex": "codex-oauth-stored-token-abcdef",
    }), encoding="utf-8")
    hkeys.mark_disconnected("openrouter")
    try:
        from harness.api.providers import get_providers
        _code, rows = get_providers()
        by_name = {r["name"]: r for r in rows}
        assert by_name["opencode-go"]["has_key"] is True
        assert by_name["openai-codex"]["has_key"] is True
        assert by_name["openrouter"]["has_key"] is False
        assert by_name["openrouter"]["disconnected"] is True
        # Banner stays hidden when agentic_ready is true (gate for ProviderKeyBanner
        # is `agentic_ready === false`).
        assert pilot_keys_ready() is True
    finally:
        hkeys.unmark_disconnected("openrouter")


def test_pilot_keys_ready_false_when_truly_keyless(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("puppetmaster.providers.available_providers", lambda: [])
    for k in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY", "OPENCODE_GO_API_KEY", "OPENAI_CODEX_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    # Host machines may have Cursor CLI / OAuth pools; force the authoritative
    # providers path closed so the keyless banner regression stays hermetic.
    monkeypatch.setattr(
        "harness.registry_wizard.get_provider_key", lambda _p: None, raising=False
    )
    monkeypatch.setattr(
        "harness.keys.get_api_key_status",
        lambda _name: {"has_key": False, "masked": ""},
        raising=False,
    )
    monkeypatch.setattr("harness.keys.get_disconnected", lambda: set(), raising=False)
    assert pilot_keys_ready() is False


def test_workers_ready_false_for_cursor_cli_only(monkeypatch, tmp_path):
    """Pilot-only Cursor Agent login must not claim swarms can run."""
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    for k in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY", "OPENCODE_GO_API_KEY", "OPENAI_CODEX_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(k, raising=False)

    def _key(provider):
        return "cursor-cli-login" if provider.name == "cursor-cli" else None

    monkeypatch.setattr("harness.registry_wizard.get_provider_key", _key)
    monkeypatch.setattr(
        "harness.keys.get_api_key_status",
        lambda name: {"has_key": name == "cursor-cli", "masked": "****"},
    )
    monkeypatch.setattr("harness.keys.get_disconnected", lambda: set())
    monkeypatch.setattr(
        "harness.edit_engines.cursor_platform_available", lambda: False
    )
    assert workers_ready() is False
    assert pilot_keys_ready() is True


def test_workers_ready_true_for_stored_openrouter(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    for k in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY", "OPENCODE_GO_API_KEY", "OPENAI_CODEX_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(k, raising=False)

    def _key(provider):
        return "sk-or-stored" if provider.name == "openrouter" else None

    monkeypatch.setattr("harness.registry_wizard.get_provider_key", _key)
    monkeypatch.setattr(
        "harness.keys.get_api_key_status",
        lambda name: {"has_key": name == "openrouter", "masked": "****"},
    )
    monkeypatch.setattr("harness.keys.get_disconnected", lambda: set())
    monkeypatch.setattr(
        "harness.edit_engines.cursor_platform_available", lambda: False
    )
    assert workers_ready() is True
    assert pilot_keys_ready() is True


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


def test_finalize_worktree_patch_rejects_missing_path():
    with pytest.raises(RuntimeError, match="does not exist"):
        finalize_worktree_patch("/no/such/worktree-path")


def test_finalize_worktree_patch_rejects_non_repo(tmp_path):
    with pytest.raises(RuntimeError, match="not a git repository"):
        finalize_worktree_patch(str(tmp_path))


def test_finalize_worktree_patch_name_only_failure_does_not_return_empty(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        with managed_worktree(repo_dir) as wt_path:
            with open(os.path.join(wt_path, "x.txt"), "w", encoding="utf-8") as fh:
                fh.write("x\n")
            real_run = subprocess.run

            def fake_run(cmd, *a, **k):
                if isinstance(cmd, (list, tuple)) and "--name-only" in cmd:
                    return type(
                        "P", (),
                        {"returncode": 1, "stdout": "", "stderr": "name-only boom"},
                    )()
                return real_run(cmd, *a, **k)

            monkeypatch.setattr(subprocess, "run", fake_run)
            with pytest.raises(RuntimeError) as caught:
                finalize_worktree_patch(wt_path)
            text = str(caught.value)
            assert "failed (exit 1)" in text
            assert "--name-only" in text
            assert "name-only boom" in text
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


def test_agentic_payload_uses_immutable_per_action_pin(monkeypatch):
    from harness.swarm_model_pin import AgenticModelPin

    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        captured: list[dict] = []
        _install_agentic_mocks(
            monkeypatch,
            capture_payload=captured,
            orchestrator_result=_fake_pm_result([
                _fake_artifact(model="stealth/ox-alpha", stdout="ok"),
            ]),
        )
        monkeypatch.setenv("HARNESS_IMPLEMENT_PROVIDER", "openai")
        monkeypatch.setenv("HARNESS_IMPLEMENT_MODEL", "wrong-model")
        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("patch", ["a.txt"]),
        )
        pin = AgenticModelPin(
            requested="openrouter/stealth/ox-alpha",
            provider="openrouter",
            model="stealth/ox-alpha",
            router_model_id="agentic/openrouter/stealth/ox-alpha",
        )

        result = run_agentic_edit(cfg, "goal", agentic_pin=pin)

        assert result.ok is True
        assert result.requested_model == pin.requested
        assert result.provider == "openrouter"
        assert result.routing_policy == "explicit_pin"
        payload = captured[0]
        assert payload["provider"] == "openrouter"
        assert payload["model"] == "stealth/ox-alpha"
        assert payload["auto_route"] is False
        assert payload["allowed_adapters"] == ["agentic"]
        assert payload["pinned_model"] == pin.router_model_id
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_payload_fails_closed_on_routed_model_mismatch(monkeypatch):
    from harness.swarm_model_pin import AgenticModelPin

    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        routed = _fake_artifact(model="other/model", stdout="wrong model")
        routed.type = ""
        _install_agentic_mocks(
            monkeypatch,
            orchestrator_result=_fake_pm_result([routed]),
        )
        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("patch", ["a.txt"]),
        )
        pin = AgenticModelPin(
            requested="openrouter/stealth/ox-alpha",
            provider="openrouter",
            model="stealth/ox-alpha",
            router_model_id="agentic/openrouter/stealth/ox-alpha",
        )

        result = run_agentic_edit(cfg, "goal", agentic_pin=pin)

        assert result.ok is False
        assert result.error == AGENTIC_ROUTE_FAILED
        assert "model mismatch" in result.summary.lower()
        assert result.requested_model == pin.requested
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


def test_agentic_analysis_unlabeled_prose_fails_structured_gate(monkeypatch):
    """expects_diff=False must not green on thin unlabeled non-reasoning prose."""
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        _install_agentic_mocks(
            monkeypatch,
            orchestrator_result=_fake_pm_result([
                _fake_artifact(
                    stdout="The auth module looks fine overall after a careful pass.",
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
        assert "missing FINDING/RISK/DECISION" in (
            result.error or result.summary or ""
        )
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_analysis_coerces_substantive_unlabeled_prose(monkeypatch):
    """Substantive unlabeled path-citing prose soft-coerces before fail-closed."""
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        prose = (
            "harness/edit_engines.py:505 analysis empty-diff path must coerce "
            "substantive unlabeled prose that cites a concrete path:line locus."
        )
        _install_agentic_mocks(
            monkeypatch,
            orchestrator_result=_fake_pm_result([
                _fake_artifact(
                    stdout=prose,
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
        assert result.ok is True
        assert "FINDING:" in (result.summary or "")
        assert any(
            isinstance(r, dict) and r.get("type") == "finding"
            for r in (result.findings or [])
        )
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_analysis_promotes_verification_parked_prose(monkeypatch):
    """empty_or_unstructured verification prose that bridge promotes must green.

    Prose lives in claim (not stdout) so final_text is empty and
    coerce_unlabeled_analysis_prose cannot rescue — only
    rescue_analysis_compact / _promote_degraded_prose can.
    """
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        prose = (
            "harness/edit_engines.py:506 analysis empty-diff path must promote "
            "verification-parked empty_or_unstructured prose so agentic edit "
            "agrees with swarm/bridge when the worker never labeled FINDING."
        )
        parked = _fake_artifact(
            claim=prose,
            tokens_out=3,
            tokens_in=2,
            failure="empty_or_unstructured_agentic_result",
        )
        parked.type = "verification"
        _install_agentic_mocks(
            monkeypatch,
            orchestrator_result=_fake_pm_result([parked]),
        )
        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("", []),
        )
        # Prove coerce alone cannot green this fixture (no final_text).
        from harness.worker import coerce_unlabeled_analysis_prose
        assert coerce_unlabeled_analysis_prose("") == ""

        result = run_agentic_edit(cfg, "audit promote path", expects_diff=False)
        assert result.ok is True
        assert "FINDING:" in (result.summary or "")
        assert "harness/edit_engines.py:506" in (result.summary or "")
        assert any(
            isinstance(r, dict) and r.get("type") == "finding"
            for r in (result.findings or [])
        )
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_analysis_summary_from_artifacts_is_substantive(monkeypatch):
    """Artifact FINDING must become a parent-gate-passing summary, not a stub."""
    from harness.edit_engines import _agentic_analysis_summary
    from harness.pilot_guards import analysis_summary_is_substantive

    claim = (
        "FINDING: harness/edit_engines.py:330 analysis must use "
        "read-only analyze mode instead of mode=implement."
    )
    summary = _agentic_analysis_summary(
        [{"type": "finding", "headline": claim, "body": claim, "empty_headline": False}],
        "",
    )
    assert "FINDING:" in summary
    assert analysis_summary_is_substantive(summary)

    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        finding = _fake_artifact(claim=claim, tokens_out=10, tokens_in=5)
        finding.type = "finding"
        _install_agentic_mocks(
            monkeypatch,
            orchestrator_result=_fake_pm_result([finding]),
        )
        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("", []),
        )
        result = run_agentic_edit(cfg, "audit analyze path", expects_diff=False)
        assert result.ok is True
        assert analysis_summary_is_substantive(result.summary or "")
        assert "FINDING:" in (result.summary or "")
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


def test_agentic_edit_worktree_create_failed(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)

        @contextlib.contextmanager
        def _boom_worktree(*_args, **_kwargs):
            raise RuntimeError(
                "fatal: could not create worktree api_key=sk-abcdefghijklmnopqrstuvwxyz"
            )
            yield ""  # pragma: no cover

        monkeypatch.setattr(
            "harness.edit_engines.managed_worktree_for_goal", _boom_worktree,
        )
        result = run_agentic_edit(cfg, "goal")
        assert result.ok is False
        assert result.error == WORKTREE_CREATE_FAILED
        assert result.requested_mode == "implement"
        assert result.managed_worktree_mode == "none"
        assert result.patch_capture_status == "skipped"
        assert "fatal" in result.summary
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in result.summary
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in result.failure_stderr
        assert result.failure_command
        assert result.engine == "agentic"
        assert result.adapter == "agentic"
        assert result.worktree_diff_empty is None
        assert "unavailable" not in (result.summary or "").lower()
        assert "mode=unknown" not in (result.summary or "")
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_unclassified_crash_stamps_requested_mode(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)

        def boom_store(*_a, **_k):
            raise RuntimeError("store exploded")

        monkeypatch.setattr("puppetmaster.store_factory.create_store", boom_store)
        result = run_agentic_edit(cfg, "goal")
        assert result.ok is False
        assert result.error == AGENTIC_ERROR
        assert result.requested_mode == "implement"
        assert result.engine == "agentic"
        assert result.adapter == "agentic"
        assert result.managed_worktree_mode == "managed"
        assert "store exploded" in result.summary
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_incomplete_swarm_stamps_provenance_and_gate_reason(monkeypatch):
    """Orchestrator fail-closed must not look like 'worktree status unavailable'."""
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)

        class _BoomOrch:
            def __init__(self, store):
                self.store = store

            def run(self, goal, specs=None, **kwargs):
                payload = ((specs[0].payload if specs else {}) or {})
                cwd = payload.get("cwd") or ""
                if cwd:
                    with open(os.path.join(cwd, "test.txt"), "a", encoding="utf-8") as fh:
                        fh.write("from-worker\n")
                job, _created = self.store.create_or_get_job(goal or "g", label="boom")
                self.store.emit(
                    job.id,
                    "worker.gate_failed",
                    {"reason": "require_diff: no PATCH artifact", "task_id": "t1"},
                )
                raise RuntimeError("swarm exited with incomplete tasks")

        monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _BoomOrch)
        result = run_agentic_edit(cfg, "implement the thing")
        assert result.ok is False
        assert result.error == AGENTIC_ORCHESTRATOR_FAILED
        assert result.requested_mode == "implement"
        assert result.engine == "agentic"
        assert result.adapter == "agentic"
        assert result.managed_worktree_mode == "managed"
        assert result.worktree_diff_empty is False
        assert result.patch_capture_status == "ok"
        assert "test.txt" in (result.files_changed or [])
        assert (result.patch or "").strip()
        assert "incomplete tasks" in (result.summary or "")
        assert "require_diff" in (result.summary or "")
        assert result.pm_job_id
        assert "unavailable" not in (result.summary or "").lower()
        assert "mode=unknown" not in (result.summary or "")
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_incomplete_swarm_empty_worktree_is_explicit(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)

        class _BoomOrch:
            def __init__(self, store):
                self.store = store

            def run(self, goal, specs=None, **kwargs):
                job, _created = self.store.create_or_get_job(goal or "g", label="boom")
                self.store.emit(
                    job.id,
                    "worker.failed_task",
                    {"error": "adapter boom", "task_id": "t1"},
                )
                raise RuntimeError("swarm exited with incomplete tasks")

        monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _BoomOrch)
        result = run_agentic_edit(cfg, "implement the thing")
        assert result.ok is False
        assert result.error == AGENTIC_ORCHESTRATOR_FAILED
        assert result.requested_mode == "implement"
        assert result.managed_worktree_mode == "managed"
        assert result.worktree_diff_empty is True
        assert result.files_changed == []
        assert "adapter boom" in (result.summary or "")
        assert "unavailable" not in (result.summary or "").lower()
        assert "mode=unknown" not in (result.summary or "")
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
        assert result.requested_mode == "implement"
        assert result.engine == "agentic"
        assert result.adapter == "agentic"
        assert result.patch_capture_status == "ok"
        assert result.usage_known is True
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


# --- run_edit_worker dispatch and fallback ---


def test_run_edit_worker_forwards_job_id_to_agentic(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.select_edit_engine", lambda *a, **k: "agentic")
        seen = {}

        def fake_agentic(config, goal, **kwargs):
            seen.update(kwargs)
            return WorkerResult(ok=True, patch="p", summary="ok")

        monkeypatch.setattr("harness.edit_engines.run_agentic_edit", fake_agentic)
        result = run_edit_worker(
            cfg, "do it", job_id="local-abc", session_id="sess-1",
        )
        assert result.ok is True
        assert seen["job_id"] == "local-abc"
        assert seen["session_id"] == "sess-1"
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_marks_scratch_and_stamps_host_dispatch(monkeypatch):
    import json

    from harness.cli_job_merge import HOST_SCRATCH_MARKER_NAME, is_marionette_host_scratch_dir

    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        marked = []
        runs = []
        from harness.cli_job_merge import mark_marionette_host_scratch as real_mark

        def _spy_mark(path):
            marked.append(path)
            real_mark(path)
            assert is_marionette_host_scratch_dir(path)
            assert os.path.basename(path).startswith("pmh-edit-")
            assert os.path.isfile(os.path.join(path, HOST_SCRATCH_MARKER_NAME))

        monkeypatch.setattr(
            "harness.cli_job_merge.mark_marionette_host_scratch", _spy_mark,
        )
        _install_agentic_mocks(
            monkeypatch,
            orchestrator_result=_fake_pm_result([
                _fake_artifact(tokens_out=10, tokens_in=4, stdout="ok"),
            ]),
            capture_run=runs,
        )
        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("diff --git a/x b/x\n+line", ["x.py"]),
        )
        result = run_agentic_edit(
            cfg, "goal", session_id="sess-z", job_id="local-host-1",
        )
        assert result.ok is True
        assert marked
        assert os.path.basename(marked[0]).startswith("pmh-edit-")
        assert runs
        label = json.loads(runs[0]["label"])
        assert label["session_id"] == "sess-z"
        assert label["dispatch_id"] == "local-host-1"
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_cursor_edit_marks_scratch_and_stamps_host_dispatch(monkeypatch):
    import json

    from harness.edit_engines import run_cursor_edit

    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        marked = []
        runs = []
        from harness.cli_job_merge import mark_marionette_host_scratch as real_mark

        def _spy_mark(path):
            marked.append(path)
            real_mark(path)

        monkeypatch.setattr(
            "harness.cli_job_merge.mark_marionette_host_scratch", _spy_mark,
        )
        monkeypatch.setattr("harness.edit_engines.cursor_platform_available", lambda: True)
        _install_agentic_mocks(monkeypatch, capture_run=runs)
        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("diff --git a/x b/x\n+line", ["x.py"]),
        )
        result = run_cursor_edit(
            cfg, "goal", session_id="sess-c", job_id="local-cur-1",
        )
        assert result.ok is True
        assert marked
        assert os.path.basename(marked[0]).startswith("pmh-cursor-edit-")
        label = json.loads(runs[0]["label"])
        assert label["session_id"] == "sess-c"
        assert label["dispatch_id"] == "local-cur-1"
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


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
        monkeypatch.setattr("harness.edit_engines.cursor_platform_available", lambda: False)
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


def test_run_edit_worker_explicit_agentic_never_falls_back(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr(
            "harness.edit_engines.agentic_available", lambda: True,
        )
        monkeypatch.setattr(
            "harness.edit_engines.agentic_platform_enabled", lambda: True,
        )
        agentic_failure = WorkerResult(
            ok=False,
            error=AGENTIC_ROUTE_FAILED,
            summary="route failed",
        )
        monkeypatch.setattr(
            "harness.edit_engines.run_agentic_edit",
            lambda *args, **kwargs: agentic_failure,
        )
        fallback_calls = []
        monkeypatch.setattr(
            "harness.edit_engines.run_cursor_edit",
            lambda *args, **kwargs: fallback_calls.append("cursor"),
        )
        monkeypatch.setattr(
            "harness.edit_engines.run_native_edit",
            lambda *args, **kwargs: fallback_calls.append("native"),
        )

        result = run_edit_worker(
            cfg,
            "goal",
            requested_adapter="agentic",
        )

        assert result is agentic_failure
        assert fallback_calls == []
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_run_edit_worker_explicit_agentic_unavailable_fails_closed(monkeypatch):
    cfg = HarnessConfig()
    monkeypatch.setattr(
        "harness.edit_engines.agentic_platform_enabled", lambda: True,
    )
    monkeypatch.setattr(
        "harness.edit_engines.agentic_available", lambda: False,
    )
    fallback_calls = []
    monkeypatch.setattr(
        "harness.edit_engines.run_native_edit",
        lambda *args, **kwargs: fallback_calls.append("native"),
    )

    result = run_edit_worker(
        cfg,
        "goal",
        requested_adapter="agentic",
    )

    assert result.ok is False
    assert result.error == AGENTIC_UNAVAILABLE
    assert result.engine == "agentic"
    assert fallback_calls == []


def test_run_parallel_forwards_same_agentic_pin_to_every_child(monkeypatch):
    from harness.swarm_model_pin import AgenticModelPin

    pin = AgenticModelPin(
        requested="openrouter/stealth/ox-alpha",
        provider="openrouter",
        model="stealth/ox-alpha",
        router_model_id="agentic/openrouter/stealth/ox-alpha",
    )
    seen = []

    def fake_implement(config, goal, **kwargs):
        seen.append((goal, kwargs))
        return WorkerResult(ok=True, patch="patch")

    monkeypatch.setattr("harness.edit_engines.run_implement", fake_implement)

    results = run_parallel(
        HarnessConfig(),
        ["one", "two"],
        requested_adapter="agentic",
        agentic_pin=pin,
        strict_adapter=True,
    )

    assert len(results) == 2
    assert [goal for goal, _kwargs in seen] == ["one", "two"]
    assert all(kwargs["agentic_pin"] is pin for _goal, kwargs in seen)
    assert all(kwargs["strict_adapter"] is True for _goal, kwargs in seen)


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
        monkeypatch.setattr("harness.edit_engines.cursor_platform_available", lambda: False)
        native_sentinel = WorkerResult(ok=True, summary="native")

        for err in (AGENTIC_ROUTE_FAILED, WORKTREE_CREATE_FAILED):
            monkeypatch.setattr(
                "harness.edit_engines.run_agentic_edit",
                lambda *a, err=err, **k: WorkerResult(ok=False, error=err),
            )
            monkeypatch.setattr("harness.edit_engines.run_native_edit", lambda *a, **k: native_sentinel)
            assert run_edit_worker(cfg, "goal") is native_sentinel

        native_called = []
        monkeypatch.setattr(
            "harness.edit_engines.run_native_edit",
            lambda *a, **k: native_called.append(True) or WorkerResult(ok=False),
        )
        for err in (
            AGENTIC_ORCHESTRATOR_FAILED, PATCH_CAPTURE_FAILED, AGENTIC_ERROR,
            WORKER_CLEANUP_FAILED,
        ):
            monkeypatch.setattr(
                "harness.edit_engines.run_agentic_edit",
                lambda *a, err=err, **k: WorkerResult(ok=False, error=err, patch=""),
            )
            result = run_edit_worker(cfg, "goal")
            assert result.error == err
            assert native_called == []
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_run_edit_worker_no_fallback_on_partial_implement(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.select_edit_engine", lambda *a, **k: "agentic")
        monkeypatch.setattr("harness.edit_engines.cursor_platform_available", lambda: False)
        native_called = []
        monkeypatch.setattr(
            "harness.edit_engines.run_native_edit",
            lambda *a, **k: native_called.append(True) or WorkerResult(ok=False),
        )
        partial = WorkerResult(
            ok=False,
            error=WORKTREE_CREATE_FAILED,
            patch="diff --git a/x b/x\n+line",
            files_changed=["x.py"],
        )
        monkeypatch.setattr(
            "harness.edit_engines.run_agentic_edit",
            lambda *a, **k: partial,
        )
        result = run_edit_worker(cfg, "goal")
        assert result is partial
        assert native_called == []
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_ignores_parent_dirty_file(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        parent_file = os.path.join(repo_dir, "test.txt")
        with open(parent_file, "a", encoding="utf-8") as fh:
            fh.write("parent-dirty\n")
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)

        class _Orch:
            def __init__(self, store):
                self.store = store

            def run(self, goal, specs=None, **kwargs):
                payload = ((specs[0].payload if specs else {}) or {})
                cwd = payload.get("cwd") or ""
                with open(os.path.join(cwd, "new_from_worker.txt"), "w", encoding="utf-8") as out:
                    out.write("only-in-wt\n")
                return _fake_pm_result([
                    _fake_artifact(tokens_out=1, tokens_in=1, stdout="ok"),
                ])

        monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _Orch)
        result = run_agentic_edit(cfg, "add worker_only.txt")
        assert result.ok is True
        assert "new_from_worker.txt" in (result.files_changed or [])
        assert "test.txt" not in (result.files_changed or [])
        with open(parent_file, encoding="utf-8") as fh:
            assert "parent-dirty" in fh.read()
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_patch_capture_failed(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        _install_agentic_mocks(monkeypatch, orchestrator_result=_fake_pm_result([
            _fake_artifact(tokens_out=2, tokens_in=1, stdout="ok"),
        ]))

        def boom_finalize(_wt):
            raise RuntimeError(
                "git -C /tmp/wt diff --cached --name-only failed (exit 1): boom"
            )

        monkeypatch.setattr("harness.edit_engines.finalize_worktree_patch", boom_finalize)
        result = run_agentic_edit(cfg, "goal")
        assert result.ok is False
        assert result.error == PATCH_CAPTURE_FAILED
        assert result.patch == ""
        assert result.worktree_diff_empty is None
        assert result.patch_capture_status == "failed"
        assert result.requested_mode == "implement"
        assert result.managed_worktree_mode == "managed"
        assert result.worktree_diff_empty is not True
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_finalize_fail_keeps_orchestrator_error(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)

        class _BoomOrch:
            def __init__(self, store):
                self.store = store

            def run(self, goal, specs=None, **kwargs):
                raise RuntimeError("swarm exited with incomplete tasks")

        monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _BoomOrch)
        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: (_ for _ in ()).throw(RuntimeError("git add failed")),
        )
        result = run_agentic_edit(cfg, "implement the thing")
        assert result.error == AGENTIC_ORCHESTRATOR_FAILED
        assert result.patch_capture_status == "failed"
        assert result.worktree_diff_empty is None
        assert result.managed_worktree_mode == "managed"
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_cleanup_failure_keeps_orchestrator_error(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)

        class _BoomOrch:
            def __init__(self, store):
                self.store = store

            def run(self, goal, specs=None, **kwargs):
                raise RuntimeError("swarm exited with incomplete tasks")

        monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _BoomOrch)
        real_rmtree = shutil.rmtree

        def boom_rmtree(path, *a, **k):
            if os.path.basename(str(path)).startswith("pmh-edit-"):
                if k.get("ignore_errors"):
                    return real_rmtree(path, *a, **k)
                raise OSError("rmtree denied")
            return real_rmtree(path, *a, **k)

        monkeypatch.setattr("harness.edit_engines.shutil.rmtree", boom_rmtree)
        result = run_agentic_edit(cfg, "implement the thing")
        assert result.error == AGENTIC_ORCHESTRATOR_FAILED
        assert "incomplete tasks" in (result.summary or "")
        assert "Cleanup also failed" in (result.summary or "")
        assert result.managed_worktree_mode == "managed"
        assert result.cleanup_status == "failed"
        assert "store" in (result.cleanup_stage or "")
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_apply_cleanup_provenance_keeps_primary_and_patch():
    from harness.edit_engines import _apply_cleanup_provenance

    wr = WorkerResult(
        ok=False,
        error=AGENTIC_ORCHESTRATOR_FAILED,
        summary="orch boom",
        patch="diff --git a/x b/x\n+line",
    )
    _apply_cleanup_provenance(wr, [{"stage": "store", "exc": OSError("rmtree denied")}])
    assert wr.error == AGENTIC_ORCHESTRATOR_FAILED
    assert wr.ok is False
    assert wr.patch.startswith("diff --git")
    assert wr.cleanup_status == "failed"
    assert wr.cleanup_stage == "store"
    assert "Cleanup also failed" in wr.summary

    clean = WorkerResult(ok=True, patch="diff --git a/y b/y\n+ok")
    _apply_cleanup_provenance(clean, [
        {"stage": "worktree_remove", "exc": RuntimeError("worktree remove denied")},
    ])
    assert clean.error == WORKER_CLEANUP_FAILED
    assert clean.ok is False
    assert clean.patch.startswith("diff --git")
    assert clean.cleanup_stage == "worktree_remove"
    assert "Worker cleanup failed" in clean.summary


def test_managed_worktree_records_remove_failure(monkeypatch):
    from harness.worktrees import list_worktrees
    from harness.worktrees import remove_worktree as real_remove

    repo_dir = create_temp_git_repo()
    bag = []

    def boom_remove(*_a, **_k):
        raise RuntimeError("worktree remove denied")

    monkeypatch.setattr("harness.worktrees.remove_worktree", boom_remove)
    try:
        with managed_worktree(repo_dir, cleanup_errors=bag) as wt_path:
            assert os.path.isdir(wt_path)
        assert bag
        assert bag[0]["stage"] == "worktree_remove"
    finally:
        for wt in list_worktrees(repo_dir):
            path = (wt.get("path") or "").strip()
            if path and os.path.realpath(path) != os.path.realpath(repo_dir):
                with contextlib.suppress(Exception):
                    real_remove(repo_dir, path, force=True)
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_worktree_remove_failure_keeps_patch(monkeypatch):
    from harness.worktrees import list_worktrees
    from harness.worktrees import remove_worktree as real_remove

    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)

        class _OkOrch:
            def __init__(self, store):
                self.store = store

            def run(self, goal, specs=None, **kwargs):
                return _fake_pm_result([
                    _fake_artifact(tokens_out=1, tokens_in=1, stdout="ok"),
                ])

        monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _OkOrch)
        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("diff --git a/x b/x\n+line", ["x.py"]),
        )

        def boom_remove(*_a, **_k):
            raise RuntimeError("worktree remove denied")

        monkeypatch.setattr("harness.worktrees.remove_worktree", boom_remove)
        result = run_agentic_edit(cfg, "goal")
        assert result.ok is False
        assert result.error == WORKER_CLEANUP_FAILED
        assert result.patch.startswith("diff --git")
        assert result.cleanup_status == "failed"
        assert "worktree_remove" in (result.cleanup_stage or "").split(",")
        assert "Worker cleanup failed" in (result.summary or "")
    finally:
        for wt in list_worktrees(repo_dir):
            path = (wt.get("path") or "").strip()
            if path and os.path.realpath(path) != os.path.realpath(repo_dir):
                with contextlib.suppress(Exception):
                    real_remove(repo_dir, path, force=True)
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_branch_delete_failure_keeps_patch(monkeypatch):
    from harness.worktrees import delete_branch as real_delete
    from harness.worktrees import list_worktrees
    from harness.worktrees import remove_worktree as real_remove

    repo_dir = create_temp_git_repo()

    def boom_delete(repo, branch, raise_on_error=False):
        if raise_on_error:
            raise RuntimeError("branch delete denied")
        return real_delete(repo, branch, raise_on_error=raise_on_error)

    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)

        class _OkOrch:
            def __init__(self, store):
                self.store = store

            def run(self, goal, specs=None, **kwargs):
                return _fake_pm_result([
                    _fake_artifact(tokens_out=1, tokens_in=1, stdout="ok"),
                ])

        monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _OkOrch)
        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("diff --git a/x b/x\n+line", ["x.py"]),
        )
        monkeypatch.setattr("harness.worktrees.delete_branch", boom_delete)
        result = run_agentic_edit(cfg, "goal")
        assert result.ok is False
        assert result.error == WORKER_CLEANUP_FAILED
        assert result.patch.startswith("diff --git")
        assert result.cleanup_status == "failed"
        assert result.cleanup_stage == "branch_delete"
    finally:
        monkeypatch.setattr("harness.worktrees.delete_branch", real_delete)
        for wt in list_worktrees(repo_dir):
            path = (wt.get("path") or "").strip()
            if path and os.path.realpath(path) != os.path.realpath(repo_dir):
                with contextlib.suppress(Exception):
                    real_remove(repo_dir, path, force=True)
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_no_leaked_pmh_edit_dirs(monkeypatch):
    repo_dir = create_temp_git_repo()
    created = []
    real_mkdtemp = tempfile.mkdtemp

    def spy_mkdtemp(*a, **k):
        path = real_mkdtemp(*a, **k)
        if str(k.get("prefix") or "").startswith("pmh-edit"):
            created.append(path)
        return path

    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("tempfile.mkdtemp", spy_mkdtemp)
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)
        _install_agentic_mocks(monkeypatch, orchestrator_result=_fake_pm_result([
            _fake_artifact(tokens_out=1, tokens_in=1, stdout="ok"),
        ]))
        monkeypatch.setattr(
            "harness.edit_engines.finalize_worktree_patch",
            lambda _wt: ("diff --git a/x b/x\n+line", ["x.py"]),
        )
        ok_result = run_agentic_edit(cfg, "goal")
        assert ok_result.ok is True
        assert ok_result.cleanup_status == "ok"

        class _BoomOrch:
            def __init__(self, store):
                self.store = store

            def run(self, goal, specs=None, **kwargs):
                raise RuntimeError("swarm exited with incomplete tasks")

        monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _BoomOrch)
        fail_result = run_agentic_edit(cfg, "goal")
        assert fail_result.ok is False
        assert created
        for path in created:
            assert not os.path.exists(path)
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_managed_worktree_for_goal_paths_are_distinct():
    from harness.edit_engines import managed_worktree_for_goal

    repo_dir = create_temp_git_repo()
    try:
        with managed_worktree_for_goal(repo_dir, "goal a") as wt1:
            with open(os.path.join(wt1, "only_in_first.txt"), "w", encoding="utf-8") as fh:
                fh.write("secret-patch-a\n")
            patch1, files1 = finalize_worktree_patch(wt1)
            with managed_worktree_for_goal(repo_dir, "goal b") as wt2:
                assert wt1 != wt2
                patch2, files2 = finalize_worktree_patch(wt2)
                assert "only_in_first.txt" not in files2
                assert "secret-patch-a" not in patch2
            assert "only_in_first.txt" in files1
        assert not os.path.exists(wt1)
        assert not os.path.exists(wt2)
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_classifies_rate_limit(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)

        class _BoomOrch:
            def __init__(self, store):
                self.store = store

            def run(self, goal, specs=None, **kwargs):
                raise RuntimeError("HTTP 429 Too Many Requests Retry-After: 12 quota exceeded")

        monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _BoomOrch)
        result = run_agentic_edit(cfg, "implement the thing")
        assert result.error == AGENTIC_PROVIDER_RATE_LIMITED
        assert result.http_status == 429
        assert result.retry_after == "12"
        assert result.managed_worktree_mode == "managed"
        assert result.requested_mode == "implement"
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_classifies_timeout(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)

        class _BoomOrch:
            def __init__(self, store):
                self.store = store

            def run(self, goal, specs=None, **kwargs):
                raise TimeoutError("request timed out waiting for provider")

        monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _BoomOrch)
        result = run_agentic_edit(cfg, "implement the thing")
        assert result.error == AGENTIC_TIMEOUT
        assert result.managed_worktree_mode == "managed"
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def test_agentic_edit_connection_error_is_orchestrator_failed(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        cfg = _cfg(repo_dir)
        monkeypatch.setattr("harness.edit_engines.agentic_available", lambda: True)

        class _BoomOrch:
            def __init__(self, store):
                self.store = store

            def run(self, goal, specs=None, **kwargs):
                raise RuntimeError("connection refused")

        monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _BoomOrch)
        result = run_agentic_edit(cfg, "implement the thing")
        assert result.error == AGENTIC_ORCHESTRATOR_FAILED
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)
