"""Tests for declarative pre/post checks (harness/declarative_checks.py)."""
import json
import os
import subprocess
import tempfile
import shutil

import pytest

from harness.declarative_checks import (
    CheckSpec,
    discover_check_parse_warnings,
    find_check_specs,
    load_checks,
    run_checks,
)
from harness.conversation import ConversationalSession
from harness.worker import ProviderWorker
from puppetmaster.models import Artifact, ArtifactType
from puppetmaster.store_factory import create_store


def _git_repo():
    repo_dir = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo_dir, capture_output=True)
    with open(os.path.join(repo_dir, "hello.txt"), "w", encoding="utf-8") as f:
        f.write("hello world\n")
    subprocess.run(["git", "add", "hello.txt"], cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, capture_output=True)
    return repo_dir


def test_load_valid_spec_round_trip(tmp_path):
    spec = {
        "version": 1,
        "pre": [
            {
                "id": "clean",
                "kind": "shell",
                "cmd": "python -c \"import sys; sys.exit(0)\"",
                "on_fail": "blocked",
            }
        ],
        "post": [
            {
                "id": "file-ok",
                "kind": "file",
                "path": "hello.txt",
                "contains": "hello",
                "on_fail": "failed",
            }
        ],
    }
    path = tmp_path / "checks.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    loaded = load_checks(str(path))
    assert len(loaded) == 2
    assert loaded[0].id == "clean"
    assert loaded[1].phase == "post"


def test_load_unknown_kind_rejected():
    with pytest.raises(ValueError, match="unknown kind"):
        load_checks({"pre": [{"id": "x", "kind": "yaml", "on_fail": "warn"}]})


def test_load_traversal_path_rejected():
    with pytest.raises(ValueError, match="traversal"):
        load_checks(
            {
                "pre": [
                    {
                        "id": "bad",
                        "kind": "file",
                        "path": "../secret",
                        "exists": True,
                        "on_fail": "warn",
                    }
                ]
            }
        )


def test_malformed_file_yields_warn_result(tmp_path):
    checks_dir = tmp_path / ".marionette" / "checks"
    checks_dir.mkdir(parents=True)
    (checks_dir / "broken.json").write_text("{not json", encoding="utf-8")
    warns = discover_check_parse_warnings(str(tmp_path))
    assert len(warns) == 1
    assert warns[0].on_fail == "warn"
    assert warns[0].passed is False


def test_run_shell_pass_fail_timeout(tmp_path):
    repo = str(tmp_path)
    ok_spec = CheckSpec(
        id="ok",
        kind="shell",
        phase="pre",
        on_fail="blocked",
        cmd="python -c \"import sys; sys.exit(0)\"",
    )
    fail_spec = CheckSpec(
        id="fail",
        kind="shell",
        phase="pre",
        on_fail="blocked",
        cmd="python -c \"import sys; sys.exit(1)\"",
    )
    timeout_spec = CheckSpec(
        id="slow",
        kind="shell",
        phase="pre",
        on_fail="warn",
        cmd="python -c \"import time; time.sleep(5)\"",
        timeout_s=1,
    )
    results = run_checks([ok_spec, fail_spec, timeout_spec], repo=repo, phase="pre")
    by_id = {r.id: r for r in results}
    assert by_id["ok"].passed is True
    assert by_id["fail"].passed is False
    assert by_id["slow"].passed is False
    assert "timed out" in by_id["slow"].output.lower()


def test_run_file_exists_contains(tmp_path):
    repo = str(tmp_path)
    target = tmp_path / "note.txt"
    target.write_text("alpha beta", encoding="utf-8")
    specs = [
        CheckSpec(
            id="exists",
            kind="file",
            phase="post",
            on_fail="failed",
            path="note.txt",
            exists=True,
        ),
        CheckSpec(
            id="has-alpha",
            kind="file",
            phase="post",
            on_fail="failed",
            path="note.txt",
            contains="alpha",
        ),
        CheckSpec(
            id="no-gamma",
            kind="file",
            phase="post",
            on_fail="failed",
            path="note.txt",
            not_contains="gamma",
        ),
        CheckSpec(
            id="missing-file",
            kind="file",
            phase="post",
            on_fail="failed",
            path="missing.txt",
            exists=True,
        ),
    ]
    results = run_checks(specs, repo=repo, phase="post")
    by_id = {r.id: r for r in results}
    assert by_id["exists"].passed is True
    assert by_id["has-alpha"].passed is True
    assert by_id["no-gamma"].passed is True
    assert by_id["missing-file"].passed is False


