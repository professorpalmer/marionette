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
