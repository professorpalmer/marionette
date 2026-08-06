"""Continual harness REFINE — propose cards with snapshot/rollback."""
from __future__ import annotations

import tempfile

from harness.config import HarnessConfig
from harness.harness_refine import get_refine_controller
from harness.rule_store import RuleStore


def _session(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "harness.memory_store.MEMORY_PATH",
        tmp_path / "memory.json",
    )
    monkeypatch.setattr(
        "harness.conversation.RuleStore",
        lambda *a, **k: RuleStore(path=str(tmp_path / "rules.json")),
    )
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    from harness.conversation import ConversationalSession

    return ConversationalSession(cfg)


def test_refine_propose_requires_accept(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch)
    ctrl = get_refine_controller(session)
    prop = ctrl.propose(kind="memory", text="Prefer dark terminals", scope="global")
    assert prop is not None
    assert len(session._memory.list()) == 0
    accepted = ctrl.accept(prop.id)
    assert accepted["ok"] is True
    assert len(session._memory.list()) == 1
    assert session._memory.list()[0].text == "Prefer dark terminals"


def test_refine_never_mutates_frozen_system_prompt(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch)
    session._frozen_system_prompt = "FROZEN BASE PROMPT"
    ctrl = get_refine_controller(session)
    # Explicitly refuse system_prompt kind.
    assert ctrl.propose(kind="system_prompt", text="hijack") is None
    prop = ctrl.propose(kind="memory", text="safe fact", scope="global")
    assert prop is not None
    ctrl.accept(prop.id)
    assert session._frozen_system_prompt == "FROZEN BASE PROMPT"


def test_refine_local_vs_global(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch)
    ctrl = get_refine_controller(session)
    local = ctrl.propose(kind="memory", text="local-only note", scope="local")
    global_p = ctrl.propose(kind="memory", text="global note", scope="global")
    assert local and global_p
    ctrl.accept(local.id)
    ctrl.accept(global_p.id)
    assert any(e.get("text") == "local-only note" for e in ctrl.local.list("memory"))
    assert any(e.text == "global note" for e in session._memory.list())
    assert not any(e.text == "local-only note" for e in session._memory.list())


def test_refine_snapshot_rollback(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch)
    ctrl = get_refine_controller(session)
    prop = ctrl.propose(kind="memory", text="rollback me", scope="global")
    assert prop is not None
    assert ctrl.accept(prop.id)["ok"]
    assert len(session._memory.list()) == 1
    rolled = ctrl.rollback()
    assert rolled["ok"] is True
    assert len(session._memory.list()) == 0


def test_refine_skipped_in_autopilot(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch)
    session._auto_mode = True
    ctrl = get_refine_controller(session)
    assert ctrl.propose(kind="rule", text="never in auto", scope="global") is None
    session._turn_refine_queue = [
        {"kind": "memory", "text": "queued", "scope": "global"},
    ]
    assert ctrl.flush_queued() == []
    assert session._turn_refine_queue == []
