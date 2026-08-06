"""Bounded peek_history / peek_artifact pilot tools."""
from __future__ import annotations

import tempfile

from harness.config import HarnessConfig
from harness.pilot import PilotAction, from_wire
from harness.sessions import save_transcript


def _session(tmp_path, monkeypatch, state_dir: str | None = None):
    monkeypatch.setattr(
        "harness.conversation.RuleStore",
        lambda *a, **k: __import__("harness.rule_store", fromlist=["RuleStore"]).RuleStore(
            path=str(tmp_path / "rules.json")
        ),
    )
    monkeypatch.setattr("harness.memory_store.MEMORY_PATH", tmp_path / "mem.json")
    sd = state_dir or tempfile.mkdtemp()
    from harness.conversation import ConversationalSession

    session = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", state_dir=sd))
    return session, sd


def test_peek_history_reads_durable_transcript_after_compact(tmp_path, monkeypatch):
    session, state_dir = _session(tmp_path, monkeypatch)
    session.harness_session_id = "sess_peek"
    sid = session.harness_session_id
    # Persist a durable transcript larger than the live residual.
    history = [
        {"role": "user", "content": f"turn-{i} detail about topic {i}"}
        for i in range(12)
    ]
    save_transcript(state_dir, sid, {"history": history, "display": [], "job_ids": []})
    # Live residual looks compacted / short.
    session._history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "summary only"},
    ]
    ok, status, text = session._do_peek_history(
        PilotAction(kind="peek_history", arguments={"offset": 0, "limit": 5})
    )
    assert ok and status == "success"
    assert "compaction_generation=" in text
    assert "turn-0" in text
    assert "turn-4" in text
    assert "turn-11" not in text  # beyond limit


def test_peek_history_role_filter_and_generation_stale(tmp_path, monkeypatch):
    session, state_dir = _session(tmp_path, monkeypatch)
    session.harness_session_id = "sess_peek_role"
    sid = session.harness_session_id
    save_transcript(
        state_dir,
        sid,
        {
            "history": [
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"},
            ],
            "display": [],
            "job_ids": [],
        },
    )
    ok, status, text = session._do_peek_history(
        PilotAction(kind="peek_history", arguments={"role": "assistant", "limit": 10})
    )
    assert ok
    assert "a1" in text
    assert "u1" not in text

    ok, status, err = session._do_peek_history(
        PilotAction(
            kind="peek_history",
            arguments={"expected_generation": 999},
        )
    )
    assert not ok
    assert status == "stale_generation"
    assert "stale" in err


def test_peek_history_hard_caps_messages(tmp_path, monkeypatch):
    session, state_dir = _session(tmp_path, monkeypatch)
    session.harness_session_id = "sess_peek_cap"
    sid = session.harness_session_id
    history = [{"role": "user", "content": f"m{i}"} for i in range(40)]
    save_transcript(state_dir, sid, {"history": history, "display": [], "job_ids": []})
    ok, status, text = session._do_peek_history(
        PilotAction(kind="peek_history", arguments={"limit": 100})
    )
    assert ok
    # Hard max 20 messages.
    assert "returned=20" in text


def test_peek_artifact_by_uri_and_ids(tmp_path, monkeypatch):
    session, state_dir = _session(tmp_path, monkeypatch)
    job_id = "local-peek1"
    art = {
        "type": "finding",
        "id": f"{job_id}-finding-0",
        "headline": "short headline",
        "body": "BODY-" + ("x" * 200),
    }
    session._register_local_job(job_id, goal="peek", role="explore", engine="native")
    session._finish_local_job(
        job_id, ok=True, summary="done", status="done", engine="native", findings=[art],
    )
    uri = f"artifact://{job_id}/{job_id}-finding-0"
    ok, status, text = session._do_peek_artifact(
        PilotAction(kind="peek_artifact", path=uri, arguments={"uri": uri, "max_bytes": 512})
    )
    assert ok and status == "success"
    assert "uri=" in text
    assert "short headline" in text or "BODY-" in text

    ok2, status2, text2 = session._do_peek_artifact(
        PilotAction(
            kind="peek_artifact",
            arguments={"job_id": job_id, "artifact_id": f"{job_id}-finding-0", "max_bytes": 256},
        )
    )
    assert ok2 and status2 == "success"
    assert "truncated=true" in text2 or len(text2.encode("utf-8")) <= 256 + 200


def test_peek_artifact_wire_and_validate():
    act = from_wire("peek_artifact", {"uri": "artifact://job/a1"})
    assert act.kind == "peek_artifact"
    assert act.path == "artifact://job/a1" or (act.arguments or {}).get("uri")

    try:
        from_wire("peek_artifact", {})
        assert False, "expected PilotError"
    except Exception as exc:
        assert "peek_artifact" in str(exc)
