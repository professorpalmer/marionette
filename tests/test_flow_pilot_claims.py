"""A pilot write in Marionette becomes a Puppetmaster flow claim while a flow walks."""
from __future__ import annotations

import subprocess
from pathlib import Path

from puppetmaster import flow
from puppetmaster.flow import NodeOutcome

from harness.checkpoint_hunks import record_agent_write

def _repo(tmp_path: Path) -> Path:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


GRAPH = {"id": "regions", "entry": "build", "defaults": {"adapter": "codex"},
         "nodes": [{"id": "build", "kind": "agent", "task": "build"}, {"id": "z", "kind": "end"}],
         "edges": [{"from": "build", "to": "z"}]}


def _walk(state: Path, repo: Path, during) -> str:
    run = flow.new_run(state, GRAPH, "goal", cwd=str(repo))

    def execute(node, current, prev):
        during()
        return NodeOutcome(ok=True, output="built")

    assert flow.walk(state, run.run_id, execute=execute).status == "done"
    return run.run_id


def test_a_pilot_write_during_a_walk_is_claimed(tmp_path: Path):
    repo = _repo(tmp_path)
    state = tmp_path / "state"

    def during():
        (repo / "preview_world.py").write_text("print(1)\n", encoding="utf-8")
        record_agent_write(str(repo), "preview_world.py", session_id="s1")
        record_agent_write(str(repo), str(repo / "renders" / "integrated.png"), session_id="s1")

    run_id = _walk(state, repo, during)
    assert flow.pilot_claims(state, run_id) == ["preview_world.py", "renders/integrated.png"]


def test_a_write_with_no_walk_claims_nothing(tmp_path: Path):
    repo = _repo(tmp_path)
    state = tmp_path / "state"
    run = flow.new_run(state, GRAPH, "goal", cwd=str(repo))
    record_agent_write(str(repo), "preview_world.py", session_id="s1")
    assert flow.pilot_claims(state, run.run_id) == []
