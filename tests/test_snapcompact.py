"""Hermetic snapcompact: archive + vault + journal on the existing path."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from harness.api.session_control import (
    SessionControlServices,
    post_session_compact_routed,
    post_session_snapcompact,
)
from harness.compaction_archive import load_compaction_archive_messages
from harness.compaction_vault import retrieve_vault_chunks, snap_compact
from harness.history_compaction_journal import DB_FILENAME


NONCE = "omega-snap-token-9f3a"


def _messages():
    return [
        {"role": "user", "content": "Run the snapcompact probe."},
        {
            "role": "assistant",
            "content": f"Probe finished with measurement {NONCE}.",
        },
    ]


def test_snap_compact_writes_archive_vault_and_journal(tmp_path):
    result = snap_compact(str(tmp_path), "sess-snap", _messages())
    assert result["ok"] is True
    assert result["snapshot_id"].startswith("snap-")
    assert result["session_id"] == "sess-snap"
    assert result["archived"] is True
    assert result["archived_messages"] >= 2
    assert result["vault_chunks"] >= 1
    assert result["chars_before"] > 0

    archived = load_compaction_archive_messages(str(tmp_path), "sess-snap")
    assert any(NONCE in str(row.get("content") or "") for row in archived)

    hits = retrieve_vault_chunks(
        str(tmp_path),
        "sess-snap",
        "What snapcompact probe measurement token was returned?",
    )
    assert any(NONCE in hit for hit in hits)

    conn = sqlite3.connect(str(tmp_path / DB_FILENAME))
    try:
        rows = conn.execute(
            "SELECT summary_preview, compact_policy, messages_compacted "
            "FROM compactions"
        ).fetchall()
    finally:
        conn.close()
    assert rows
    preview, policy, count = rows[-1]
    assert result["snapshot_id"] in str(preview)
    assert policy == "snap"
    assert int(count) >= 2


def test_snap_compact_empty_history_is_noop(tmp_path):
    result = snap_compact(str(tmp_path), "sess-empty", [])
    assert result["ok"] is False
    assert result["reason"] == "no_compactable_history"
    assert result["snapshot_id"] == ""
    assert load_compaction_archive_messages(str(tmp_path), "sess-empty") == []


def _svc(tmp_path, pilot, sessions=None, not_ready=None):
    return SessionControlServices(
        cfg=SimpleNamespace(driver="m1", state_dir=str(tmp_path), max_context_tokens=96000),
        get_pilot=lambda: pilot,
        get_runners=lambda: SimpleNamespace(
            get=lambda sid: None,
            statuses=lambda: {},
            active_view_id="v1",
        ),
        gate_active_pilot_ready=lambda: not_ready,
        stash_put=lambda msg, imgs: "mid1",
        save_active_transcript=lambda: None,
        upload_dir="/uploads",
        diag=lambda *a: None,
        get_sessions=lambda: sessions or SimpleNamespace(active="sess-snap"),
        save_transcript=lambda *a, **k: None,
        set_resume_latch=lambda *a, **k: None,
        persist_boot_usage=lambda **k: None,
        peek_resume_pending=lambda idle, session_id="": False,
        consume_resume_pending=lambda idle, session_id="": False,
        checkpoint_transcript=lambda: None,
        context_at=lambda *a: None,
    )


class _SnapPilot:
    harness_session_id = "sess-snap"

    def export_transcript_data(self):
        return {"history": _messages(), "display": [], "job_ids": []}


def test_post_session_snapcompact_returns_snapshot_id(tmp_path):
    svc = _svc(tmp_path, _SnapPilot())
    code, payload = post_session_snapcompact(svc)
    assert code == 200
    assert payload["ok"] is True
    assert payload["compacted"] is True
    assert payload["op"] == "snap"
    assert str(payload["snapshot_id"]).startswith("snap-")
    assert payload["archived_messages"] >= 2
    assert payload["vault_chunks"] >= 1


def test_post_session_compact_op_snap_uses_snapcompact(tmp_path):
    svc = _svc(tmp_path, _SnapPilot())
    code, payload = post_session_compact_routed({"op": "snap"}, svc)
    assert code == 200
    assert payload["op"] == "snap"
    assert payload["snapshot_id"]
    archived = load_compaction_archive_messages(str(tmp_path), "sess-snap")
    assert any(NONCE in str(row.get("content") or "") for row in archived)


def test_snapcompact_route_is_registered():
    import harness.http_routes as http_routes
    import harness.server as srv

    svc = srv._route_services()
    post = http_routes.build_post_json_routes(svc)
    assert "/api/session/snapcompact" in post
    assert "/api/session/compact" in post
    assert callable(post["/api/session/snapcompact"])


def test_snapcompact_respects_pilot_gate(tmp_path):
    svc = _svc(tmp_path, _SnapPilot(), not_ready={"error": "busy"})
    code, payload = post_session_snapcompact(svc)
    assert code == 409
    assert payload["error"] == "busy"
