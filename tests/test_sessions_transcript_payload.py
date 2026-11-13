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


def test_live_transcript_carries_the_ring_position_read_before_the_export():
    from harness.api import sse

    sse._sse_ring_clear_for_tests()
    try:
        ring = sse._sse_ring_begin("s1")
        ring.append("message", {"text": "a"})
        ring.append("assistant_done", {})
        seen = []

        def export():
            # A frame that lands during the export is not in the position.
            seen.append(ring.cursor)
            ring.append("notice", {})
            return {"history": [], "display": [{"type": "message"}]}

        code, body = get_sessions_transcript({"session": ["s1"]}, _svc_export(export))
        assert code == 200
        assert seen == [2]
        assert body["ring"] == {"generation": ring.generation, "cursor": 2}
    finally:
        sse._sse_ring_clear_for_tests()


def test_transcript_without_a_ring_or_a_live_runner_has_no_position(tmp_path):
    from harness.api import sse

    sse._sse_ring_clear_for_tests()
    try:
        code, body = get_sessions_transcript({"session": ["s1"]}, _svc({"history": [], "display": []}))
        assert "ring" not in body
        ring = sse._sse_ring_begin("s1")
        ring.append("assistant_done", {})
        svc = _svc({})
        svc.runners = SimpleNamespace(get=lambda sid: None)
        svc.cfg = SimpleNamespace(state_dir=str(tmp_path))
        code, body = get_sessions_transcript({"session": ["s1"]}, svc)
        assert code == 200
        assert "ring" not in body
    finally:
        sse._sse_ring_clear_for_tests()


def _svc_export(export):
    svc = _svc({})
    svc.runners = SimpleNamespace(get=lambda sid: SimpleNamespace(export_transcript_data=export))
    return svc
