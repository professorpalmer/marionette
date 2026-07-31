"""Distilled knowledge is methodology, never current evidence.

Skills and promoted memory exist so a run does not re-derive method from
scratch. Injected as bare prose, though, they read as established fact: a worker
(or the pilot) restates a remembered issue in the present tense with no dispatch
behind it, and a reader cannot tell the difference from a fresh finding. Both
prompt surfaces therefore have to say what that content is for, and what it is
not.
"""

from __future__ import annotations

import tempfile

from types import SimpleNamespace

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.repo_resolve import resolve_effective_repo
from pmharness.bridge import _analysis_instruction

_EXPLICIT_SUBJECT_REPO = "/repo/subject"
_RESOLVED_SUBJECT_REPO = resolve_effective_repo(_EXPLICIT_SUBJECT_REPO)


def _system_prompt(session) -> str:
    return session._history[0]["content"]


class TestWorkerInstruction:
    def _instruction(self, **kwargs):
        return _analysis_instruction(
            "audit the router", _EXPLICIT_SUBJECT_REPO, "explore", **kwargs,
        )

    def test_names_skills_memory_and_prior_transcripts_as_context_only(self):
        text = self._instruction().lower()
        for source in ("active skills", "distilled memory", "prior transcript"):
            assert source in text
        assert "methodology and context only" in text
        assert "never current findings" in text

    def test_requires_this_dispatch_as_the_basis_for_claims(self):
        text = self._instruction().lower()
        assert "this dispatch" in text
        assert "path:line" in text
        assert "acceptance criterion" in text
        assert "it is not proof by itself" in text

    def test_an_unrunnable_check_is_not_verified_not_a_defect(self):
        text = self._instruction().lower()
        assert "not_verified" in text
        assert "do not report it as a defect" in text

    def test_the_rule_reaches_the_browser_variant_too(self):
        assert "CURRENT-DISPATCH EVIDENCE RULE" in self._instruction(browser=True)

    def test_the_rule_reaches_the_native_worker_variant_too(self):
        assert "CURRENT-DISPATCH EVIDENCE RULE" in self._instruction(via_tool=False)

    def test_the_rule_does_not_displace_the_submit_contract(self):
        text = self._instruction()
        assert "READ-ONLY" in text
        assert _RESOLVED_SUBJECT_REPO in text

    def test_emits_pm_parseable_acceptance_criteria_block(self):
        text = self._instruction(acceptance_criteria=["pyright is clean"])
        assert "Acceptance criteria:\n- pyright is clean" in text
        assert "do not invent extras" not in text.lower()


class TestPilotSystemPrompt:
    def _session_with_active_skill(self, monkeypatch, tmp_path):
        skill = SimpleNamespace(
            name="audit-the-router",
            description="How to audit routing decisions.",
            body="Start from harness/router.py and follow the receipt.",
        )

        class _OneSkillStore:
            def list(self, state=None):
                return [skill] if state == "active" else []

        monkeypatch.setattr(
            "harness.conversation.SkillStore", lambda *_a, **_k: _OneSkillStore(),
        )
        cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
        cfg.repo = str(tmp_path)
        return ConversationalSession(cfg)

    def test_active_skills_still_load(self, monkeypatch, tmp_path):
        system = _system_prompt(self._session_with_active_skill(monkeypatch, tmp_path))
        assert "audit-the-router" in system
        assert "Start from harness/router.py" in system

    def test_active_skills_are_framed_as_method_not_evidence(
        self, monkeypatch, tmp_path,
    ):
        system = _system_prompt(self._session_with_active_skill(monkeypatch, tmp_path))
        assert "METHOD ONLY" in system
        assert "never current findings" in system
        assert "re-verify" in system
        assert "not verified" in system

    def test_no_active_skills_adds_no_framing(self, monkeypatch, tmp_path):
        class _EmptyStore:
            def list(self, state=None):
                return []

        monkeypatch.setattr(
            "harness.conversation.SkillStore", lambda *_a, **_k: _EmptyStore(),
        )
        cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
        cfg.repo = str(tmp_path)
        system = _system_prompt(ConversationalSession(cfg))
        assert "METHOD ONLY" not in system

    def test_durable_memory_is_context_not_current_evidence(
        self, monkeypatch, tmp_path,
    ):
        class _Memory:
            def render_block(self):
                return "# Durable memory\nThe router used to drop alternatives."

        monkeypatch.setattr("harness.conversation.MemoryStore", _Memory)
        cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
        cfg.repo = str(tmp_path)
        system = _system_prompt(ConversationalSession(cfg))
        assert "CONTEXT ONLY" in system
        assert "never current execution evidence" in system
        assert "not verified" in system
