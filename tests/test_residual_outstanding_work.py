"""The compaction residual names live work of this session, and only live work."""
from __future__ import annotations

import json

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.outstanding_work import OUTSTANDING_HEADING, outstanding_work_block

REQUEST = "Run the full test suite in the background and fix what fails."


def _tool_round(history, i):
    call_id = "call_%d" % i
    history.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "type": "function", "function": {
            "name": "run_command", "arguments": json.dumps({"command": "grep -n x src/m%d.py" % i})}}],
    })
    history.append({"role": "tool", "tool_call_id": call_id, "content": ("line %d " % i) * 400})


def _session(tmp_path, monkeypatch, name="s"):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "catalog")
    root = tmp_path / name
    root.mkdir()
    cfg = HarnessConfig(max_context_tokens=1_000_000, state_dir=str(root))
    cfg.repo = str(root)
    s = ConversationalSession(cfg)
    s.harness_session_id = "sess-outstanding"
    s._history.append({"role": "user", "content": REQUEST, "_user_request": True})
    return s


def _command(s, job_id, command, status="running", started_at=None):
    row = s._register_command_job(job_id, command=command, action_id="act-" + job_id)
    with s._local_jobs_lock:
        s._local_jobs[job_id]["status"] = status
        if started_at is not None:
            s._local_jobs[job_id]["started_at"] = started_at
    return row


def _compact(s):
    for i in range(30):
        _tool_round(s._history, i)
    list(s._maybe_compact_history(force=True))
    residual = s._history[1]
    assert residual.get("_compressed_summary") is True
    return residual["content"]


def test_running_background_command_is_named(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    _command(s, "local-cmd-live", "pytest -q tests OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456")
    content = _compact(s)
    assert OUTSTANDING_HEADING in content
    section = content.split(OUTSTANDING_HEADING, 1)[1]
    assert "background command local-cmd-live (running)" in section
    assert "pytest -q tests" in section
    assert "started " in section
    # The label is secret-free.
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in content


def test_terminal_and_foreign_jobs_are_not_named(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    for status in ("completed", "failed", "cancelled", "timeout"):
        _command(s, "local-cmd-" + status, "make " + status, status=status)
    _command(s, "local-cmd-other", "make other")
    with s._local_jobs_lock:
        s._local_jobs["local-cmd-other"]["session_id"] = "another-session"
    content = _compact(s)
    assert OUTSTANDING_HEADING not in content
    assert outstanding_work_block(s) == ""


def test_residual_is_unchanged_without_live_work(tmp_path, monkeypatch):
    bare = _session(tmp_path, monkeypatch, "bare")
    done = _session(tmp_path, monkeypatch, "done")
    _command(done, "local-cmd-done", "make all", status="completed")
    assert _compact(done) == _compact(bare)


def test_live_worker_and_flow_are_named_newest_first(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    _command(s, "local-cmd-old", "make old", started_at=1000.0)
    s._register_local_job("local-impl-1", "Refactor the exporter", role="implement")
    s._flow_runs = {"flow-run-1": {"objective": "Port four modules", "status": "running"},
                    "flow-run-2": {"objective": "Finished", "status": "done"}}
    block = outstanding_work_block(s)
    lines = block.splitlines()[2:]
    assert lines[0].startswith("- implement job local-impl-1 (running): Refactor the exporter")
    assert lines[1].startswith("- background command local-cmd-old")
    assert lines[2] == "- flow run flow-run-1 (running): Port four modules"
    assert "flow-run-2" not in block


def test_cap_holds_with_many_jobs(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    for i in range(30):
        _command(s, "local-cmd-%02d" % i, "python long_job.py --step %d " % i + "x" * 300,
                 started_at=2000.0 + i)
    block = outstanding_work_block(s)
    assert len(block) <= 1500
    items = [line for line in block.splitlines() if line.startswith("- background command")]
    assert 0 < len(items) <= 8
    # Newest first.
    assert "local-cmd-29" in items[0]
    assert block.splitlines()[-1].startswith("- and ")
    assert OUTSTANDING_HEADING in _compact(s)


class _BrokenLock:
    def __enter__(self):
        raise OSError("store read failed")

    def __exit__(self, *exc):
        return False


def test_store_read_error_omits_section(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    _command(s, "local-cmd-live", "make live")
    good_lock = s._local_jobs_lock
    s._local_jobs_lock = _BrokenLock()
    try:
        assert outstanding_work_block(s) == ""
    finally:
        s._local_jobs_lock = good_lock
    import harness.outstanding_work as ow

    def _boom(*_a, **_k):
        raise OSError("store read failed")

    monkeypatch.setattr(ow, "_job_items", _boom)
    content = _compact(s)
    assert OUTSTANDING_HEADING not in content
    assert REQUEST in content
