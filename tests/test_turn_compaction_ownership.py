"""Regression coverage for turn admission and abandoned compaction ownership."""
import copy
from contextvars import copy_context
import threading

import pytest

from harness.approval_identity import get_approval_turn_id
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, ConvEvent
from pmharness.drivers.base import DriverResponse


def session_at(tmp_path):
    return ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))


def test_rejected_send_preserves_stop_and_warnings(tmp_path):
    session = session_at(tmp_path)
    session._busy.acquire()
    gen = session._mark_busy_acquired()
    session._cancel.set()
    session._pending_advisor_warnings = ["owned warning"]
    try:
        events = list(session.send("rejected"))
        assert events[0].kind == "error"
        assert session._cancel.is_set()
        assert session._pending_advisor_warnings == ["owned warning"]
    finally:
        session._release_busy(gen)


def test_stale_send_cleanup_preserves_new_owner(tmp_path, monkeypatch):
    session = session_at(tmp_path)
    initial_turn_id = get_approval_turn_id()

    def suspended(*args, **kwargs):
        yield ConvEvent("notice", {"message": "suspended"})

    monkeypatch.setattr(session, "_send_locked_inner", suspended)
    # Overlapping HTTP requests execute in separate contexts, not nested turns.
    old_context, new_context = copy_context(), copy_context()
    old = session.send("old")
    new = session.send("new")
    try:
        old_context.run(next, old)
        old_turn_id = old_context.run(get_approval_turn_id)
        monkeypatch.setattr(session, "_turn_deadline_seconds", lambda: 1)
        session._busy_since -= 2
        session._busy_last_progress = session._busy_since
        assert session._reap_stuck_turn()
        new_context.run(next, new)
        new_turn_id = new_context.run(get_approval_turn_id)
        assert old_turn_id and new_turn_id and old_turn_id != new_turn_id
        session._state = "streaming"
        session._history[0]["content"] = "new owner prefix"
        old_context.run(old.close)
        assert old_context.run(get_approval_turn_id) == initial_turn_id
        assert new_context.run(get_approval_turn_id) == new_turn_id
        assert session._state == "streaming"
        assert session._history[0]["content"] == "new owner prefix"
        assert session._busy.locked()
    finally:
        old_context.run(old.close)
        new_context.run(new.close)
    assert new_context.run(get_approval_turn_id) == initial_turn_id
    assert get_approval_turn_id() == initial_turn_id


def fat_session(tmp_path, monkeypatch):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "summary")
    session = ConversationalSession(HarnessConfig(state_dir=str(tmp_path), max_context_tokens=4000))
    session._history = [{"role": "system", "content": "sys"}]
    for i in range(10):
        session._history.extend([
            {"role": "user", "content": f"User message {i}: " + "A" * 150},
            {"role": "assistant", "content": f"Assistant response {i}: " + "B" * 150},
        ])
    return session


@pytest.mark.parametrize("replace_pilot", [False, True])
def test_timed_out_summary_never_mutates_active_model(tmp_path, monkeypatch, replace_pilot):
    session = fat_session(tmp_path, monkeypatch)
    monkeypatch.setenv("HARNESS_COMPACTION_MODEL", "summary-model")
    entered, finish, done = threading.Event(), threading.Event(), threading.Event()
    seen = []

    class Pilot:
        def fork_for_compaction(self, *, model):
            from copy import copy
            local = copy(self)
            if model:
                local.model = model
            return local

        model = "original-model"

        def chat(self, messages, *, system):
            seen.append(self.model)
            entered.set()
            assert finish.wait(5)
            return DriverResponse(text="summary " * 50)

    pilot = Pilot()
    session.pilot = pilot
    real_thread = threading.Thread
    workers = []

    class TimedOutThread(real_thread):
        def run(self):
            try:
                super().run()
            finally:
                done.set()

        def join(self, timeout=None):
            workers.append(self)
            assert entered.wait(5)
            # Return while the summarizer is blocked, exactly as timeout does.

    monkeypatch.setattr("harness.compaction_mixin.threading.Thread", TimedOutThread)
    try:
        events = list(session._maybe_compact_history(force=True))
        during = pilot.model
        if replace_pilot:
            session.pilot = Pilot()
        session.pilot.model = "current-model"
    finally:
        finish.set()
        assert done.wait(5)
        for worker in workers:
            real_thread.join(worker, 5)
    assert seen == ["summary-model"]
    assert during == "original-model"
    assert session.pilot.model == "current-model"
    assert events[-1].kind == "compaction"


@pytest.mark.parametrize("raises", [False, True])
def test_archive_failure_preserves_history(tmp_path, monkeypatch, raises):
    session = fat_session(tmp_path, monkeypatch)
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "catalog")
    before = copy.deepcopy(session._history)

    def failed_write(*args):
        if raises:
            raise OSError("archive unavailable")
        return False

    monkeypatch.setattr("harness.compaction_archive.append_compaction_archive", failed_write)
    events = list(session._maybe_compact_history(force=True))
    assert session._history == before
    assert events[-1].kind == "compaction"
    assert events[-1].data["aborted"] is True
    assert events[-1].data["reason"] == "archive_failed"


def test_archive_filesystem_failure_preserves_history(tmp_path, monkeypatch):
    session = fat_session(tmp_path, monkeypatch)
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "catalog")
    before = copy.deepcopy(session._history)
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file blocks archive directory")
    session.state_dir = str(blocked)
    events = list(session._maybe_compact_history(force=True))
    assert session._history == before
    assert events[-1].data["reason"] == "archive_failed"
    assert events[-1].data["aborted"] is True


def test_optional_vault_failure_does_not_block_archived_compaction(tmp_path, monkeypatch):
    from harness.compaction_archive import load_compaction_archive_messages

    session = fat_session(tmp_path, monkeypatch)
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "catalog")

    def failed_index(*args):
        raise OSError("index unavailable")

    monkeypatch.setattr("harness.compaction_vault.index_elided_messages", failed_index)
    events = list(session._maybe_compact_history(force=True))
    assert events[-1].data.get("aborted") is not True
    assert session._history[1]["_compressed_summary"]
    assert load_compaction_archive_messages(session.state_dir, "default")


def test_reaper_publishes_idle_before_new_owner_can_enter(tmp_path, monkeypatch):
    session = session_at(tmp_path)
    session._busy.acquire()
    session._mark_busy_acquired()
    session._busy_since -= 2
    session._busy_last_progress = session._busy_since
    monkeypatch.setattr(session, "_turn_deadline_seconds", lambda: 1)
    released, finish = threading.Event(), threading.Event()
    real_meta = session._busy_meta
    reaper_thread = None

    class PausedExit:
        def __enter__(self):
            return real_meta.__enter__()

        def __exit__(self, *args):
            result = real_meta.__exit__(*args)
            if threading.current_thread() is reaper_thread:
                released.set()
                assert finish.wait(5)
            return result

    session._busy_meta = PausedExit()
    reaper_thread = threading.Thread(target=session._reap_stuck_turn)
    reaper_thread.start()
    try:
        assert released.wait(5)
        assert session._busy.acquire(blocking=False)
        new_gen = session._mark_busy_acquired()
        session._state = "streaming"
    finally:
        finish.set()
        reaper_thread.join(5)
    try:
        assert not reaper_thread.is_alive()
        assert session._state == "streaming"
    finally:
        session._release_busy(new_gen)
