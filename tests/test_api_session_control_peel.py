"""Characterization tests for session_control API peel."""
from __future__ import annotations

from types import SimpleNamespace

from harness.api.session_control import (
    SessionControlServices,
    get_session_context_at,
    get_session_loop,
    get_session_queue,
    get_session_state,
    get_session_swarm_results,
    post_chat_stash,
    post_session_compact,
    post_session_interrupt,
    post_session_loop,
    post_session_persist,
    post_session_queue,
    post_session_queue_reorder,
    post_session_rewind,
    post_session_steer,
    post_session_todo,
    prepare_session_restart,
)
from harness.session_actions import ActionKind, SessionActionStore
from harness.session_loop import SessionLoop


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
        set_resume_latch=lambda *a, **k: None,
        persist_boot_usage=lambda **k: None,
        peek_resume_pending=lambda idle, session_id="": False,
        consume_resume_pending=lambda idle, session_id="": False,
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
    code, payload = post_session_interrupt({}, "", svc)
    assert code == 200
    assert payload == {"ok": True}
    assert p.n == 1
    code, payload = post_session_interrupt({}, "gone", svc)
    assert code == 404


def test_interrupt_returns_pending_honesty_notices():
    class _P:
        def __init__(self):
            self.n = 0
            self._pending = [
                {"message": "orphan procs", "reason": "owned_command_orphan", "count": 1},
                {"message": "dropped steers", "reason": "steer_dropped", "count": 2},
            ]

        def interrupt(self):
            self.n += 1

        def peek_post_interrupt_notices(self):
            return list(self._pending)

    p = _P()
    svc = _svc(pilot=p)
    code, payload = post_session_interrupt({}, "", svc)
    assert code == 200
    assert payload["ok"] is True
    assert payload["notices"] == p._pending
    assert p.n == 1
    # Interrupt API must not drain — stream flush still owns pending.
    assert len(p.peek_post_interrupt_notices()) == 2


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
    code_steer, payload_steer = post_session_steer({"text": "go"}, svc)
    assert code_steer == 200
    assert payload_steer["ok"] is True
    assert payload_steer["action"] == "enqueue_steer"
    assert p.steers == ["go"]
    code, enq = post_session_queue({"text": "next"}, svc)
    assert code == 200 and enq["item"]["id"] == "q1"
    assert get_session_queue(svc)[1]["items"][0]["id"] == "q1"
    assert post_session_queue({"clear": True}, svc)[1]["cleared"] == 1
    code2, reo = post_session_queue_reorder({"ids": ["a", "b"]}, svc)
    assert code2 == 200 and [i["id"] for i in reo["items"]] == ["a", "b"]


def test_steer_vision_images_reports_enqueue_prompt(tmp_path):
    """Busy-Enter with pixels must tell the UI it queued, not steered."""
    img = tmp_path / "shot.png"
    img.write_bytes(b"x")

    class _P:
        def __init__(self):
            self.steers = []
            self.prompts = []

        def enqueue_steer(self, text):
            self.steers.append(text)

        def enqueue_prompt(self, text, images=None, model=None):
            item = {"id": "q1", "text": text, "images": list(images or [])}
            self.prompts.append(item)
            return item

        def steer_with_images(self, text, images=None):
            self.enqueue_prompt(text or "(see attached image)", images=images)
            return "enqueue_prompt"

    p = _P()
    svc = _svc(pilot=p, upload_dir=str(tmp_path))
    code, payload = post_session_steer(
        {"text": "look", "images": [str(img)]}, svc,
    )
    assert code == 200
    assert payload["ok"] is True
    assert payload["action"] == "enqueue_prompt"
    assert p.steers == []
    assert p.prompts[0]["text"] == "look"


def test_steer_invalid_turn_input_mode_is_400():
    class _P:
        def enqueue_steer(self, text):
            raise AssertionError("invalid mode must not enqueue")

    svc = _svc(pilot=_P())
    code, payload = post_session_steer(
        {"text": "go", "turn_input_mode": "recover"}, svc,
    )
    assert code == 400
    assert payload["ok"] is False
    assert payload["error"] == "invalid turn_input_mode"


