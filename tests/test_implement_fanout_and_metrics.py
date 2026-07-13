from __future__ import annotations

"""Fan-out guard + worktree seed + local-job routing previews."""

import tempfile

from harness.implement_guards import (
    check_implement_workspace,
    check_oversized_single_file_rewrite,
    extract_goal_paths,
    is_home_or_ephemeral_workspace,
    is_preflight_worker_error,
    looks_like_analysis_only_goal,
    max_single_file_rewrite_lines,
)
from harness.job_scoping import filter_local_jobs
from harness.local_job_routing import preview_agentic_route
from harness.worktree_seed import seed_worktree_from_goal


def test_extract_goal_paths_finds_rel_and_basename():
    paths = extract_goal_paths(
        "REWRITE the file addons/kotoba/translator.py and also helper.lua"
    )
    assert "addons/kotoba/translator.py" in paths
    assert "helper.lua" in paths


def test_oversized_rewrite_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_IMPLEMENT_MAX_FILE_LINES", "50")
    monkeypatch.delenv("HARNESS_IMPLEMENT_FANOUT_GUARD", raising=False)
    big = tmp_path / "huge.py"
    big.write_text("\n".join(f"line {i}" for i in range(200)), encoding="utf-8")
    msg = check_oversized_single_file_rewrite(
        f"REWRITE the file {big.name} completely from scratch",
        str(tmp_path),
    )
    assert msg is not None
    assert "REFUSED" in msg
    assert "huge.py" in msg


def test_sectioned_rewrite_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_IMPLEMENT_MAX_FILE_LINES", "50")
    big = tmp_path / "huge.py"
    big.write_text("\n".join(f"line {i}" for i in range(200)), encoding="utf-8")
    msg = check_oversized_single_file_rewrite(
        f"REWRITE lines 1-50 of {big.name}",
        str(tmp_path),
    )
    assert msg is None


def test_small_rewrite_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_IMPLEMENT_MAX_FILE_LINES", "250")
    small = tmp_path / "tiny.py"
    small.write_text("print(1)\n", encoding="utf-8")
    msg = check_oversized_single_file_rewrite(
        f"REWRITE the file {small.name}",
        str(tmp_path),
    )
    assert msg is None


