"""The transcript endpoint sends history only when there is no display."""
from __future__ import annotations

from types import SimpleNamespace

from harness.api.sessions import get_sessions_transcript


def _svc(data):
    runner = SimpleNamespace(export_transcript_data=lambda: data)
    return SimpleNamespace(
        sessions=SimpleNamespace(active="s1"),
        runners=SimpleNamespace(get=lambda sid: runner),
        cfg=SimpleNamespace(state_dir=""),
        diag=lambda *a, **k: None,
    )


def test_display_transcripts_do_not_also_ship_history():
    history = [{"role": "tool", "content": "x" * 5000}] * 20
    display = [{"type": "message", "role": "assistant", "text": "hi"}]
    code, body = get_sessions_transcript({"session": ["s1"]}, _svc({"history": history, "display": display, "job_ids": ["j"]}))
    assert code == 200
    assert body == {"history": [], "display": display, "job_ids": ["j"]}


def test_legacy_transcripts_without_display_keep_history():
    history = [{"role": "user", "content": "hello"}]
    code, body = get_sessions_transcript({"session": ["s1"]}, _svc({"history": history, "display": []}))
    assert body["history"] == history