def test_steer_recover_turn_admits_recover_and_requires_expected_turn_id():
    class _P:
        def __init__(self):
            self._session_actions = SessionActionStore()

        def enqueue_steer(self, text):
            raise AssertionError("RecoverTurn must not enqueue_steer")

    p = _P()
    svc = _svc(pilot=p)
    code, payload = post_session_steer(
        {"text": "resume", "kind": "recover"}, svc,
    )
    assert code == 400
    assert payload["ok"] is False
    assert payload["code"] == "recover_requires_expected_turn_id"
    assert list(p._session_actions) == []

    code, payload = post_session_steer(
        {"text": "resume", "kind": "recover", "expected_turn_id": "turn-7"}, svc,
    )
    assert code == 200
    assert payload["ok"] is True
    assert payload["action"] == "recover"
    admitted = list(p._session_actions)
    assert [a.kind for a in admitted] == [ActionKind.RECOVER]
    assert admitted[0].expected_turn_id == "turn-7"


def test_steer_turn_input_mode_start_if_idle():
    class _P:
        def __init__(self):
            self._session_actions = SessionActionStore()
            self._busy = False

        def is_turn_busy(self):
            return self._busy

        def enqueue_steer(self, text):
            raise AssertionError("start_if_idle must admit, not enqueue_steer")

        def enqueue_prompt(self, text, images=None, model=None):
            item = {"id": "s1", "text": text, "images": images or []}
            self.prompts.append(item)
            return item

    p = _P()
    p.prompts = []
    svc = _svc(pilot=p)
    code, payload = post_session_steer(
        {"text": "begin", "turn_input_mode": "start_if_idle"}, svc,
    )
    assert code == 200
    assert payload["action"] == "start"
    assert payload["kind"] == "start"
    assert list(p._session_actions)[0].kind is ActionKind.START
    assert p.prompts[0]["text"] == "begin"
    assert payload["item"]["text"] == "begin"
    assert payload["deferred"] is True

    p._busy = True
    code, payload = post_session_steer(
        {"text": "nope", "turn_input_mode": "start_if_idle"}, svc,
    )
    assert code == 400
    assert payload["code"] == "start_if_idle_busy"


def test_session_loop_start_stop_and_rejects_interval_without_seconds():
    p = SimpleNamespace(_loop_state=SessionLoop())
    svc = _svc(pilot=p)
    code, payload = get_session_loop(svc)
    assert code == 200
    assert payload["loop"]["enabled"] is False

    code, payload = post_session_loop(
        {"action": "start", "mode": "interval", "prompt": "again"}, svc,
    )
    assert code == 400
    assert payload["code"] == "interval_requires_seconds"

    code, payload = post_session_loop(
        {
            "action": "start",
            "mode": "self_paced",
            "prompt": "next beat",
        },
        svc,
    )
    assert code == 200
    assert payload["loop"]["enabled"] is True
    assert payload["loop"]["mode"] == "self_paced"
    code, payload = post_session_loop({"action": "stop"}, svc)
    assert code == 200
    assert payload["loop"]["enabled"] is False


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
    calls = {"latch": 0, "usage": 0, "save": 0, "sid": None}

    class _Pilot:
        def export_transcript_data(self):
            return {"history": []}

    sessions = SimpleNamespace(active="s1")
    svc = _svc(pilot=_Pilot(), sessions=sessions)

    def _set_latch(session_id=""):
        calls["latch"] = calls["latch"] + 1
        calls["sid"] = session_id

    svc.set_resume_latch = _set_latch
    svc.persist_boot_usage = lambda **k: calls.__setitem__(
        "usage", calls["usage"] + 1
    )
    svc.save_transcript = lambda *a, **k: calls.__setitem__(
        "save", calls["save"] + 1
    )

    assert prepare_session_restart(svc) == (True, None)
    assert calls["latch"] == 1 and calls["usage"] == 1 and calls["save"] == 1
    assert calls["sid"] == "s1"
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

    def __init__(self, reason="no_compactable_history"):
        self.exports = 0
        self._history = [{"role": "user", "content": "hi"}]
        self._last_compaction_attempt = {"reason": reason}

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
    assert payload.get("reason") == "ok"
    # Manual compaction must bypass the 75% trigger.
    assert pilot.force_calls == [True]
    ack = getattr(pilot, "_compaction_advice_ack", None)
    assert isinstance(ack, dict) and ack.get("reason") == "ok"

    code2, state = get_session_state({}, svc)
    assert code2 == 200
    assert state["state"] == "idle"
    assert state["active_view_id"] == "v1"


