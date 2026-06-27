import os
import pytest
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, ConvEvent
from harness.skill_store import SkillStore, Skill
from harness.rule_store import RuleStore, Rule
from harness.skill_distiller import distill_session, distill_rules


class FakePilot:
    def __init__(self, responses):
        self.responses = responses if isinstance(responses, list) else [responses]
        self.call_count = 0

    def complete(self, prompt, *, system=None):
        class R:
            def __init__(self, text):
                self.text = text
        text = self.responses[self.call_count % len(self.responses)]
        self.call_count += 1
        return R(text)


def test_auto_distill_default_on(monkeypatch):
    # Test default when env is empty
    monkeypatch.delenv("HARNESS_AUTO_DISTILL", raising=False)
    config = HarnessConfig()
    session = ConversationalSession(config)
    assert session._auto_distill is True

    # Test falsey env values
    for val in ("0", "false", "no", "FALSE", "No"):
        monkeypatch.setenv("HARNESS_AUTO_DISTILL", val)
        session = ConversationalSession(config)
        assert session._auto_distill is False

    # Test truthy env values
    for val in ("1", "true", "yes", "TRUE", "Yes"):
        monkeypatch.setenv("HARNESS_AUTO_DISTILL", val)
        session = ConversationalSession(config)
        assert session._auto_distill is True


def test_hard_task_trigger(tmp_path):
    config = HarnessConfig()
    config.repo = str(tmp_path)
    session = ConversationalSession(config)
    session._skills = SkillStore(root=str(tmp_path / "skills"))
    session._rules = RuleStore(path=str(tmp_path / "rules.json"))

    # Less than MIN_FINDINGS findings
    session._session_findings = [{"type": "finding", "headline": "Only one finding"}]
    session._first_objective = "Fix critical memory leak"
    
    # Simulate high tool call count
    session._total_tool_calls = 8
    
    # Seed display transcript
    session._display_transcript = [
        {"role": "user", "text": "Fix critical memory leak"},
        {"role": "assistant", "text": "I analyzed memory usage and found a leaking dictionary. I cleared it."}
    ]

    # Pilot returns valid candidate JSON
    session.pilot = FakePilot('{"name":"Clear dict leak","description":"When dictionaries grow unbounded","body":"1. inspect dict usage\\n2. clear dict reference"}')

    res = session.distill()
    assert res["skill"]["status"] == "proposed"
    
    # Ensure it was proposed as PENDING (human gate holds)
    skill = session._skills.get(res["skill"]["slug"])
    assert skill is not None
    assert skill.state == "pending"
    assert "inspect dict usage" in skill.body


def test_duplicate_with_new_info_proposes_patch(tmp_path):
    store = SkillStore(root=str(tmp_path / "skills"))
    
    # Save active skill
    existing = Skill(
        name="SSE Fix",
        description="Fixes SSE issues",
        body="1. check flush",
        state="active"
    )
    store.save(existing)
    
    # 1. Candidate is Jaccard similar to existing skill
    # 2. Pilot classifies it as an "update" with a merged body
    pilot = FakePilot([
        # Response for distill_session's original complete call
        '{"name":"SSE Fix","description":"Fixes SSE issues","body":"1. check flush\\n2. check content-type"}',
        # Response for _classify_candidate complete call
        '{"verdict":"update","slug":"sse-fix","merged_body":"1. check flush\\n2. check content-type"}'
    ])
    
    findings = [
        {"type": "finding", "headline": "SSE needs flush"},
        {"type": "finding", "headline": "content-type must be set"}
    ]
    
    r = distill_session(pilot, "sse task", findings, store)
    assert r["status"] == "patch_proposed"
    assert r["supersedes"] == "sse-fix"
    assert r["slug"] == "sse-fix-patch"
    
    # Ensure the patch skill was stored in PENDING
    patch_skill = store.get("sse-fix-patch")
    assert patch_skill is not None
    assert patch_skill.state == "pending"
    assert "check content-type" in patch_skill.body
    
    # Approve the patch skill and ensure it updates the active skill and unlinks the patch skill
    approved = store.set_state("sse-fix-patch", "active")
    assert approved is not None
    assert approved.slug == "sse-fix"
    assert approved.state == "active"
    assert "check content-type" in approved.body
    
    # Original active skill is updated, patch skill is deleted
    assert store.get("sse-fix-patch") is None
    assert store.get("sse-fix").body == "1. check flush\n2. check content-type"


def test_corrections_feed_distill_rules(tmp_path):
    rule_store = RuleStore(path=str(tmp_path / "rules.json"))
    
    pilot = FakePilot('{"rules": [{"text": "never use emojis in output", "scope": "global"}]}')
    
    # No findings, but we have user corrections
    findings = []
    corrections = ["don't use emojis", "stop using emojis"]
    
    r = distill_rules(pilot, "no emoji task", findings, rule_store, corrections=corrections)
    assert r["status"] == "proposed"
    assert "never-use-emojis-in-output" in r["proposed"]
    
    # Ensure human gate holds (proposed rule is pending)
    rules = rule_store.list()
    rule = next((r for r in rules if r.slug == "never-use-emojis-in-output"), None)
    assert rule is not None
    assert rule.state == "pending"
    assert rule.text == "never use emojis in output"


