"""Smoke + characterization tests for the ConversationJobsMixin extraction.

Guards the mechanical move of await/apply, provider-worker background, and
swarm-drain helpers out of harness.conversation into harness.conversation_jobs.
If the class-hierarchy wiring or the MRO ever regresses, these fail loudly.
"""

import json
import subprocess
import tempfile
from unittest.mock import patch

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.conversation_jobs import ConversationJobsMixin


MOVED_METHODS = (
    "_await_and_apply_job",
    "_run_provider_worker_background",
    "drain_swarm_results",
)


def test_session_inherits_mixin():
    assert issubclass(ConversationalSession, ConversationJobsMixin)
    assert ConversationJobsMixin in ConversationalSession.__mro__


def test_moved_methods_present_on_session():
    for name in MOVED_METHODS:
        assert hasattr(ConversationalSession, name), name
        attr = getattr(ConversationalSession, name)
        assert callable(attr), name


def test_moved_methods_resolve_to_mixin():
    for name in MOVED_METHODS:
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"ConversationJobsMixin.{name}", (
            name,
            attr.__qualname__,
        )


def test_mixin_defines_no_init():
    assert "__init__" not in ConversationJobsMixin.__dict__


def test_methods_defined_on_mixin_module():
    for name in MOVED_METHODS:
        assert name in ConversationJobsMixin.__dict__, name
        assert name not in ConversationalSession.__dict__, name


def test_local_jobs_not_folded_into_conversation_jobs():
    from harness.local_jobs import LocalJobsMixin

    for name in ("_register_local_job", "_finish_local_job", "live_local_jobs"):
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"LocalJobsMixin.{name}", (
            name,
            attr.__qualname__,
        )
        assert name not in ConversationJobsMixin.__dict__


def test_await_and_apply_job_still_on_session_surface(tmp_path):
    """Public surface unchanged: session._await_and_apply_job still applies patches."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)

    file_path = tmp_path / "hello.txt"
    file_path.write_text("Hello World\n")
    subprocess.run(["git", "add", "hello.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp_path, check=True)

    cfg = HarnessConfig()
    cfg.repo = str(tmp_path)
    session = ConversationalSession(cfg)
    assert session._tokens_used == 0

    mock_artifacts = [
        {
            "type": "patch",
            "payload": {
                "files": ["hello.txt"],
                "unified_diff": (
                    "diff --git a/hello.txt b/hello.txt\n"
                    "--- a/hello.txt\n"
                    "+++ b/hello.txt\n"
                    "@@ -1,1 +1,2 @@\n"
                    " Hello World\n"
                    "+Hello New World\n"
                ),
            },
            "tokens_in": 100,
            "tokens_out": 50,
        }
    ]

    original_run = subprocess.run

    def mock_subprocess_run(cmd, *args, **kwargs):
        is_await = isinstance(cmd, list) and any(arg == "await" for arg in cmd)
        is_artifacts = isinstance(cmd, list) and any(arg == "artifacts" for arg in cmd)
        if is_await:
            return subprocess.CompletedProcess(cmd, 0, stdout="Awaiting complete", stderr="")
        if is_artifacts:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(mock_artifacts), stderr=""
            )
        return original_run(cmd, *args, **kwargs)

    with patch("subprocess.run", side_effect=mock_subprocess_run):
        res = session._await_and_apply_job("job_mixin_char_001")

    assert res["job_id"] == "job_mixin_char_001"
    assert res["applied"] is True
    assert res["files"] == ["hello.txt"]
    assert res["tokens_in"] == 100
    assert res["tokens_out"] == 50
    assert "Applied patch" in res["summary"]
    assert res["error"] is None
    assert session._tokens_used == 150
    assert file_path.read_text() == "Hello World\nHello New World\n"


def test_drain_swarm_results_characterization():
    """Drain still yields swarm_result + pilot_resume and appends history."""
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)
    session._swarm_results.put({
        "job_id": "job_drain_char",
        "objective": "characterize drain",
        "result": {
            "applied": True,
            "files": ["a.py"],
            "summary": "ok",
        },
    })
    events = list(session.drain_swarm_results())
    kinds = [e.kind for e in events]
    assert "swarm_result" in kinds
    assert "pilot_resume" in kinds
    assert any(
        m["role"] == "assistant" and "[swarm result for: characterize drain]" in m["content"]
        for m in session._history
    )
    assert any(
        m["role"] == "user" and "[background job job_drain_char finished]" in m["content"]
        for m in session._history
    )


def test_run_provider_worker_background_finishes_local_job():
    """Provider-worker background path still finishes the local job + queues result."""
    from harness.worker import WorkerResult

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)
    job_id = "job_provider_bg_char"
    session._register_local_job(job_id, goal="noop analysis", role="implement", engine="native")

    fake = WorkerResult(
        ok=True,
        summary="analysis complete",
        patch="",
        files_changed=[],
        tokens_in=10,
        tokens_out=5,
        tokens_cached=0,
        engine="native",
        model="stub",
    )

    with patch.object(session, "_run_edit_worker_bounded", return_value=fake):
        session._run_provider_worker_background(job_id, "noop analysis")

    item = session._swarm_results.get_nowait()
    assert item["job_id"] == job_id
    assert item["result"]["applied"] is True
    assert item["result"]["error"] is None
    assert "analysis complete" in (item["result"]["summary"] or "")

    finished = session._local_jobs[job_id]
    assert finished["status"] == "completed"
    assert finished.get("model") == "native/stub"
