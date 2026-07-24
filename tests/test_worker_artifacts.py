from __future__ import annotations

"""Regression: ProviderWorker must NOT sweep build artifacts (*.pyc, __pycache__,
.pytest_cache, etc.) into its patch. Found by the multi-file refactor stress test
where git add -A swept worker-created .pyc files into files_changed."""
import os
import subprocess
import tempfile

from harness.worker import ProviderWorker
from harness.autobudget import AutoBudget


def _git(d, *args):
    return subprocess.run(["git", "-C", d, *args], capture_output=True, text=True)


def test_worker_patch_excludes_build_artifacts(monkeypatch):
    repo = tempfile.mkdtemp(prefix="artifact-")
    _git(repo, "init", "-q")
    with open(os.path.join(repo, "mod.py"), "w") as f:
        f.write("def f():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")

    # Simulate the worker session writing a real edit AND a build artifact landing
    # in the worktree (as pytest would create when the worker runs tests).
    def fake_run_auto(self, objective, budget=None, *, require_codegraph=True, **kwargs):
        wt = self.config.repo
        with open(os.path.join(wt, "mod.py"), "w") as fh:
            fh.write("def f():\n    return 2\n")
        pdir = os.path.join(wt, "__pycache__")
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "mod.cpython-39.pyc"), "wb") as fh:
            fh.write(b"\x00\x01artifact")
        with open(os.path.join(wt, "extra.pyc"), "wb") as fh:
            fh.write(b"\x00\x01artifact")
        return iter(())

    monkeypatch.setattr("harness.conversation.ConversationalSession.run_auto", fake_run_auto)

    b = AutoBudget(max_tokens=1000, max_seconds=30, max_swarms=2)
    w = ProviderWorker(repo=repo, goal="bump return value", driver="stub-oracle",
                       reach="openrouter", budget=b, require_codegraph=False)
    res = w.run()

    assert res.ok, res.error
    # the real edit is present
    assert any(p.endswith("mod.py") for p in res.files_changed), res.files_changed
    # NO build artifacts leaked into the patch
    assert not any(".pyc" in p or "__pycache__" in p for p in res.files_changed), \
        f"build artifacts leaked: {res.files_changed}"
    assert "__pycache__" not in res.patch and ".pyc" not in res.patch