def test_human_gate_holds_all(tmp_path):
    # Propose skill/rule and confirm they are pending
    skill_store = SkillStore(root=str(tmp_path / "skills"))
    rule_store = RuleStore(path=str(tmp_path / "rules.json"))
    
    pilot = FakePilot([
        '{"name":"New Procedure","description":"New description","body":"New body"}',
        '{"rules": [{"text": "never edit without verifying", "scope": "global"}]}'
    ])
    
    findings = [
        {"type": "finding", "headline": "step one done"},
        {"type": "finding", "headline": "step two done"}
    ]
    
    skill_res = distill_session(pilot, "task", findings, skill_store)
    rule_res = distill_rules(pilot, "task", findings, rule_store)
    
    assert skill_res["status"] == "proposed"
    assert rule_res["status"] == "proposed"
    
    skill = skill_store.get(skill_res["slug"])
    assert skill.state == "pending"
    
    rules = rule_store.list()
    rule = next((r for r in rules if r.slug == rule_res["proposed"][0]), None)
    assert rule is not None
    assert rule.state == "pending"


def test_repeated_patch_no_slug_growth(tmp_path):
    """Regression: repeatedly patching the same skill must not chain the slug
    (foo-patch-patch-patch...) into an OS filename-length crash. Found by stress
    test. The patch slug must stay stable ({root}-patch) and supersede the root."""
    from harness.skill_store import SkillStore, Skill
    store = SkillStore(root=str(tmp_path / "skills"))
    store.save(Skill(name="Deploy flow", description="how to deploy",
                     body="v1", state="active"))
    root = store.list("active")[0].slug
    # 50 repeated patches of the same skill - must not crash or multiply
    for n in range(50):
        store.propose_update(root, f"body v{n + 2}", source="distilled:patch")
    pending = store.list("pending")
    assert len(pending) == 1, f"expected 1 stable pending patch, got {len(pending)}"
    # the patch supersedes the ROOT, not a chained patch slug
    assert pending[0].supersedes == root
    assert "-patch-patch" not in pending[0].slug
    # approving merges into the original and removes the patch
    approved = store.set_state(pending[0].slug, "active")
    assert approved is not None
    assert len([s for s in store.list("active") if s.name == "Deploy flow"]) == 1


def test_distinct_skills_not_destructively_merged(tmp_path):
    """Regression: borderline-similar but DISTINCT skills (same domain, different
    verb ~= 0.6 similarity) must be proposed as NEW pending skills, not merged
    into one (which destroys knowledge)."""
    from harness.skill_store import SkillStore, Skill
    from harness.skill_distiller import distill_session

    store = SkillStore(root=str(tmp_path / "skills"))
    f = [{"type": "finding", "headline": "a"}, {"type": "finding", "headline": "b"}]
    import json
    
    # First skill: no shortlist exists yet, so no classify call
    p1 = FakePilot(json.dumps({"name": "Trace SSE websocket handshake",
                               "description": "trace the SSE websocket handshake",
                               "body": "x"}))
    r1 = distill_session(p1, "o", f, store)
    assert r1["status"] == "proposed"
    
    # Second skill: shortlist exists containing first skill. Classify is called and returns "new".
    p2 = FakePilot([
        json.dumps({"name": "Debug SSE websocket handshake",
                    "description": "debug the SSE websocket handshake",
                    "body": "y"}),
        '{"verdict":"new"}'
    ])
    r2 = distill_session(p2, "o", f, store)
    
    assert r2["status"] == "proposed"
    assert len(store.list("pending")) == 2


def test_classify_fallback_proposes_new(tmp_path):
    """An unparseable/garbage classify response falls back to 'new' (proposes), never crashes."""
    from harness.skill_store import SkillStore, Skill
    from harness.skill_distiller import distill_session
    
    store = SkillStore(root=str(tmp_path / "skills"))
    # Save an active skill to force a shortlist match and a classify call
    store.save(Skill(name="Deploy flow", description="how to deploy", body="v1", state="active"))
    
    p = FakePilot([
        '{"name":"Deploy flow","description":"how to deploy","body":"v2"}',
        'this is garbage { unclosed'
    ])
    
    findings = [{"type": "finding", "headline": "a"}, {"type": "finding", "headline": "b"}]
    r = distill_session(p, "obj", findings, store)
    assert r["status"] == "proposed"
    # It proposed a new skill instead of patching or crashing
    assert len(store.list("pending")) == 1
