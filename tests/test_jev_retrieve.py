from __future__ import annotations

from types import SimpleNamespace

from harness.jev.judge import Judgment, clear_cache
from harness.jev.retrieve import select_turn_skills
from harness.skill_retrieve import format_retrieved_skill_bodies


def _skill(name, description, body):
    return SimpleNamespace(name=name, description=description, body=body, slug=name)


def test_retrieve_jev_named_skill(monkeypatch):
    monkeypatch.setenv("HARNESS_JEV", "1")
    monkeypatch.setattr("harness.jev.client.resolve_openrouter_key", lambda: "sk-test")
    router = _skill("audit-the-router", "How to audit routing decisions.", "ROUTER_BODY")
    wrap = _skill("session-wrap", "End of session wrap.", "WRAP_BODY")
    judged = Judgment(skill="session-wrap", skill_fits=0.9, gate=0.8, depth="STANDARD")
    picked, out = select_turn_skills(
        "please audit routing",
        [router, wrap],
        judgment=judged,
    )
    assert picked == [wrap]
    assert out.skill == "session-wrap"
    assert "WRAP_BODY" in format_retrieved_skill_bodies(picked)
    assert "ROUTER_BODY" not in format_retrieved_skill_bodies(picked)


def test_retrieve_jev_none_beats_overlap(monkeypatch):
    monkeypatch.setenv("HARNESS_JEV", "1")
    monkeypatch.setattr("harness.jev.client.resolve_openrouter_key", lambda: "sk-test")
    skill = _skill("technical-writing", "Write the README.", "DO_NOT_LOAD")
    judged = Judgment(skill=None, gate=0.4, depth="MICRO", lane="inline", playbook="none")
    picked, out = select_turn_skills("typo in README.md", [skill], judgment=judged)
    assert picked == []
    assert out.depth == "MICRO"


def test_retrieve_disabled_falls_back_to_overlap(monkeypatch):
    monkeypatch.setenv("HARNESS_JEV", "0")
    skill = _skill("audit-the-router", "How to audit routing decisions.", "ROUTER_BODY")
    picked, judged = select_turn_skills("please audit routing", [skill])
    assert picked == [skill]
    assert judged.skill is None


def test_retrieve_unset_with_key_still_uses_overlap(monkeypatch):
    monkeypatch.delenv("HARNESS_JEV", raising=False)
    monkeypatch.setattr("harness.jev.client.resolve_openrouter_key", lambda: "sk-test")

    def boom(*_a, **_k):
        raise AssertionError("Jev must stay dark without opt-in")

    monkeypatch.setattr("harness.jev.retrieve.judge_turn", boom)
    skill = _skill("audit-the-router", "How to audit routing decisions.", "ROUTER_BODY")
    picked, judged = select_turn_skills("please audit routing", [skill])
    assert picked == [skill]
    assert judged.skill is None


def test_retrieve_error_falls_back_to_overlap(monkeypatch):
    monkeypatch.setenv("HARNESS_JEV", "1")
    monkeypatch.setattr("harness.jev.client.resolve_openrouter_key", lambda: "sk-test")

    def boom(*_a, **_k):
        raise RuntimeError("jev down")

    monkeypatch.setattr("harness.jev.retrieve.judge_turn", boom)
    skill = _skill("audit-the-router", "How to audit routing decisions.", "ROUTER_BODY")
    picked, _judged = select_turn_skills("please audit routing", [skill])
    assert picked == [skill]


def test_trailer_jev_named_skill_not_overlap(monkeypatch, tmp_path):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    monkeypatch.setenv("HARNESS_JEV", "1")
    monkeypatch.setattr("harness.jev.client.resolve_openrouter_key", lambda: "sk-test")
    router = _skill("audit-the-router", "How to audit routing decisions.", "ROUTER_BODY_UNIQUE")
    wrap = _skill("session-wrap", "End of session wrap.", "WRAP_BODY_UNIQUE")

    class _Store:
        def list(self, state=None):
            return [router, wrap] if state == "active" else []

    monkeypatch.setattr("harness.conversation.SkillStore", lambda *_a, **_k: _Store())
    monkeypatch.setattr("harness.plugin_registry.list_enabled_plugin_skills", lambda: [])

    class _Empty:
        def list(self, state=None):
            return []

        def render_block(self):
            return ""

    monkeypatch.setattr("harness.conversation.RuleStore", lambda *_a, **_k: _Empty())
    monkeypatch.setattr("harness.conversation.MemoryStore", lambda *_a, **_k: _Empty())
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path / "state"))
    cfg.repo = str(tmp_path)
    session = ConversationalSession(cfg)
    monkeypatch.setattr(session, "_build_turn_cg_section", lambda _msg: "")
    monkeypatch.setattr(session, "_build_turn_wiki_section", lambda _msg: "")
    monkeypatch.setattr(session, "_build_turn_vault_section", lambda _msg: "")
    session._jev_judgment = Judgment(
        skill="session-wrap", skill_fits=0.92, gate=0.7, depth="STANDARD", lane="skill",
    )
    out = session._append_turn_context_trailer("hello", "please audit routing")
    assert "WRAP_BODY_UNIQUE" in out
    assert "ROUTER_BODY_UNIQUE" not in out
    assert "session-wrap" in out


def test_clear_cache_isolated():
    clear_cache()
