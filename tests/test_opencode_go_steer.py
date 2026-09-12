"""OpenCode Go steer: publication must survive compaction vs stale checkpoints.

A live pentest-playbook session (2026-09-12) showed mid-turn steer as
``[error] The native transcript contains history absent from this runner``.
The steer receipt later reconciled; the follow-up send published. Disk had
already been compacted (``_compressed_summary``, generation 7) while SSE
checkpoints can still write a full pre-compact snapshot taken without
``_busy_meta``. OpenCode Go hits this constantly: huge reasoning transcripts
and advisor-``now`` auto-compact.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.input_receipts import InputReceiptError, session_input_store
from harness.sessions import (
    _write_transcript,
    load_transcript,
    persist_live_transcript,
    save_transcript,
)
from pmharness.drivers.codex_responses import _messages_to_responses_input


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr("harness.conversation.prov.build_pilot", lambda *_: SimpleNamespace())
    for name in ("SkillStore", "RuleStore"):
        monkeypatch.setattr(
            "harness.conversation." + name,
            lambda *_a, **_k: SimpleNamespace(list=lambda *_: []),
        )
    monkeypatch.setattr(
        "harness.conversation.MemoryStore",
        lambda *_a, **_k: SimpleNamespace(render_block=lambda: ""),
    )
    monkeypatch.setattr(
        "harness.conversation.WikiClient",
        lambda *_a, **_k: SimpleNamespace(configured=False),
    )
    monkeypatch.setattr("harness.plugin_registry.list_enabled_plugin_skills", lambda *_a, **_k: [])
    monkeypatch.setattr("harness.browser_auth.ensure_shared_browser_env", lambda: {})
    s = ConversationalSession(HarnessConfig(state_dir=str(tmp_path), repo="", swarm_adapter="demo"))
    s.harness_session_id = "opencode-steer"
    s.bind_prompt_queue(str(tmp_path), s.harness_session_id)
    return s


def _fat_opencode_history(session):
    """Reasoning-heavy Go turn: DeepSeek-shaped rows plus a live tail."""
    session._history.extend([
        {"role": "user", "content": "earlier input", "input_ids": ["seed-user"]},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "opaque reasoning " * 40,
            "tool_calls": [{
                "id": "call_00_SI8alDtCgQfC1ICJTAd92436",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_00_SI8alDtCgQfC1ICJTAd92436",
            "content": "tool result",
        },
        {
            "role": "assistant",
            "content": "Shipped. But you should know how it landed.",
            "reasoning_content": "final reasoning",
        },
    ])


def _compact_runner(session):
    sysmsg = session._history[0]
    tail = [m for m in session._history[1:] if not m.get("_compressed_summary")][-2:]
    session._history[:] = [
        sysmsg,
        {
            "role": "user",
            "content": "[Earlier conversation summarized to fit context]",
            "_compressed_summary": True,
        },
        *tail,
    ]


def _deliver_steer_without_persist(session, text):
    """Admit + append like inject, but skip the live persist seam."""
    session.enqueue_steer(text)
    actions = session._drain_steer_actions()
    assert actions
    contents = [session._format_steer_user_content(action.text) for action in actions]
    for action, content in zip(actions, contents):
        session._history.append({"role": "user", "content": content, "input_id": action.id})
    return [action.id for action in actions]


def test_stale_full_disk_blocks_publish_without_persist(session):
    """Receipt layer stays fail-closed: compacted export vs fat disk."""
    _fat_opencode_history(session)
    save_transcript(session.state_dir, session.harness_session_id, session.export_transcript_data())
    _compact_runner(session)
    input_ids = _deliver_steer_without_persist(session, "grab em, adapt em, ship it all. thanks")
    with pytest.raises(InputReceiptError) as failure:
        session_input_store(session).publish_injected(
            input_ids, session.export_transcript_data(),
        )
    assert failure.value.code == "input_publication_conflict"
    assert "absent from this runner" in str(failure.value)


def test_steer_inject_syncs_disk_after_in_memory_compact(session):
    """Screenshot path on 476: compact in memory, then steer, no extra persist."""
    _fat_opencode_history(session)
    save_transcript(session.state_dir, session.harness_session_id, session.export_transcript_data())
    _compact_runner(session)
    session.enqueue_steer("why are these failing? we need to understand why. is it opencode go issue?")
    events = list(session._check_and_inject_steer())
    assert [e.kind for e in events] == ["steer"]
    assert all(r["status"] == "injected" for r in session.input_receipts())
    disk = load_transcript(session.state_dir, session.harness_session_id)
    assert disk["history"][0].get("_compressed_summary") is True
    assert "is it opencode go issue" in disk["history"][-1]["content"]


def test_steer_survives_sanitize_recast_of_orphan_tool(session):
    """Failed weave leaves an orphan tool row on disk; export recasts it."""
    session._history.extend([
        {"role": "user", "content": "pin the weave", "input_ids": ["seed-user"]},
        {"role": "assistant", "content": "running swarm"},
        {"role": "tool", "tool_call_id": "call_orphan_weave", "content": "incomplete tasks"},
        {
            "role": "assistant",
            "content": "The pinned weave failed — swarm exited with incomplete tasks.",
        },
    ])
    _write_transcript(session.state_dir, session.harness_session_id, {
        "history": [row for row in session._history if row.get("role") != "system"],
        "display": [],
        "job_ids": [],
    })
    session.enqueue_steer("why are these failing? we need to understand why. is it opencode go issue?")
    events = list(session._check_and_inject_steer())
    assert [e.kind for e in events] == ["steer"]
    assert all(r["status"] == "injected" for r in session.input_receipts())


def test_locked_persist_lets_opencode_steer_publish_after_compact(session):
    _fat_opencode_history(session)
    save_transcript(session.state_dir, session.harness_session_id, session.export_transcript_data())
    _compact_runner(session)
    persist_live_transcript(session, session.state_dir, session.harness_session_id)
    session.enqueue_steer("grab em, adapt em, ship it all. thanks")
    events = list(session._check_and_inject_steer())
    assert [e.kind for e in events] == ["steer"]
    assert all(r["status"] == "injected" for r in session.input_receipts())
    disk = load_transcript(session.state_dir, session.harness_session_id)
    assert disk["history"][0].get("_compressed_summary") is True
    assert disk["history"][-1]["content"].startswith("grab em, adapt em, ship it all. thanks")


def test_persist_live_transcript_exports_after_busy_meta(session):
    _fat_opencode_history(session)
    save_transcript(session.state_dir, session.harness_session_id, session.export_transcript_data())
    holding = threading.Event()
    done = threading.Event()

    def compact_under_lock():
        with session._busy_meta:
            holding.set()
            assert done.wait(2)
            _compact_runner(session)

    worker = threading.Thread(target=compact_under_lock)
    worker.start()
    assert holding.wait(2)
    persist = threading.Thread(
        target=persist_live_transcript,
        args=(session, session.state_dir, session.harness_session_id),
    )
    persist.start()
    time.sleep(0.05)
    assert persist.is_alive()
    done.set()
    worker.join(2)
    persist.join(2)
    assert not persist.is_alive()
    disk = load_transcript(session.state_dir, session.harness_session_id)
    assert disk["history"][0].get("_compressed_summary") is True


def test_fat_go_persist_then_list_does_not_kill_live_publish(session):
    """Session 574a8561f4f0: one long Go thread, not every send.

    persist_live_transcript writes the new input_id before publish_injected.
    Hydrate list() used to reconcile that same-instance delivering row and
    the next publish raised ``Input has no current delivery attempt.``
    Other sends on the same session that won the race stayed fine.
    """
    from harness.input_receipts import publish_session_injected, session_input_store

    _fat_opencode_history(session)
    save_transcript(session.state_dir, session.harness_session_id, session.export_transcript_data())
    store = session_input_store(session)
    row = store.admit(
        "no I will handle the fixes separately go ahead and do the next audit "
        "of the harness looking for improvements optimizations bugs breaks Etc "
        "as we wereuse the open router deepseek V 4.1"
    )
    store.prepare_delivery(row["id"])
    session._history.append({
        "role": "user",
        "content": row["original_text"],
        "input_id": row["id"],
    })
    persist_live_transcript(session, session.state_dir, session.harness_session_id)
    listed = next(r for r in store.list() if r["id"] == row["id"])
    assert listed["status"] == "delivering"
    publish_session_injected(session, [row["id"]])
    published = next(r for r in store.list() if r["id"] == row["id"])
    assert published["status"] == "injected"
    assert published.get("reason") != "exact_native_input_id_reconciled"


def test_opencode_responses_input_keeps_steer_after_assistant():
    """Go Responses / Grok / Luna must replay the injected user row."""
    inp = _messages_to_responses_input([
        {"role": "user", "content": "audit the kit"},
        {
            "role": "assistant",
            "content": "Shipped.",
            "reasoning_content": "thinking",
            "phase": "final_answer",
        },
        {"role": "user", "content": "grab em, adapt em, ship it all. thanks"},
    ])
    users = [row for row in inp if row.get("type") == "message" and row.get("role") == "user"]
    assert len(users) == 2
    assert users[-1]["content"][0]["text"].startswith("grab em, adapt em")
