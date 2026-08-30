from __future__ import annotations

"""Chat archive: ingest copy, vault search, prune only after a vault copy."""

from pathlib import Path

from harness.chat_archive import (
    archive_status,
    distinctive_tokens,
    ingest_all,
    ingest_marionette_session,
    prune_ingested_transcripts,
    read_archived_chat,
    restore_pruned_transcript,
    search_archive,
)
from harness.sessions import load_transcript, save_transcript


def test_distinctive_tokens_drop_stops():
    assert distinctive_tokens("the leak in renderer") == ("renderer", "leak")
    assert distinctive_tokens("the and for") == ()


def test_ingest_search_read_and_backup(tmp_path):
    state = str(tmp_path)
    (tmp_path / "transcripts").mkdir()
    save_transcript(
        state,
        "sess1",
        [
            {"role": "user", "content": "Track the quasar preview bug"},
            {"role": "assistant", "content": "The preview pane dropped the nebula thumbnail."},
        ],
    )
    report = ingest_marionette_session(state, "sess1", title="Quasar preview")
    assert report.ingested == 1
    hits = search_archive(state, "quasar nebula")
    assert hits
    assert hits[0]["chat_id"] == "marionette:sess1"
    payload = read_archived_chat(state, "marionette:sess1")
    assert payload is not None
    assert payload["source"] == "marionette"
    assert "nebula thumbnail" in payload["messages"][1]["text"]
    backup = Path(payload["backup_path"])
    assert backup.is_file()
    assert "nebula thumbnail" in backup.read_text(encoding="utf-8")
    again = ingest_marionette_session(state, "sess1", title="Quasar preview")
    assert again.ingested == 0
    assert again.skipped_unchanged == 1


def test_search_requires_all_tokens(tmp_path):
    state = str(tmp_path)
    (tmp_path / "transcripts").mkdir()
    save_transcript(
        state,
        "sess1",
        [{"role": "user", "content": "Track the quasar preview bug"}],
    )
    ingest_marionette_session(state, "sess1", title="Quasar preview")
    assert search_archive(state, "quasar xylophone-zzztop") == []


def test_ingest_all_skips_active_sessions(tmp_path):
    state = str(tmp_path)
    (tmp_path / "transcripts").mkdir()
    save_transcript(state, "hot", [{"role": "user", "content": "live zircon topic"}])
    save_transcript(state, "cold", [{"role": "user", "content": "archived nebula topic"}])
    out = ingest_all(
        state,
        sessions=[
            {"id": "hot", "archived": False, "title": "Live"},
            {"id": "cold", "archived": True, "title": "Cold"},
        ],
    )
    assert out["ingested"] == 1
    assert search_archive(state, "nebula")[0]["chat_id"] == "marionette:cold"
    assert search_archive(state, "zircon") == []


def test_prune_only_after_ingest_and_never_active(tmp_path):
    state = str(tmp_path)
    (tmp_path / "transcripts").mkdir()
    save_transcript(
        state,
        "cold",
        [
            {"role": "user", "content": "Archive the nebula thumbnail hunt"},
            {"role": "assistant", "content": "Vault copy then compact."},
        ],
    )
    save_transcript(state, "hot", [{"role": "user", "content": "still active zircon"}])
    sessions = [
        {"id": "cold", "archived": True, "title": "Cold"},
        {"id": "hot", "archived": False, "title": "Hot"},
    ]
    refused = prune_ingested_transcripts(state, sessions)
    assert refused["pruned"] == 0
    ingest_all(state, sessions=sessions)
    pruned = prune_ingested_transcripts(state, sessions)
    assert pruned["pruned"] == 1
    assert load_transcript(state, "cold") == []
    assert "zircon" in str(load_transcript(state, "hot"))
    hits = search_archive(state, "nebula thumbnail")
    assert hits[0]["chat_id"] == "marionette:cold"
    assert restore_pruned_transcript(state, "cold") is True
    restored = load_transcript(state, "cold")
    assert any("nebula thumbnail" in str(m.get("content")) for m in restored)


def test_status_is_marionette_vault(tmp_path):
    st = archive_status(str(tmp_path))
    assert "prunes_cursor_db" not in st
    assert "vault_path" not in st
    assert st["chats"] == 0
    assert st["vault_present"] is False


def test_parse_and_dispatch_archive_tools(tmp_path):
    from types import SimpleNamespace
    from harness.pilot import parse_tool_calls
    from harness.send_loop_phases import LOCAL_ACTION_KINDS
    from harness.tool_dispatch import ToolDispatchMixin

    assert "search_archive" in LOCAL_ACTION_KINDS
    assert "read_archived_chat" in LOCAL_ACTION_KINDS
    state = str(tmp_path)
    (tmp_path / "transcripts").mkdir()
    save_transcript(
        state,
        "sess1",
        [{"role": "user", "content": "Track the quasar preview bug"}],
    )
    ingest_marionette_session(state, "sess1", title="Quasar preview")

    actions = parse_tool_calls([
        {
            "id": "t1",
            "type": "function",
            "function": {
                "name": "search_archive",
                "arguments": '{"query": "quasar preview"}',
            },
        },
        {
            "id": "t2",
            "type": "function",
            "function": {
                "name": "read_archived_chat",
                "arguments": '{"chat_id": "marionette:sess1"}',
            },
        },
    ])
    assert actions[0].kind == "search_archive"
    assert actions[0].query == "quasar preview"
    assert actions[1].kind == "read_archived_chat"
    assert actions[1].path == "marionette:sess1"

    host = SimpleNamespace(
        state_dir=state,
        config=SimpleNamespace(state_dir=state),
    )
    ok, status, text = ToolDispatchMixin._do_search_archive(host, actions[0])
    assert ok and status == "success"
    assert "marionette:sess1" in text
    ok, status, body = ToolDispatchMixin._do_read_archived_chat(host, actions[1])
    assert ok and status == "success"
    assert "quasar preview" in body.lower()


def test_archive_http_peel(tmp_path):
    from harness.api.chat_archive import (
        ChatArchiveServices,
        get_archive_search,
        get_archive_status,
        post_archive_ingest,
        post_archive_prune,
    )

    state = str(tmp_path)
    (tmp_path / "transcripts").mkdir()
    save_transcript(
        state,
        "sess1",
        [{"role": "user", "content": "Track the quasar preview bug"}],
    )
    sessions = [{"id": "sess1", "archived": True, "title": "Quasar preview"}]

    def _svc():
        return ChatArchiveServices(state_dir=lambda: state, list_sessions=lambda: sessions)

    code, report = post_archive_ingest({}, _svc())
    assert code == 200
    assert report["ingested"] == 1
    assert "cursor_buddy" not in report
    code, payload = get_archive_search({"q": ["quasar"]}, _svc())
    assert code == 200
    assert payload["hits"]
    code, status = get_archive_status({}, _svc())
    assert code == 200
    assert status["chats"] >= 1
    assert "prunes_cursor_db" not in status
    code, pruned = post_archive_prune({}, _svc())
    assert code == 200
    assert pruned["pruned"] == 1
    assert load_transcript(state, "sess1") == []