def test_fanout_guard_disable(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_IMPLEMENT_FANOUT_GUARD", "0")
    monkeypatch.setenv("HARNESS_IMPLEMENT_MAX_FILE_LINES", "10")
    big = tmp_path / "huge.py"
    big.write_text("\n".join(f"x{i}" for i in range(100)), encoding="utf-8")
    assert check_oversized_single_file_rewrite(
        f"REWRITE the file {big.name}", str(tmp_path),
    ) is None


def test_check_implement_workspace_refuses_non_git(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_IMPLEMENT_GIT_GUARD", raising=False)
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    msg = check_implement_workspace(str(bare), goal="edit foo.py")
    assert msg is not None
    assert "REFUSED" in msg
    assert "not a git repository" in msg.lower()


def test_check_implement_workspace_allows_git(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.delenv("HARNESS_IMPLEMENT_GIT_GUARD", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    assert check_implement_workspace(str(repo), goal="edit foo.py") is None


def test_check_implement_workspace_refuses_home(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_IMPLEMENT_GIT_GUARD", raising=False)
    home = tmp_path / "pmharness" / "home"
    home.mkdir(parents=True)
    # Norm path contains /pmharness/home so is_home_or_ephemeral_workspace trips.
    assert is_home_or_ephemeral_workspace(str(home))
    msg = check_implement_workspace(
        str(home),
        goal="compare Ashita and Windower directory trees",
    )
    assert msg is not None
    assert "Marionette Home" in msg
    assert "analysis-only" in msg.lower() or "run_command" in msg


def test_check_implement_workspace_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_IMPLEMENT_GIT_GUARD", "0")
    bare = tmp_path / "bare"
    bare.mkdir()
    assert check_implement_workspace(str(bare)) is None


def test_looks_like_analysis_only_goal():
    assert looks_like_analysis_only_goal(
        "compare Ashita and Windower and report which files differ"
    )
    assert not looks_like_analysis_only_goal(
        "compare then fix kotoba.lua in both trees"
    )


def test_is_preflight_worker_error():
    assert is_preflight_worker_error("not a git repo: C:\\Users\\x\\.pmharness\\home")
    assert is_preflight_worker_error("REFUSED: workspace is Marionette Home")
    assert not is_preflight_worker_error("no changes produced")


def test_drain_preflight_failure_soft_resume():
    """Preflight errors must not say 'did NOT land a patch'."""
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s._swarm_results.put({
        "job_id": "local-preflight",
        "objective": "compare trees",
        "result": {
            "applied": False,
            "files": [],
            "summary": "not a git repo",
            "error": "not a git repo: C:/tmp/home",
            "has_patch_art": False,
        },
    })
    list(s.drain_swarm_results())
    resume = [
        m for m in s._history
        if m["role"] == "user" and "FAILED" in m["content"]
    ]
    assert resume
    text = resume[0]["content"]
    assert "before work started" in text or "preflight" in text.lower()
    assert "did NOT land a patch" not in text
    assert "no patch was attempted" in text.lower()


def test_seed_untracked_into_worktree(tmp_path):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    target = repo / "addons" / "kotoba" / "translator.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('live')\n", encoding="utf-8")
    # Worktree has neither the dir nor the file (HEAD checkout miss).
    seeded = seed_worktree_from_goal(
        str(repo), str(wt), "REWRITE the file addons/kotoba/translator.py",
    )
    assert "addons/kotoba/translator.py" in seeded
    assert (wt / "addons" / "kotoba" / "translator.py").read_text(encoding="utf-8") == "print('live')\n"


def test_seed_dynamic_from_dirty_token_match(tmp_path):
    """Vague goals still seed when live dirty paths match a significant token."""
    import subprocess
    from harness.worktree_seed import goal_match_tokens

    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True,
    )
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True,
    )
    target = repo / "addons" / "kotoba" / "ad.html"
    target.parent.mkdir(parents=True)
    target.write_text("<html>ad</html>\n", encoding="utf-8")
    # Unrelated untracked file must not seed on a kotoba-only goal.
    noise = repo / "other" / "noise.py"
    noise.parent.mkdir(parents=True)
    noise.write_text("print(1)\n", encoding="utf-8")

    assert "kotoba" in goal_match_tokens("fix the kotoba thrift ad")
    seeded = seed_worktree_from_goal(
        str(repo), str(wt), "fix the kotoba thrift ad",
    )
    assert "addons/kotoba/ad.html" in seeded
    assert (wt / "addons" / "kotoba" / "ad.html").read_text(encoding="utf-8") == (
        "<html>ad</html>\n"
    )
    assert "other/noise.py" not in seeded


def test_seed_html_path_token(tmp_path):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    target = repo / "page.html"
    target.write_text("<p>hi</p>\n", encoding="utf-8")
    seeded = seed_worktree_from_goal(str(repo), str(wt), "edit page.html")
    assert "page.html" in seeded


def test_filter_local_running_visible_on_session_drift(tmp_path):
    # Session stamp drifted but cwd is under the open workspace -- still show.
    rows = [
        {
            "id": "local-run",
            "status": "running",
            "session_id": "old-sess",
            "cwd": str(tmp_path / "proj"),
        },
        {
            "id": "local-done",
            "status": "completed",
            "session_id": "old-sess",
            "cwd": str(tmp_path / "proj"),
        },
    ]
    (tmp_path / "proj").mkdir()
    visible = filter_local_jobs(
        rows, active_session_id="new-sess", repo_root=str(tmp_path / "proj"),
    )
    assert [j["id"] for j in visible] == ["local-run"]


def test_preview_agentic_route_empty_on_bad_goal():
    assert preview_agentic_route("") == {}


def test_register_local_job_stamps_routing(monkeypatch):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    monkeypatch.setattr(
        "harness.local_job_routing.preview_agentic_route",
        lambda goal, role="implement": {
            "model_id": "z-ai/glm-4.9",
            "est_cost_usd": 0.0123,
            "artifact": {
                "type": "ROUTING",
                "headline": "Routed to z-ai/glm-4.9",
                "created_by": "router",
                "model": "z-ai/glm-4.9",
                "est_cost_usd": 0.0123,
                "role": role,
                "rejected": [],
                "detail": "test",
            },
        },
    )
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s._register_local_job(
        "local-route", "edit foo.py", role="implement",
        engine="agentic", model="",
    )
    job = s._local_jobs["local-route"]
    assert job["model"] == "agentic/z-ai/glm-4.9"
    assert abs(job["est_cost_usd"] - 0.0123) < 1e-9
    arts = job["artifacts"]
    assert len(arts) == 1
    assert arts[0]["type"] == "ROUTING"
    assert arts[0]["model"] == "z-ai/glm-4.9"


def test_finish_preserves_routing_artifact(monkeypatch):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    monkeypatch.setattr(
        "harness.local_job_routing.preview_agentic_route",
        lambda goal, role="implement": {
            "model_id": "z-ai/glm-4.9",
            "est_cost_usd": 0.01,
            "artifact": {
                "type": "ROUTING",
                "headline": "Routed to z-ai/glm-4.9",
                "created_by": "router",
                "model": "z-ai/glm-4.9",
                "est_cost_usd": 0.01,
                "role": role,
                "rejected": [],
                "detail": "test",
            },
        },
    )
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s._register_local_job(
        "local-fin-r", "edit", role="implement", engine="agentic", model="",
    )
    s._finish_local_job(
        "local-fin-r", ok=True, summary="done", files=["a.py"],
        tokens=100, engine="agentic", model="z-ai/glm-4.9",
        est_cost_usd=0.05,
    )
    job = s._local_jobs["local-fin-r"]
    types = [a.get("type") for a in job["artifacts"]]
    assert "ROUTING" in types
    assert "patch" in types
    routing = next(a for a in job["artifacts"] if a["type"] == "ROUTING")
    assert abs(routing["est_cost_usd"] - 0.05) < 1e-9
    assert max_single_file_rewrite_lines() >= 50
