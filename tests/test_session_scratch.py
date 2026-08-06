"""Session scratch bindings — durable L1, distinct from memory / history."""
from __future__ import annotations

import tempfile

from harness.config import HarnessConfig
from harness.session_scratch import (
    MAX_SCRATCH_KEYS,
    MAX_SCRATCH_TOTAL_CHARS,
    MAX_SCRATCH_VALUE_CHARS,
    ScratchStoreError,
    SessionScratchStore,
)


def test_scratch_roundtrip_save_load():
    state_dir = tempfile.mkdtemp()
    store = SessionScratchStore(state_dir)
    store.set("focus", "handle-first digests")
    assert store.get("focus") == "handle-first digests"
    reloaded = SessionScratchStore(state_dir)
    assert reloaded.get("focus") == "handle-first digests"


def test_scratch_list_delete_clear():
    state_dir = tempfile.mkdtemp()
    store = SessionScratchStore(state_dir)
    store.set("a", "1")
    store.set("b", "22")
    rows = store.list()
    assert rows == [("a", 1), ("b", 2)]
    assert store.delete("a") is True
    assert store.get("a") is None
    assert store.clear() == 1
    assert store.list() == []


def test_scratch_caps_reject_oversized():
    state_dir = tempfile.mkdtemp()
    store = SessionScratchStore(state_dir)
    try:
        store.set("big", "x" * (MAX_SCRATCH_VALUE_CHARS + 1))
        assert False, "expected ScratchStoreError"
    except ScratchStoreError as exc:
        assert "exceeds" in str(exc)

    for i in range(MAX_SCRATCH_KEYS):
        store.set(f"k{i}", "v")
    try:
        store.set("overflow", "v")
        assert False, "expected ScratchStoreError"
    except ScratchStoreError as exc:
        assert "full" in str(exc)


def test_scratch_total_chars_cap():
    state_dir = tempfile.mkdtemp()
    store = SessionScratchStore(state_dir)
    # Leave headroom for key lengths; next max-sized value must trip the total cap.
    per = MAX_SCRATCH_VALUE_CHARS
    store.set("a", "y" * per)
    store.set("b", "y" * per)
    store.set("c", "y" * per)
    try:
        store.set("overflow", "z" * per)
        assert False, "expected ScratchStoreError"
    except ScratchStoreError as exc:
        assert "total chars" in str(exc)
    assert MAX_SCRATCH_TOTAL_CHARS >= per * 3


def test_scratch_survives_mock_compact_not_in_history(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "harness.conversation.RuleStore",
        lambda *a, **k: __import__("harness.rule_store", fromlist=["RuleStore"]).RuleStore(
            path=str(tmp_path / "rules.json")
        ),
    )
    monkeypatch.setattr("harness.memory_store.MEMORY_PATH", tmp_path / "mem.json")
    state_dir = tempfile.mkdtemp()
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=state_dir)
    from harness.conversation import ConversationalSession

    session = ConversationalSession(cfg)
    session._scratch_store.set("note", "survives compact")
    # Mock compact: wipe live residual history the way compaction would.
    session._history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert "survives compact" not in str(session._history)
    assert session._scratch_store.get("note") == "survives compact"

    session2 = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", state_dir=state_dir))
    assert session2._scratch_store.get("note") == "survives compact"


def test_scratch_tools_via_dispatch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "harness.conversation.RuleStore",
        lambda *a, **k: __import__("harness.rule_store", fromlist=["RuleStore"]).RuleStore(
            path=str(tmp_path / "rules.json")
        ),
    )
    monkeypatch.setattr("harness.memory_store.MEMORY_PATH", tmp_path / "mem.json")
    state_dir = tempfile.mkdtemp()
    from harness.conversation import ConversationalSession
    from harness.pilot import PilotAction
    from harness.send_loop_phases import dispatch_local_action

    session = ConversationalSession(
        HarnessConfig(driver="stub-oracle-v2", state_dir=state_dir)
    )
    session._append_action_result = lambda *a, **k: None  # type: ignore

    act = PilotAction(
        kind="store_scratch",
        path="k1",
        content="v1",
        arguments={"key": "k1", "value": "v1"},
    )
    events = list(dispatch_local_action(session, act, "a1", True, []))
    assert events[0].data.get("error") is None
    ok, status, val = session._do_load_scratch(
        PilotAction(kind="load_scratch", path="k1", arguments={"key": "k1"})
    )
    assert ok and status == "success" and val == "v1"
    ok, status, listed = session._do_list_scratch(PilotAction(kind="list_scratch"))
    assert ok and "k1" in listed
    ok, status, cleared = session._do_clear_scratch(PilotAction(kind="clear_scratch"))
    assert ok and "1" in cleared