def test_session_state_resume_pending_peek_vs_consume():
    """Plain GET peeks; only ?consume_resume=1 clears the latch."""
    calls = {"peek": 0, "consume": 0}
    latch = {"armed": True, "owner": "sess-a"}

    def peek(idle: bool, session_id: str = "") -> bool:
        calls["peek"] += 1
        return bool(
            latch["armed"] and idle and session_id == latch["owner"]
        )

    def consume(idle: bool, session_id: str = "") -> bool:
        calls["consume"] += 1
        if not (latch["armed"] and idle and session_id == latch["owner"]):
            return False
        latch["armed"] = False
        return True

    pilot = _CompactingPilot()
    svc = _svc(pilot=pilot)
    svc.peek_resume_pending = peek
    svc.consume_resume_pending = consume

    code, state = get_session_state({"session_id": ["sess-a"]}, svc)
    assert code == 200 and state["resume_pending"] is True
    assert calls == {"peek": 1, "consume": 0}
    assert latch["armed"] is True

    code, state = get_session_state(
        {"consume_resume": ["0"], "session_id": ["sess-a"]}, svc
    )
    assert code == 200 and state["resume_pending"] is True
    assert calls == {"peek": 2, "consume": 0}
    assert latch["armed"] is True

    code, state = get_session_state(
        {"consume_resume": ["1"], "session_id": ["sess-a"]}, svc
    )
    assert code == 200 and state["resume_pending"] is True
    assert calls == {"peek": 2, "consume": 1}
    assert latch["armed"] is False

    code, state = get_session_state(
        {"consume_resume": ["1"], "session_id": ["sess-a"]}, svc
    )
    assert code == 200 and state["resume_pending"] is False
    assert calls["consume"] == 2


def test_session_state_resume_pending_fail_closed_wrong_session():
    """Latch armed for A must not peek/consume for B."""
    latch = {"armed": True, "owner": "sess-a"}

    def peek(idle: bool, session_id: str = "") -> bool:
        return bool(
            latch["armed"] and idle and session_id == latch["owner"]
        )

    def consume(idle: bool, session_id: str = "") -> bool:
        if not (latch["armed"] and idle and session_id == latch["owner"]):
            return False
        latch["armed"] = False
        return True

    pilot = _CompactingPilot()
    svc = _svc(pilot=pilot)
    svc.peek_resume_pending = peek
    svc.consume_resume_pending = consume

    code, state = get_session_state({"session_id": ["sess-b"]}, svc)
    assert code == 200 and state["resume_pending"] is False
    assert latch["armed"] is True

    code, state = get_session_state(
        {"consume_resume": ["1"], "session_id": ["sess-b"]}, svc
    )
    assert code == 200 and state["resume_pending"] is False
    assert latch["armed"] is True

    code, state = get_session_state({"session_id": ["sess-a"]}, svc)
    assert code == 200 and state["resume_pending"] is True


def test_session_state_rearm_resume_restores_latch():
    """?rearm_resume=1 restores latch after a consume abandoned by switch."""
    latch = {"armed": False, "owner": ""}
    rearm_calls = {"n": 0, "sid": ""}

    def peek(idle: bool, session_id: str = "") -> bool:
        return bool(
            latch["armed"] and idle and session_id == latch["owner"]
        )

    def consume(idle: bool, session_id: str = "") -> bool:
        if not (latch["armed"] and idle and session_id == latch["owner"]):
            return False
        latch["armed"] = False
        return True

    def set_latch(session_id: str = "") -> None:
        rearm_calls["n"] += 1
        rearm_calls["sid"] = session_id
        latch["armed"] = True
        latch["owner"] = session_id

    pilot = _CompactingPilot()
    svc = _svc(pilot=pilot)
    svc.peek_resume_pending = peek
    svc.consume_resume_pending = consume
    svc.set_resume_latch = set_latch

    code, state = get_session_state(
        {"rearm_resume": ["1"], "session_id": ["sess-a"]}, svc
    )
    assert code == 200 and state["resume_pending"] is True
    assert rearm_calls == {"n": 1, "sid": "sess-a"}
    assert latch["armed"] is True

    code, state = get_session_state(
        {"consume_resume": ["1"], "session_id": ["sess-a"]}, svc
    )
    assert code == 200 and state["resume_pending"] is True
    assert latch["armed"] is False

    code, state = get_session_state(
        {"rearm_resume": ["1"], "session_id": ["sess-a"]}, svc
    )
    assert code == 200 and state["resume_pending"] is True
    assert latch["armed"] is True


