"""resolve_effective_repo: Marionette Home parent → single git child checkout."""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from harness.repo_resolve import clear_effective_repo_cache, resolve_effective_repo

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git not available",
)


def _git_init(repo: str) -> None:
    subprocess.run(
        ["git", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_effective_repo_cache()
    yield
    clear_effective_repo_cache()


def test_root_is_git_repo_returns_toplevel(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git_init(str(root))
    got = resolve_effective_repo(str(root))
    assert _norm(got) == _norm(str(root))


def test_exactly_one_git_child_returns_child(tmp_path):
    home = tmp_path / "marionette-home"
    home.mkdir()
    (home / "notes.txt").write_text("not a repo", encoding="utf-8")
    child = home / "marionette"
    child.mkdir()
    _git_init(str(child))
    got = resolve_effective_repo(str(home))
    assert _norm(got) == _norm(str(child))


def test_two_git_children_returns_root_unchanged(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    a = home / "a"
    b = home / "b"
    a.mkdir()
    b.mkdir()
    _git_init(str(a))
    _git_init(str(b))
    got = resolve_effective_repo(str(home))
    assert _norm(got) == _norm(str(home))


def test_non_git_root_no_git_children_unchanged(tmp_path):
    home = tmp_path / "empty-home"
    home.mkdir()
    (home / "subdir").mkdir()
    (home / "readme.md").write_text("hi", encoding="utf-8")
    got = resolve_effective_repo(str(home))
    assert _norm(got) == _norm(str(home))


def test_git_subdirectory_returns_toplevel(tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    _git_init(str(root))
    nested = root / "pkg" / "inner"
    nested.mkdir(parents=True)
    got = resolve_effective_repo(str(nested))
    assert _norm(got) == _norm(str(root))


def test_empty_root_unchanged():
    assert resolve_effective_repo("") == ""
    assert resolve_effective_repo("   ") == "   "


def test_result_is_cached(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    child = home / "only"
    child.mkdir()
    _git_init(str(child))
    first = resolve_effective_repo(str(home))
    assert _norm(first) == _norm(str(child))

    calls = {"n": 0}
    real_listdir = os.listdir

    def counting_listdir(path):
        calls["n"] += 1
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", counting_listdir)
    second = resolve_effective_repo(str(home))
    assert _norm(second) == _norm(first)
    assert calls["n"] == 0


def _marionette_home_layout(tmp_path):
    """tmp/home/.marionette (non-git) + tmp/home/.marionette/marionette (git)."""
    home = tmp_path / "home" / ".marionette"
    home.mkdir(parents=True)
    (home / "notes.txt").write_text("not a repo", encoding="utf-8")
    child = home / "marionette"
    child.mkdir()
    _git_init(str(child))
    return home, child


def _brief_target_path(instruction: str) -> str:
    """Extract the path after 'Analyze the REAL codebase at ' (before trailing '.')."""
    marker = "Analyze the REAL codebase at "
    assert marker in instruction
    rest = instruction.split(marker, 1)[1]
    # Brief continues: "<path>. Emit evidenced..."
    return rest.split(". ", 1)[0].strip()


def test_analysis_instruction_uses_git_child_not_parent(tmp_path):
    """Swarm brief must name the clone, never the non-git Marionette Home parent."""
    from pmharness.bridge import _analysis_instruction

    home, child = _marionette_home_layout(tmp_path)
    child_resolved = resolve_effective_repo(str(home))
    assert _norm(child_resolved) == _norm(str(child))
    inst = _analysis_instruction("audit the peel", str(home), "explore")
    target = _brief_target_path(inst)
    assert _norm(target) == _norm(child_resolved)
    assert _norm(target) != _norm(str(home))


def test_parallel_analysis_brief_uses_git_child_not_parent(tmp_path):
    """Parallel/analysis workers call _analysis_instruction(via_tool=False); same pin."""
    from pmharness.bridge import _analysis_instruction

    home, child = _marionette_home_layout(tmp_path)
    child_resolved = resolve_effective_repo(str(home))
    assert _norm(child_resolved) == _norm(str(child))
    # Matches harness/worker.py analysis path (expects_diff=False).
    inst = _analysis_instruction(
        "review auth", str(home), "explore", via_tool=False,
    )
    target = _brief_target_path(inst)
    assert _norm(target) == _norm(child_resolved)
    assert _norm(target) != _norm(str(home))


def _capturing_swarm_harness(monkeypatch, bridge_mod, tmp_path):
    """Shared WorkerSpec/Orchestrator fakes; returns (captured list, cleanup noop)."""
    from dataclasses import dataclass, field
    from typing import Any

    captured: list = []

    @dataclass
    class _CapturingWorkerSpec:
        role: str
        instruction: str
        adapter: str
        payload: dict = field(default_factory=dict)

        def __post_init__(self) -> None:
            captured.append(self)

    class _FakeJob:
        id = "job_test"
        status = "complete"

    class _FakeResult:
        job = _FakeJob()
        status = "complete"
        mode = "inline"
        artifacts: list = []
        summary = "ok"

    class _FakeOrchestrator:
        def __init__(self, store: Any) -> None:
            self.store = store

        def run(self, goal: str, specs=None, worker_mode=None, label=None):
            return _FakeResult()

    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "agentic")
    monkeypatch.setattr("puppetmaster.workers.WorkerSpec", _CapturingWorkerSpec)
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(bridge_mod, "_warn_if_unindexed", lambda *_a, **_k: None)
    return captured


def test_execute_intent_resolves_cwd_for_swarm_brief(monkeypatch, tmp_path):
    """execute_intent belt-and-braces: unresolved parent cwd still pins brief + payload."""
    import pmharness.bridge as bridge
    from pmharness.intent import DriverIntent

    home, child = _marionette_home_layout(tmp_path)
    captured = _capturing_swarm_harness(monkeypatch, bridge, tmp_path)
    monkeypatch.delenv("HARNESS_REPO", raising=False)

    intent = DriverIntent(action="run_swarm", goal="audit auth", roles=["explore"])
    result = bridge.execute_intent(
        intent,
        state_dir=str(tmp_path / "state"),
        cwd=str(home),
    )
    assert result is not None
    assert captured
    child_resolved = resolve_effective_repo(str(home))
    assert _norm(child_resolved) == _norm(str(child))
    target = _brief_target_path(captured[0].instruction)
    assert _norm(target) == _norm(child_resolved)
    assert _norm(target) != _norm(str(home))
    assert _norm(captured[0].payload.get("cwd") or "") == _norm(child_resolved)


def test_execute_intent_resolves_harness_repo_env_for_swarm_brief(monkeypatch, tmp_path):
    """When only HARNESS_REPO is the parent (no cwd kwarg), brief still names the child."""
    import os

    import pmharness.bridge as bridge
    from pmharness.intent import DriverIntent

    home, child = _marionette_home_layout(tmp_path)
    captured = _capturing_swarm_harness(monkeypatch, bridge, tmp_path)
    monkeypatch.setenv("HARNESS_REPO", str(home))

    intent = DriverIntent(action="run_swarm", goal="audit auth", roles=["explore"])
    result = bridge.execute_intent(
        intent,
        state_dir=str(tmp_path / "state"),
    )
    assert result is not None
    assert captured
    child_resolved = resolve_effective_repo(str(home))
    assert _norm(child_resolved) == _norm(str(child))
    target = _brief_target_path(captured[0].instruction)
    assert _norm(target) == _norm(child_resolved)
    assert _norm(target) != _norm(str(home))
    assert _norm(captured[0].payload.get("cwd") or "") == _norm(child_resolved)
    # Env is restored after the call so a mid-session parent view pointer stays put.
    assert _norm(os.environ.get("HARNESS_REPO") or "") == _norm(str(home))
