"""Real read-only analysis path: product default is agentic; demo is opt-in only.

A target repo is never mutated. The live model call is exercised separately.
"""
import pytest
pytestmark = pytest.mark.swarm
import hashlib
import tempfile

from pmharness.intent import DriverIntent
from pmharness import bridge


def test_product_refuses_demo_without_allow(monkeypatch):
    monkeypatch.delenv("HARNESS_SWARM_ADAPTER", raising=False)
    monkeypatch.delenv("HARNESS_REPO", raising=False)
    monkeypatch.delenv("HARNESS_ALLOW_DEMO_SWARM", raising=False)
    with pytest.raises(ValueError, match="refusing demo substrate"):
        bridge.execute_intent(
            DriverIntent(action="run_swarm", goal="x", rationale="y"),
            state_dir=tempfile.mkdtemp(),
        )


def test_eval_demo_allowed_with_flag(monkeypatch):
    monkeypatch.delenv("HARNESS_SWARM_ADAPTER", raising=False)
    monkeypatch.delenv("HARNESS_REPO", raising=False)
    monkeypatch.setenv("HARNESS_ALLOW_DEMO_SWARM", "1")
    res = bridge.execute_intent(
        DriverIntent(action="run_swarm", goal="x", rationale="y"),
        state_dir=tempfile.mkdtemp(),
    )
    assert res.adapter == "demo"


def test_openai_path_requires_repo(monkeypatch):
    # openai set but no repo -> cannot analyze; product refuses demo fallback
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "openai")
    monkeypatch.delenv("HARNESS_REPO", raising=False)
    monkeypatch.delenv("HARNESS_ALLOW_DEMO_SWARM", raising=False)
    with pytest.raises(ValueError, match="refusing demo substrate"):
        bridge.execute_intent(
            DriverIntent(action="run_swarm", goal="x", rationale="y"),
            state_dir=tempfile.mkdtemp(),
        )


def test_analysis_specs_are_read_only():
    from puppetmaster.workers import WorkerSpec, spec_edits_files, spec_explicitly_no_edit
    spec = WorkerSpec(role="explore", instruction="analyze", adapter="openai",
                      payload={"read_only": True, "no_edit": True, "dry_run": True,
                               "cwd": "/tmp"})
    assert spec_explicitly_no_edit(spec) is True
    assert spec_edits_files(spec) is False


def test_analysis_does_not_mutate_target_repo(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def foo():\n    return 1\n")
    (repo / "b.py").write_text("X = 2\n")
    before = {p.name: hashlib.md5(p.read_bytes()).hexdigest()
              for p in repo.glob("*.py")}
    # Eval demo path with allow flag -- still proves the bridge never writes.
    monkeypatch.setenv("HARNESS_ALLOW_DEMO_SWARM", "1")
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "demo")
    monkeypatch.setenv("HARNESS_REPO", str(repo))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    bridge.execute_intent(DriverIntent(action="run_swarm", goal="audit", rationale="x"),
                          state_dir=tempfile.mkdtemp())
    after = {p.name: hashlib.md5(p.read_bytes()).hexdigest()
             for p in repo.glob("*.py")}
    assert before == after, "analysis must NEVER mutate the target repo"
    assert set(before) == set(after), "analysis must not add/remove files"