def test_find_check_specs_sorted(tmp_path):
    checks_dir = tmp_path / ".marionette" / "checks"
    checks_dir.mkdir(parents=True)
    (checks_dir / "b.json").write_text(
        json.dumps({"pre": [{"id": "b", "kind": "shell", "cmd": "python -c \"pass\"", "on_fail": "warn"}]}),
        encoding="utf-8",
    )
    (checks_dir / "a.json").write_text(
        json.dumps({"pre": [{"id": "a", "kind": "shell", "cmd": "python -c \"pass\"", "on_fail": "warn"}]}),
        encoding="utf-8",
    )
    specs = find_check_specs(str(tmp_path))
    assert [s.id for s in specs] == ["a", "b"]


def test_load_artifact_expect_round_trip():
    spec = {
        "post": [
            {
                "id": "patch-present",
                "kind": "artifact",
                "on_fail": "failed",
                "expect": {"type": "PATCH", "min_count": 1},
            }
        ]
    }
    loaded = load_checks(spec)
    assert len(loaded) == 1
    assert loaded[0].kind == "artifact"
    assert loaded[0].artifact_type == "PATCH"
    assert loaded[0].min_count == 1
    assert loaded[0].phase == "post"


def test_load_artifact_missing_expect_type_rejected():
    with pytest.raises(ValueError, match="expect.type"):
        load_checks(
            {
                "post": [
                    {
                        "id": "bad",
                        "kind": "artifact",
                        "on_fail": "failed",
                        "expect": {"min_count": 1},
                    }
                ]
            }
        )


def test_load_artifact_pre_phase_rejected():
    with pytest.raises(ValueError, match="post-only"):
        load_checks(
            {
                "pre": [
                    {
                        "id": "bad",
                        "kind": "artifact",
                        "on_fail": "warn",
                        "expect": {"type": "PATCH"},
                    }
                ]
            }
        )


def test_run_artifact_without_job_context():
    spec = CheckSpec(
        id="patch-present",
        kind="artifact",
        phase="post",
        on_fail="failed",
        artifact_type="PATCH",
        min_count=1,
    )
    results = run_checks([spec], repo="", phase="post")
    assert len(results) == 1
    assert results[0].passed is False
    assert "job context" in results[0].output


def test_run_artifact_with_store(tmp_path):
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    store = create_store("sqlite", state_dir)
    store.init()
    job = store.create_job("artifact check job")
    artifact = Artifact(
        job_id=job.id,
        task_id="task_1",
        type=ArtifactType.FINDING,
        created_by="worker-alpha",
        payload={"claim": "ok"},
        confidence=0.9,
        evidence=["tests/test_foo.py"],
    )
    store.save_artifact(artifact)

    pass_spec = CheckSpec(
        id="finding-present",
        kind="artifact",
        phase="post",
        on_fail="failed",
        artifact_type="FINDING",
        min_count=1,
    )
    fail_spec = CheckSpec(
        id="patch-missing",
        kind="artifact",
        phase="post",
        on_fail="failed",
        artifact_type="PATCH",
        min_count=1,
    )
    results = run_checks(
        [pass_spec, fail_spec],
        repo="",
        phase="post",
        state_dir=state_dir,
        job_id=job.id,
    )
    by_id = {r.id: r for r in results}
    assert by_id["finding-present"].passed is True
    assert by_id["patch-missing"].passed is False


def test_blocked_pre_check_prevents_worker_turn(monkeypatch):
    repo_dir = _git_repo()
    try:
        checks_dir = os.path.join(repo_dir, ".marionette", "checks")
        os.makedirs(checks_dir, exist_ok=True)
        spec = {
            "pre": [
                {
                    "id": "must-fail",
                    "kind": "shell",
                    "cmd": "python -c \"import sys; sys.exit(1)\"",
                    "on_fail": "blocked",
                }
            ]
        }
        with open(os.path.join(checks_dir, "block.json"), "w", encoding="utf-8") as f:
            json.dump(spec, f)

        called = {"run_auto": False}

        def mock_run_auto(self, objective, budget=None, require_codegraph=True):
            called["run_auto"] = True
            yield from ()

        monkeypatch.setattr(ConversationalSession, "run_auto", mock_run_auto)

        worker = ProviderWorker(repo=repo_dir, goal="do nothing")
        res = worker.run()
        assert res.ok is False
        assert called["run_auto"] is False
        assert "declarative pre-check blocked" in res.error
        assert any(c.get("id") == "must-fail" for c in res.declarative_checks)
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)
