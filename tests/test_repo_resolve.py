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


def test_two_git_children_no_preferred_returns_root_unchanged(tmp_path):
    """foo + bar (no preferred basename) stays ambiguous — leave parent."""
    home = tmp_path / "home"
    home.mkdir()
    a = home / "foo"
    b = home / "bar"
    a.mkdir()
    b.mkdir()
    _git_init(str(a))
    _git_init(str(b))
    got = resolve_effective_repo(str(home))
    assert _norm(got) == _norm(str(home))


def test_marionette_and_wiki_prefers_marionette_child(tmp_path):
    """Home with marionette + wiki git children resolves to marionette."""
    home = tmp_path / "home" / ".marionette"
    home.mkdir(parents=True)
    marionette = home / "marionette"
    wiki = home / "wiki"
    marionette.mkdir()
    wiki.mkdir()
    _git_init(str(marionette))
    _git_init(str(wiki))
    got = resolve_effective_repo(str(home))
    assert _norm(got) == _norm(str(marionette))


def test_only_wiki_git_child_selected_as_single_child(tmp_path):
    """Exactly one git child still resolves, even when the name is not preferred."""
    home = tmp_path / "home" / ".marionette"
    home.mkdir(parents=True)
    wiki = home / "wiki"
    wiki.mkdir()
    _git_init(str(wiki))
    got = resolve_effective_repo(str(home))
    assert _norm(got) == _norm(str(wiki))


def test_wiki_only_among_multiple_non_preferred_leaves_parent(tmp_path):
    """Multiple git children, zero preferred names → leave parent unchanged."""
    home = tmp_path / "home" / ".marionette"
    home.mkdir(parents=True)
    wiki = home / "wiki"
    docs = home / "docs"
    wiki.mkdir()
    docs.mkdir()
    _git_init(str(wiki))
    _git_init(str(docs))
    got = resolve_effective_repo(str(home))
    assert _norm(got) == _norm(str(home))


def test_preferred_name_case_insensitive(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    marionette = home / "Marionette"
    wiki = home / "wiki"
    marionette.mkdir()
    wiki.mkdir()
    _git_init(str(marionette))
    _git_init(str(wiki))
    got = resolve_effective_repo(str(home))
    assert _norm(got) == _norm(str(marionette))


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


def _git_commit_ready(repo: str) -> None:
    """Configure identity + empty initial commit so git apply has a base."""
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    # Allow empty commit on newer git; fall back to a seed file if needed.
    seed = os.path.join(repo, ".keep")
    with open(seed, "w", encoding="utf-8") as f:
        f.write("keep\n")
    subprocess.run(
        ["git", "add", ".keep"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo, check=True, capture_output=True, text=True,
    )


def test_apply_worker_patch_resolves_home_parent_to_git_child(tmp_path):
    """Session rooted at Marionette Home must apply patches into the git child."""
    import tempfile

    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    home, child = _marionette_home_layout(tmp_path)
    _git_commit_ready(str(child))
    child_resolved = resolve_effective_repo(str(home))
    assert _norm(child_resolved) == _norm(str(child))

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = str(home)  # session parent = non-git Home
    session = ConversationalSession(cfg)

    artifacts = [
        {
            "type": "patch",
            "payload": {
                "files": ["home_apply.txt"],
                "unified_diff": (
                    "diff --git a/home_apply.txt b/home_apply.txt\n"
                    "new file mode 100644\n"
                    "--- /dev/null\n"
                    "+++ b/home_apply.txt\n"
                    "@@ -0,0 +1 @@\n"
                    "+applied into git child\n"
                ),
            },
        }
    ]
    applied, files, msg = session._apply_worker_patch(artifacts)
    assert applied is True
    assert files == ["home_apply.txt"]
    assert "applied" in msg.lower() or "already" in msg.lower()

    child_file = child / "home_apply.txt"
    parent_file = home / "home_apply.txt"
    assert child_file.is_file()
    assert child_file.read_text(encoding="utf-8") == "applied into git child\n"
    assert not parent_file.exists()
    # Per-operation resolve must never persist the child onto boot state.
    assert _norm(cfg.repo) == _norm(str(home))


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
