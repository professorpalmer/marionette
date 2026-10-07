"""Pilot identity system note — models must be able to name themselves."""

from __future__ import annotations

from types import SimpleNamespace

from harness.conversation import (
    ConversationalSession,
    _friendly_pilot_model_name,
)


def test_friendly_luna_name():
    assert _friendly_pilot_model_name("gpt-5.6-luna") == "Luna 5.6"
    assert _friendly_pilot_model_name("gpt-5.6-luna-pro") == "Luna 5.6 Pro"
    assert _friendly_pilot_model_name("openai/gpt-5.6-sol") == "Sol 5.6"


def test_pilot_identity_note_names_luna():
    sess = ConversationalSession.__new__(ConversationalSession)
    sess.config = SimpleNamespace(driver="openai-codex:gpt-5.6-luna")
    sess.pilot = SimpleNamespace(model="gpt-5.6-luna")
    note = ConversationalSession._pilot_identity_system_note(sess)
    assert "gpt-5.6-luna" in note
    assert "Luna 5.6" in note
    assert "authoritative" in note.lower()


def test_refreeze_after_compaction_and_pilot_change_keeps_one_identity(tmp_path):
    from harness.config import HarnessConfig

    sess = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path)))
    base = sess._history[0]["content"]
    first = sess._ensure_frozen_system_prompt(base)
    assert first.count("PILOT IDENTITY") == 1

    # Compaction drops the freeze while history[0] still holds the frozen
    # prompt; a pilot replacement carries that history over the same way.
    sess._reset_append_only_freeze()
    sess.config.driver = "openai-codex:gpt-5.6-luna"
    second = sess._ensure_frozen_system_prompt(sess._history[0]["content"])
    assert second.count("PILOT IDENTITY") == 1
    assert "gpt-5.6-luna" in second
    assert "stub-oracle-v2" not in second.split("PILOT IDENTITY", 1)[1].split("\n\n")[0]
    assert second.startswith(base)


def test_static_system_base_strips_legacy_identity_block():
    from harness.conversation import static_system_base

    legacy = "BASE\n\nPILOT IDENTITY (authoritative for this session):\n- old\n\nMCP stuff"
    assert static_system_base(legacy) == "BASE"
