"""Characterization tests for session_control API peel."""
from __future__ import annotations

from types import SimpleNamespace

from harness.api.session_control import (
    SessionControlServices,
    get_session_queue,
    post_chat_stash,
    post_session_interrupt,
    post_session_queue,
    post_session_queue_reorder,
    post_session_rewind,
    post_session_steer,
)


def _svc(pilot=None, runners=None, upload_dir="/uploads"):
    return SessionControlServices(
        cfg=SimpleNamespace(driver="m1"),
        get_pilot=lambda: pilot,
        get_runners=lambda: runners or SimpleNamespace(get=lambda sid: None),
        gate_active_pilot_ready=lambda: None,
        stash_put=lambda msg, imgs: "mid1",
        save_active_transcript=lambda: None,
        upload_dir=upload_dir,
        diag=lambda *a: None,
    )


def test_chat_stash():
    svc = _svc()
    assert post_chat_stash({}, svc)[0] == 400
    code, payload = post_chat_stash({"message": "hi"}, svc)
    assert code == 200 and payload["id"] == "mid1"


def test_interrupt_active_and_missing_runner():
    class _P:
        def __init__(self):
            self.n = 0

        def interrupt(self):
            self.n += 1

    p = _P()
    svc = _svc(pilot=p)
    assert post_session_interrupt({}, "", svc)[0] == 200
    assert p.n == 1
    code, payload = post_session_interrupt({}, "gone", svc)
    assert code == 404


def test_steer_and_queue(tmp_path):
    class _P:
        def __init__(self):
            self.steers = []
            self.prompts = []

        def enqueue_steer(self, text):
            self.steers.append(text)

        def clear_prompts(self):
            n = len(self.prompts)
            self.prompts.clear()
            return n

        def remove_prompt(self, rid):
            self.prompts = [x for x in self.prompts if x["id"] != rid]
            return True

        def enqueue_prompt(self, text, images=None, model=None):
            item = {"id": "q1", "text": text, "model": model}
            self.prompts.append(item)
            return item

        def list_prompts(self):
            return list(self.prompts)

        def reorder_prompts(self, ids):
            return [{"id": i} for i in ids]

    p = _P()
    svc = _svc(pilot=p, upload_dir=str(tmp_path))
    assert post_session_steer({}, svc)[0] == 400
    assert post_session_steer({"text": "go"}, svc)[0] == 200
    assert p.steers == ["go"]
    code, enq = post_session_queue({"text": "next"}, svc)
    assert code == 200 and enq["item"]["id"] == "q1"
    assert get_session_queue(svc)[1]["items"][0]["id"] == "q1"
    assert post_session_queue({"clear": True}, svc)[1]["cleared"] == 1
    code2, reo = post_session_queue_reorder({"ids": ["a", "b"]}, svc)
    assert code2 == 200 and [i["id"] for i in reo["items"]] == ["a", "b"]


def test_rewind_requires_target():
    p = SimpleNamespace(
        rewind_to_user_ordinal=lambda n: {"ok": True, "n": n},
        rewind_to_display_index=lambda n: {"ok": True, "n": n},
    )
    svc = _svc(pilot=p)
    assert post_session_rewind({}, svc)[0] == 400
    code, payload = post_session_rewind({"user_ordinal": 2}, svc)
    assert code == 200 and payload["ok"] is True
