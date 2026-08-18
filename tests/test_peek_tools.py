"""Bounded peek_history / peek_artifact pilot tools."""
from __future__ import annotations

import tempfile

from harness.compaction_archive import compaction_archive_path
from harness.config import HarnessConfig
from harness.pilot import PilotAction, from_wire
from harness.sessions import load_transcript, save_transcript

_GOOD_SUMMARY = (
    "## Historical Task Snapshot\n"
    "Compaction fixture summary with enough seed characters to pass guards.\n"
    "## Resolved\nPrior turns were compacted for the unit test.\n"
    "## Pending / Open Questions\nNone.\n"
    "## Key Facts / Decisions / Files\ntests/test_peek_tools.py\n"
)


class _CompactPilot:
    name = "mock"

    def __init__(self, return_text=_GOOD_SUMMARY):
        self.return_text = return_text

    def chat(self, messages, tools=None, system=None):
        return type("Resp", (), {"text": self.return_text, "error": None, "tokens_out": 10})()

    def complete(self, prompt, system=None):
        return type("Resp", (), {"text": self.return_text, "error": None, "tokens_out": 10})()


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


def _compactable_history(session):
    session._history = [{"role": "system", "content": "sys"}]
    session._history.append({
        "role": "user",
        "content": "UNIQUE-ELIDED-MARKER early topic alpha",
    })
    session._history.append({
        "role": "assistant",
        "content": "UNIQUE-ELIDED-REPLY acknowledged alpha",
    })
    for i in range(10):
        session._history.append({
            "role": "user",
            "content": f"User message number {i}: " + ("A" * 150),
        })
        session._history.append({
            "role": "assistant",
            "content": f"Assistant response number {i}: " + ("B" * 150),
        })
    session._history.append({
        "role": "user",
        "content": "UNIQUE-TAIL-MARKER latest ask",
    })
    session._history.append({
        "role": "assistant",
        "content": "UNIQUE-TAIL-REPLY latest answer",
    })


def test_peek_history_reads_elided_rows_after_real_compact_and_persist(tmp_path, monkeypatch):
    """Exercise the real compact + residual persist path, not an in-memory fixture."""
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    session, state_dir = _session(tmp_path, monkeypatch)
    session.harness_session_id = "sess_peek_real"
    session.pilot = _CompactPilot()
    _compactable_history(session)

    events = list(session._maybe_compact_history(force=True))
    assert [e.kind for e in events] == ["compacting", "compaction"]
    assert events[-1].data.get("aborted") is not True

    save_transcript(state_dir, session.harness_session_id, session.export_transcript_data())
    persisted = load_transcript(state_dir, session.harness_session_id)
    residual = list((persisted or {}).get("history") or [])
    residual_text = " ".join(str(m.get("content") or "") for m in residual)
    assert "UNIQUE-ELIDED-MARKER" not in residual_text
    assert "UNIQUE-TAIL-MARKER" in residual_text or "UNIQUE-TAIL-REPLY" in residual_text

    ok, status, head = session._do_peek_history(
        PilotAction(kind="peek_history", arguments={"offset": 0, "limit": 5})
    )
    assert ok and status == "success"
    assert "compaction_generation=" in head
    assert "UNIQUE-ELIDED-MARKER" in head

    total = 0
    for line in head.splitlines():
        if line.startswith("offset="):
            for part in line.split():
                if part.startswith("total="):
                    total = int(part.split("=", 1)[1])
    assert total > 5
    ok_tail, status_tail, tail = session._do_peek_history(
        PilotAction(
            kind="peek_history",
            arguments={"offset": max(0, total - 5), "limit": 5},
        )
    )
    assert ok_tail and status_tail == "success"
    assert "UNIQUE-TAIL-MARKER" in tail or "UNIQUE-TAIL-REPLY" in tail

    generation = session._compaction_generation()
    assert generation >= 1
    assert "compaction_generation=" in str(session._history[1].get("content") or "")
    ok_zero, status_zero, _zero = session._do_peek_history(
        PilotAction(kind="peek_history", arguments={"expected_generation": 0, "limit": 3})
    )
    assert ok_zero and status_zero == "success"
    ok_stale, status_stale, err = session._do_peek_history(
        PilotAction(kind="peek_history", arguments={"expected_generation": generation + 7})
    )
    assert not ok_stale
    assert status_stale == "stale_generation"
    assert "stale" in err


def test_peek_history_corrupt_archive_fails_closed(tmp_path, monkeypatch):
    session, state_dir = _session(tmp_path, monkeypatch)
    session.harness_session_id = "sess_peek_corrupt"
    sid = session.harness_session_id
    save_transcript(
        state_dir,
        sid,
        {
            "history": [{"role": "user", "content": "live-residual-ok"}],
            "display": [],
            "job_ids": [],
        },
    )
    archive_path = compaction_archive_path(state_dir, sid)
    from pathlib import Path

    Path(archive_path).parent.mkdir(parents=True, exist_ok=True)
    Path(archive_path).write_text("{this is not json", encoding="utf-8")

    ok, status, text = session._do_peek_history(
        PilotAction(kind="peek_history", arguments={"offset": 0, "limit": 5})
    )
    assert ok and status == "success"
    assert "live-residual-ok" in text


def test_peek_history_reads_durable_transcript_without_archive(tmp_path, monkeypatch):
    session, state_dir = _session(tmp_path, monkeypatch)
    session.harness_session_id = "sess_peek"
    sid = session.harness_session_id
    history = [
        {"role": "user", "content": f"turn-{i} detail about topic {i}"}
        for i in range(12)
    ]
    save_transcript(state_dir, sid, {"history": history, "display": [], "job_ids": []})
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
