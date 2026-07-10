"""Tests for OMP-inspired internal URI read surfaces."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.internal_uri import (
    InternalUriContext,
    InternalUriError,
    is_internal_uri,
    parse_internal_uri,
    resolve_internal_uri,
    search_internal_uris,
)
from harness.pilot import PilotAction
from puppetmaster.models import AgentRun, Artifact, ArtifactType, Task
from puppetmaster.store_factory import create_store


def _seed_store(state_dir: str) -> dict[str, str]:
    store = create_store("sqlite", state_dir)
    store.init()
    job = store.create_job("Find routing regressions in harness")
    task = Task(job_id=job.id, role="conflict-auditor", instruction="scan")
    store.save_task(task)
    run = AgentRun(
        job_id=job.id,
        task_id=task.id,
        role="conflict-auditor",
        worker_id="worker-alpha",
    )
    store.save_run(run)
    artifact = Artifact(
        job_id=job.id,
        task_id=task.id,
        type=ArtifactType.FINDING,
        created_by=run.worker_id,
        payload={"claim": "Router drops rejected alternatives on Windows", "detail": "parity gap"},
        confidence=0.91,
        evidence=["store/router.py"],
    )
    store.save_artifact(artifact)
    return {
        "job_id": job.id,
        "task_id": task.id,
        "run_id": run.id,
        "artifact_id": artifact.id,
    }


def _git(repo: str, *args: str) -> None:
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True)


def _repo_with_merge_conflict() -> tuple[str, str]:
    repo = tempfile.mkdtemp(prefix="conflict-uri-")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "t@example.com")

    base_branch = subprocess.check_output(
        ["git", "-C", repo, "branch", "--show-current"],
        text=True,
    ).strip()

    conflict_path = os.path.join(repo, "src", "module.py")
    os.makedirs(os.path.dirname(conflict_path), exist_ok=True)
    with open(conflict_path, "w", encoding="utf-8") as fh:
        fh.write("base line\n")

    _git(repo, "add", "src/module.py")
    _git(repo, "commit", "-qm", "base")

    _git(repo, "checkout", "-q", "-b", "feature")
    with open(conflict_path, "w", encoding="utf-8") as fh:
        fh.write("feature line\n")
    _git(repo, "add", "src/module.py")
    _git(repo, "commit", "-qm", "feature edit")

    _git(repo, "checkout", "-q", base_branch)
    with open(conflict_path, "w", encoding="utf-8") as fh:
        fh.write("main line\n")
    _git(repo, "add", "src/module.py")
    _git(repo, "commit", "-qm", "main edit")

    merge = subprocess.run(
        ["git", "-C", repo, "merge", "feature"],
        capture_output=True,
        text=True,
    )
    assert merge.returncode != 0
    return repo, "src/module.py"


class TestInternalUriParsing:
    def test_is_internal_uri_recognizes_supported_schemes(self):
        assert is_internal_uri("job://")
        assert is_internal_uri("artifact://job_x/artifact_y")
        assert is_internal_uri("agent://job_x/run_y/role")
        assert is_internal_uri("conflict://src/foo.py")
        assert not is_internal_uri("file://etc/passwd")
        assert not is_internal_uri("/abs/path.txt")

    def test_parse_rejects_traversal(self):
        with pytest.raises(InternalUriError, match="traversal"):
            parse_internal_uri("job://job_ok/../secret")

    def test_parse_rejects_backslashes(self):
        with pytest.raises(InternalUriError, match="backslashes"):
            parse_internal_uri(r"job://job_ok\tasks\task_1")

    @pytest.mark.parametrize(
        "uri",
        [
            r"artifact://job_ok\..\etc\passwd",
            r"conflict://src\..\..\windows\system32",
            r"agent://job_ok\runs\run_1",
        ],
    )
    def test_parse_rejects_windows_backslash_paths(self, uri):
        with pytest.raises(InternalUriError, match="backslashes"):
            parse_internal_uri(uri)

    def test_parse_line_selector(self):
        parsed = parse_internal_uri("job://job_ok:10-20")
        assert parsed.path == "job_ok"
        assert parsed.start_line == 10
        assert parsed.end_line == 20


class TestInternalUriResolution:
    @pytest.fixture()
    def seeded(self):
        state_dir = tempfile.mkdtemp(prefix="internal-uri-state-")
        ids = _seed_store(state_dir)
        ctx = InternalUriContext(state_dir=state_dir, repo=None)
        return state_dir, ids, ctx

    def test_job_list_and_detail(self, seeded):
        _, ids, ctx = seeded
        listing = resolve_internal_uri("job://", ctx)
        assert listing.is_directory
        assert ids["job_id"] in listing.content

        detail = resolve_internal_uri(f"job://{ids['job_id']}", ctx)
        assert ids["job_id"] in detail.content
        assert "routing regressions" in detail.content

    def test_job_artifact_index_read(self, seeded):
        _, ids, ctx = seeded
        index = resolve_internal_uri(f"job://{ids['job_id']}/artifacts", ctx)
        assert index.is_directory
        assert ids["artifact_id"] in index.content

    def test_artifact_read_and_field(self, seeded):
        _, ids, ctx = seeded
        full = resolve_internal_uri(
            f"artifact://{ids['job_id']}/{ids['artifact_id']}", ctx
        )
        payload = json.loads(full.content)
        assert payload["payload"]["claim"].startswith("Router drops")

        field = resolve_internal_uri(
            f"artifact://{ids['job_id']}/{ids['artifact_id']}/payload/claim", ctx
        )
        assert "Router drops rejected alternatives" in field.content

    def test_agent_field_read(self, seeded):
        _, ids, ctx = seeded
        role = resolve_internal_uri(
            f"agent://{ids['job_id']}/{ids['run_id']}/role", ctx
        )
        assert role.content.strip() == "conflict-auditor"

        worker = resolve_internal_uri(
            f"agent://{ids['job_id']}/{ids['run_id']}/worker_id", ctx
        )
        assert worker.content.strip() == "worker-alpha"

    def test_search_finds_job_and_artifact(self, seeded):
        _, ids, ctx = seeded
        job_hits = search_internal_uris("routing regressions", ctx, scheme="job")
        assert f"job://{ids['job_id']}" in job_hits

        art_hits = search_internal_uris("Router drops", ctx, scheme="artifact")
        assert f"artifact://{ids['job_id']}/{ids['artifact_id']}" in art_hits


class TestConflictUri:
    @pytest.fixture()
    def conflict_repo(self):
        repo, rel = _repo_with_merge_conflict()
        try:
            yield repo, rel
        finally:
            import shutil
            shutil.rmtree(repo, ignore_errors=True)

    def test_conflict_listing(self, conflict_repo):
        repo, rel = conflict_repo
        ctx = InternalUriContext(state_dir="", repo=repo)
        listing = resolve_internal_uri("conflict://", ctx)
        assert listing.is_directory
        assert rel.replace("\\", "/") in listing.content

    def test_conflict_detail_and_dry_run(self, conflict_repo):
        repo, rel = conflict_repo
        ctx = InternalUriContext(state_dir="", repo=repo)

        detail = resolve_internal_uri(f"conflict://{rel}", ctx)
        body = json.loads(detail.content)
        assert body["unmerged"] is True
        assert body["regions"]

        dry_ours = resolve_internal_uri(
            f"conflict://resolve/{rel}?strategy=ours", ctx
        )
        assert "dry-run only" in " ".join(dry_ours.notes)
        assert "main line" in dry_ours.content
        assert "feature line" not in dry_ours.content

        dry_theirs = resolve_internal_uri(
            f"conflict://resolve/{rel}?strategy=theirs", ctx
        )
        assert "feature line" in dry_theirs.content
        assert "main line" not in dry_theirs.content

        # Dry-run must not modify the working tree.
        with open(os.path.join(repo, rel), "r", encoding="utf-8") as fh:
            on_disk = fh.read()
        assert "<<<<<<<" in on_disk


class TestToolDispatchIntegration:
    def test_read_file_resolves_internal_uri(self):
        state_dir = tempfile.mkdtemp(prefix="internal-uri-tool-")
        ids = _seed_store(state_dir)
        cfg = HarnessConfig(state_dir=state_dir, repo="")
        session = ConversationalSession(cfg)

        act = PilotAction(
            kind="read_file",
            path=f"artifact://{ids['job_id']}/{ids['artifact_id']}/payload/claim",
        )
        ok, status, val = session._do_read_file(act)
        assert ok
        assert status == "success"
        assert "Router drops rejected alternatives" in val

    def test_list_dir_on_internal_uri_directory(self):
        state_dir = tempfile.mkdtemp(prefix="internal-uri-tool-")
        ids = _seed_store(state_dir)
        cfg = HarnessConfig(state_dir=state_dir, repo="")
        session = ConversationalSession(cfg)

        act = PilotAction(kind="list_dir", path="job://")
        ok, status, val = session._do_list_dir(act)
        assert ok
        assert status == "success"
        assert ids["job_id"] in val

    def test_read_file_on_internal_uri_directory_lists(self):
        """read_file on an internal URI directory redirects to a listing (ok=True)."""
        state_dir = tempfile.mkdtemp(prefix="internal-uri-tool-")
        ids = _seed_store(state_dir)
        cfg = HarnessConfig(state_dir=state_dir, repo="")
        session = ConversationalSession(cfg)

        act = PilotAction(kind="read_file", path="job://")
        ok, status, val = session._do_read_file(act)
        assert ok is True
        assert status == "success"
        assert "path is a directory" in val
        assert "use list_dir next time" in val
        assert ids["job_id"] in val
        assert "Path is a directory:" not in val

    def test_read_file_still_rejects_filesystem_traversal(self):
        with tempfile.TemporaryDirectory() as repo:
            cfg = HarnessConfig(state_dir=tempfile.mkdtemp(), repo=repo)
            session = ConversationalSession(cfg)
            act = PilotAction(kind="read_file", path="../../etc/passwd")
            ok, status, _ = session._do_read_file(act)
            assert not ok
            assert status == "path_traversal"

    def test_search_state_dispatch_returns_job_hit(self):
        state_dir = tempfile.mkdtemp(prefix="internal-uri-tool-")
        _seed_store(state_dir)
        cfg = HarnessConfig(state_dir=state_dir, repo="")
        session = ConversationalSession(cfg)

        act = PilotAction(kind="search_state", query="routing regressions")
        ok, status, val = session._do_search_state(act)
        assert ok
        assert status == "success"
        assert "job://" in val
        assert "routing regressions" in val.lower()