def test_compact_noop_is_not_success():
    pilot = _NoopPilot()
    saved = {"n": 0}
    svc = _svc(pilot=pilot, sessions=SimpleNamespace(active="s1"))
    svc.save_transcript = lambda *a, **k: saved.__setitem__("n", saved["n"] + 1)
    code, payload = post_session_compact(svc)
    assert code == 409
    assert payload["ok"] is False and payload["compacted"] is False
    assert payload["before_tokens"] == 50 and payload["after_tokens"] == 50
    assert payload["reason"] == "no_compactable_history"
    assert "already compact" in payload["error"].lower()
    # A no-op must not persist the transcript.
    assert saved["n"] == 0 and pilot.exports == 0
    # Failed Compact Now must not latch calm — pressure stays visible.
    assert getattr(pilot, "_compaction_advice_ack", None) is None


def test_compact_summary_rejected_reason():
    pilot = _NoopPilot(reason="summary_rejected")
    svc = _svc(pilot=pilot, sessions=SimpleNamespace(active=None))
    code, payload = post_session_compact(svc)
    assert code == 409
    assert payload["reason"] == "summary_rejected"
    assert "rejected" in payload["error"].lower()
    assert getattr(pilot, "_compaction_advice_ack", None) is None


def test_compact_aborted_event_is_not_success():
    """Aborted compaction events must not report Compacted / latch calm."""

    class _AbortedPilot(_NoopPilot):
        def __init__(self):
            super().__init__(reason="summary_rejected")
            self._tokens = 8000

        def _estimate_context_tokens(self):
            return self._tokens

        def _maybe_compact_history(self, force=False):
            yield {"kind": "compacting", "data": {}}
            yield {
                "kind": "compaction",
                "data": {
                    "before_tokens": 8000,
                    "after_tokens": 8000,
                    "summarized_messages": 0,
                    "aborted": True,
                    "reason": "insufficient_reduction",
                },
            }

    pilot = _AbortedPilot()
    svc = _svc(pilot=pilot, sessions=SimpleNamespace(active=None))
    code, payload = post_session_compact(svc)
    assert code == 409
    assert payload["ok"] is False and payload["compacted"] is False
    assert payload["reason"] == "summary_rejected"
    assert payload["before_tokens"] == payload["after_tokens"] == 8000
    assert getattr(pilot, "_compaction_advice_ack", None) is None


def test_compact_below_min_keeps_distinct_reason():
    pilot = _NoopPilot(reason="below_min_compactable")
    svc = _svc(pilot=pilot, sessions=SimpleNamespace(active=None))
    code, payload = post_session_compact(svc)
    assert code == 409
    assert payload["ok"] is False and payload["compacted"] is False
    assert payload["reason"] == "below_min_compactable"
    assert "not enough history" in payload["error"].lower()
    assert getattr(pilot, "_compaction_advice_ack", None) is None


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


def test_post_session_todo_slash():
    class _Pilot:
        def handle_todo_slash(self, command, workspace_root=""):
            return {
                "ok": True,
                "notice": "TODO 0/1",
                "todos": {"phases": [{"name": "Tasks", "tasks": []}]},
                "command": command,
                "workspace": workspace_root,
            }

    svc = _svc(pilot=_Pilot())
    svc.cfg.repo = "/repo"
    code, payload = post_session_todo({"command": "/todo view"}, svc)
    assert code == 200
    assert payload["ok"] is True
    assert payload["command"] == "/todo view"
    assert payload["workspace"] == "/repo"
