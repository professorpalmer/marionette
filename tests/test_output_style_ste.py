"""Opt-in Simplified Technical English for the pilot and its workers."""
from __future__ import annotations

from harness.api.settings import post_settings
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.output_style import OUTPUT_STYLE_ENV, WORKER_OUTPUT_STYLE_ENV, output_style_turn_note

from tests.test_api_settings_peel import _svc


def _isolate(monkeypatch):
    # post_settings writes os.environ; set first so teardown restores the old values.
    monkeypatch.setenv(OUTPUT_STYLE_ENV, "off")
    monkeypatch.setenv(WORKER_OUTPUT_STYLE_ENV, "off")


def test_the_setting_turns_on_the_pilot_and_the_workers(monkeypatch):
    _isolate(monkeypatch)
    svc, _, _, calls = _svc()
    code, _ = post_settings({"outputStyle": "ste"}, svc)
    assert code == 200
    assert dict(calls["persist"]) == {OUTPUT_STYLE_ENV: "ste", WORKER_OUTPUT_STYLE_ENV: "ste"}
    note = output_style_turn_note()
    assert "OUTPUT STYLE (ste)" in note and "ASD-STE100" in note

    code, _ = post_settings({"outputStyle": "off"}, svc)
    assert code == 200
    assert output_style_turn_note() == ""


def test_an_unknown_style_is_refused(monkeypatch):
    _isolate(monkeypatch)
    svc, _, _, calls = _svc()
    code, payload = post_settings({"outputStyle": "pirate"}, svc)
    assert code == 400
    assert payload["error"] == "Invalid outputStyle"
    assert calls["persist"] == []


def _session(monkeypatch, tmp_path) -> ConversationalSession:
    class _Empty:
        def list(self, state=None):
            return []

        def render_block(self):
            return ""

    monkeypatch.setattr("harness.conversation.SkillStore", lambda *_a, **_k: _Empty())
    monkeypatch.setattr("harness.conversation.RuleStore", lambda *_a, **_k: _Empty())
    monkeypatch.setattr("harness.conversation.MemoryStore", lambda *_a, **_k: _Empty())
    monkeypatch.setattr("harness.plugin_registry.list_enabled_plugin_skills", lambda: [])
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path / "state"))
    cfg.repo = str(tmp_path)
    session = ConversationalSession(cfg)
    for name in ("_build_turn_cg_section", "_build_turn_wiki_section", "_build_turn_vault_section"):
        monkeypatch.setattr(session, name, lambda _msg: "")
    return session


def test_each_turn_carries_the_directive_once(monkeypatch, tmp_path):
    session = _session(monkeypatch, tmp_path)
    monkeypatch.setenv(OUTPUT_STYLE_ENV, "off")
    assert "OUTPUT STYLE" not in session._append_turn_context_trailer("hello", "fix the bug")

    monkeypatch.setenv(OUTPUT_STYLE_ENV, "ste")
    first = session._append_turn_context_trailer("hello", "fix the bug")
    assert "OUTPUT STYLE (ste)" in first
    session._history.append({"role": "user", "content": first})
    second = session._append_turn_context_trailer("again", "fix the next bug")
    assert "OUTPUT STYLE (ste)" not in second
    assert "[Output style context unchanged" in second
