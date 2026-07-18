"""Characterization tests for session_control API peel."""
from __future__ import annotations

from types import SimpleNamespace

from harness.api.session_control import (
    SessionControlServices,
    get_session_context_at,
    get_session_queue,
    get_session_state,
    get_session_swarm_results,
    post_chat_stash,
    post_session_compact,
    post_session_interrupt,
    post_session_persist,
    post_session_queue,
    post_session_queue_reorder,
    post_session_rewind,
    post_session_steer,
    prepare_session_restart,
)


def _svc(pilot=None, runners=None, upload_dir="/uploads", sessions=None):
    return SessionControlServices(
        cfg=SimpleNamespace(driver="m1", state_dir=None, max_context_tokens=96000),
        get_pilot=lambda: pilot,
        get_runners=lambda: runners or SimpleNamespace(
            get=lambda sid: None,
            statuses=lambda: {},
            active_view_id="v1",
        ),
        gate_active_pilot_ready=lambda: None,
        stash_put=lambda msg, imgs: "mid1",
        save_active_transcript=lambda: None,
        upload_dir=upload_dir,
        diag=lambda *a: None,
        get_sessions=lambda: sessions or SimpleNamespace(active=None),
        save_transcript=lambda *a, **k: None,
        set_resume_latch=lambda: None,
        persist_boot_usage=lambda **k: None,
        consume_resume_pending=lambda idle: False,
        checkpoint_transcript=lambda: None,
        context_at=lambda *a: None,
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


def test_persist_and_restart_prepare():
    calls = {"latch": 0, "usage": 0, "save": 0}

    class _Pilot:
        def export_transcript_data(self):
            return {"history": []}

    sessions = SimpleNamespace(active="s1")
    svc = _svc(pilot=_Pilot(), sessions=sessions)
    svc.set_resume_latch = lambda: calls.__setitem__("latch", calls["latch"] + 1)
    svc.persist_boot_usage = lambda **k: calls.__setitem__(
        "usage", calls["usage"] + 1
    )
    svc.save_transcript = lambda *a, **k: calls.__setitem__(
        "save", calls["save"] + 1
    )

    assert prepare_session_restart(svc) == (True, None)
    assert calls == {"latch": 1, "usage": 1, "save": 1}
    code, payload = post_session_persist(svc)
    assert code == 200 and payload["ok"] is True


class _CompactingPilot:
    """Pilot stub whose forced compaction really shrinks the estimate."""

    state_dir = ""
    harness_session_id = "s1"

    def __init__(self):
        self.force_calls = []
        self.exports = 0
        self._tokens = 50
        self._history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ]

    def _estimate_context_tokens(self):
        return self._tokens

    def _maybe_compact_history(self, force=False):
        self.force_calls.append(force)
        yield {"kind": "compacting", "data": {}}
        self._tokens = 20
        yield {"kind": "compaction", "data": {"before_tokens": 50, "after_tokens": 20}}

    def export_transcript_data(self):
        self.exports += 1
        return {}

    def state(self):
        return "idle"

    def has_pending_swarms(self):
        return False


class _NoopPilot:
    """Pilot stub whose compaction attempt yields nothing (history too small)."""

    state_dir = ""
    harness_session_id = "s1"

    def __init__(self):
        self.exports = 0

    def _estimate_context_tokens(self):
        return 50

    def _maybe_compact_history(self, force=False):
        return iter(())

    def export_transcript_data(self):
        self.exports += 1
        return {}

    def state(self):
        return "idle"

    def has_pending_swarms(self):
        return False


def test_compact_and_state():
    pilot = _CompactingPilot()
    svc = _svc(pilot=pilot, sessions=SimpleNamespace(active=None))
    code, payload = post_session_compact(svc)
    assert code == 200
    assert payload["ok"] is True and payload["compacted"] is True
    assert payload["before_tokens"] == 50 and payload["after_tokens"] == 20
    # Manual compaction must bypass the 75% trigger.
    assert pilot.force_calls == [True]

    code2, state = get_session_state(svc)
    assert code2 == 200
    assert state["state"] == "idle"
    assert state["active_view_id"] == "v1"


def test_compact_noop_is_not_success():
    pilot = _NoopPilot()
    saved = {"n": 0}
    svc = _svc(pilot=pilot, sessions=SimpleNamespace(active="s1"))
    svc.save_transcript = lambda *a, **k: saved.__setitem__("n", saved["n"] + 1)
    code, payload = post_session_compact(svc)
    assert code == 409
    assert payload["ok"] is False and payload["compacted"] is False
    assert payload["before_tokens"] == 50 and payload["after_tokens"] == 50
    assert "error" in payload
    # A no-op must not persist the transcript.
    assert saved["n"] == 0 and pilot.exports == 0


def test_compact_success_persists_and_refreshes_snapshot(tmp_path):
    from harness.memory_layers import latest_layer_snapshot

    pilot = _CompactingPilot()
    pilot.state_dir = str(tmp_path)
    saved = {"n": 0}
    svc = _svc(pilot=pilot, sessions=SimpleNamespace(active="s1"))
    svc.save_transcript = lambda *a, **k: saved.__setitem__("n", saved["n"] + 1)

    assert latest_layer_snapshot(str(tmp_path), "s1") == {}
    code, payload = post_session_compact(svc)
    assert code == 200 and payload["ok"] is True
    assert saved["n"] == 1 and pilot.exports == 1
    # Fresh post-compaction snapshot recorded so /api/usage advice no longer
    # reads the stale pre-compaction L0.
    snap = latest_layer_snapshot(str(tmp_path), "s1")
    assert snap and "L0" in snap
    assert snap["L0"]["entries"] == len(pilot._history) - 1


def test_context_at_and_swarm_results():
    class _Ev:
        kind = "swarm_done"
        data = {"ok": True}

    class _Pilot:
        state_dir = "/tmp"
        harness_session_id = "s1"

        def drain_swarm_results(self):
            return [_Ev()]

    ckpt = {"n": 0}
    svc = _svc(pilot=_Pilot())
    svc.context_at = lambda *a: {"turn": a[2], "tokens": 1}
    svc.checkpoint_transcript = lambda: ckpt.__setitem__("n", ckpt["n"] + 1)

    code, rec = get_session_context_at(3, svc)
    assert code == 200 and rec["turn"] == 3
    code2, payload = get_session_swarm_results(svc)
    assert code2 == 200 and payload["results"][0]["kind"] == "swarm_done"
    assert ckpt["n"] == 1
