from __future__ import annotations

from types import SimpleNamespace

from harness.api.session_control import SessionControlServices, post_session_images_strip
from harness.conversation import ConversationalSession


def test_strip_history_images_drops_attachments(tmp_path):
    session = ConversationalSession.__new__(ConversationalSession)
    session._history = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "see this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
            ],
            "images": ["shot.png"],
        },
    ]
    session._display_transcript = [{"role": "user", "text": "see this", "images": ["shot.png"]}]
    removed = session.strip_history_images()
    assert removed >= 2
    assert "images" not in session._history[1]
    assert session._history[1]["content"] == [{"type": "text", "text": "see this"}]
    assert "images" not in session._display_transcript[0]


def test_images_strip_api(tmp_path):
    session = ConversationalSession.__new__(ConversationalSession)
    session._history = [
        {"role": "user", "content": "hi", "images": ["a.png"]},
    ]
    session._display_transcript = []
    svc = SessionControlServices(
        cfg=SimpleNamespace(state_dir=str(tmp_path)),
        get_pilot=lambda: session,
        get_runners=lambda: None,
        gate_active_pilot_ready=lambda: None,
        stash_put=lambda *a, **k: "",
        save_active_transcript=lambda: None,
        upload_dir=str(tmp_path),
        diag=lambda *a, **k: None,
        get_sessions=lambda: SimpleNamespace(active=""),
    )
    code, payload = post_session_images_strip(svc)
    assert code == 200
    assert payload["ok"] is True
    assert payload["stripped"] >= 1
    assert "images" not in session._history[0]
