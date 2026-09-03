from __future__ import annotations

"""Skill catalog stays in the frozen prefix; bodies arrive only on retrieve."""

import tempfile
from types import SimpleNamespace

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.prompt_cache_scope import (
    clear_stable_prefixes,
    find_stable_prefix,
    prompt_cache_scope,
    register_stable_prefix,
)
from harness.skill_retrieve import (
    SkillCatalogLine,
    catalog_line,
    format_retrieved_skill_bodies,
    format_skill_catalog,
    select_skill_bodies,
)


def _skill(name, description, body, slug=None):
    return SimpleNamespace(
        name=name,
        description=description,
        body=body,
        slug=slug or name,
    )


def _session_with_skills(monkeypatch, tmp_path, skills):
    class _Store:
        def list(self, state=None):
            return list(skills) if state == "active" else []

    monkeypatch.setattr(
        "harness.conversation.SkillStore", lambda *_a, **_k: _Store(),
    )
    monkeypatch.setattr(
        "harness.plugin_registry.list_enabled_plugin_skills",
        lambda: [],
    )
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = str(tmp_path)
    session = ConversationalSession(cfg)
    monkeypatch.setattr(session, "_build_turn_cg_section", lambda _msg: "")
    monkeypatch.setattr(session, "_build_turn_wiki_section", lambda _msg: "")
    monkeypatch.setattr(session, "_build_turn_vault_section", lambda _msg: "")
    return session


def test_select_overlap_hit_returns_body():
    skill = _skill(
        "audit-the-router",
        "How to audit routing decisions.",
        "Start from harness/router.py and follow the receipt.",
    )
    selected = select_skill_bodies("please audit routing", [skill])
    assert selected == [skill]


def test_select_zero_overlap_is_empty():
    skill = _skill(
        "audit-the-router",
        "How to audit routing decisions.",
        "Start from harness/router.py and follow the receipt.",
    )
    assert select_skill_bodies("unrelated zebra pineapple", [skill]) == []
    assert select_skill_bodies("", [skill]) == []
    assert select_skill_bodies("audit routing", []) == []


def test_select_scores_name_and_description_not_body():
    skill = _skill(
        "alpha-tools",
        "beta workflow notes",
        "router audit receipt that would otherwise match",
    )
    assert select_skill_bodies("router audit receipt", [skill]) == []
    assert select_skill_bodies("alpha beta workflow", [skill]) == [skill]


def test_select_greedy_respects_count_and_budget():
    first = _skill("alpha-one", "alpha routing", "x" * 40)
    second = _skill("alpha-two", "alpha routing extra", "y" * 40)
    third = _skill("alpha-three", "alpha routing more", "z" * 40)
    picked = select_skill_bodies(
        "alpha routing",
        [first, second, third],
        max_count=2,
    )
    assert picked == [first, second]

    huge = _skill("alpha-huge", "alpha routing", "w" * 8000)
    tiny = _skill("alpha-tiny", "alpha routing", "ok")
    assert select_skill_bodies(
        "alpha routing",
        [huge, tiny],
        token_budget=10,
    ) == [tiny]
    assert select_skill_bodies("alpha routing", [first], max_count=0) == []
    assert select_skill_bodies("alpha routing", [first], token_budget=0) == []


def test_catalog_omits_bodies():
    skill = _skill(
        "audit-the-router",
        "How to audit routing decisions.",
        "Start from harness/router.py and follow the receipt.",
        slug="audit-the-router",
    )
    catalog = format_skill_catalog([skill])
    assert "METHOD ONLY" in catalog
    assert "audit-the-router" in catalog
    assert "How to audit routing decisions." in catalog
    assert "slug: audit-the-router" in catalog
    assert "Start from harness/router.py" not in catalog
    assert catalog_line(skill) == SkillCatalogLine(
        name="audit-the-router",
        description="How to audit routing decisions.",
        slug="audit-the-router",
    )


def test_overlap_hit_injects_body(monkeypatch, tmp_path):
    body = "Start from harness/router.py and follow the receipt."
    skill = _skill(
        "audit-the-router",
        "How to audit routing decisions.",
        body,
    )
    session = _session_with_skills(monkeypatch, tmp_path, [skill])
    prefix = session._history[0]["content"]
    assert body not in prefix
    assert "METHOD ONLY" in prefix

    out = session._append_turn_context_trailer("hello", "please audit routing")
    assert body in out
    assert "METHOD ONLY" in out
    assert session._history[0]["content"] == prefix
    assert body not in session._history[0]["content"]


def test_zero_overlap_does_not_inject_body(monkeypatch, tmp_path):
    body = "Start from harness/router.py and follow the receipt."
    skill = _skill(
        "audit-the-router",
        "How to audit routing decisions.",
        body,
    )
    session = _session_with_skills(monkeypatch, tmp_path, [skill])
    out = session._append_turn_context_trailer("hello", "unrelated zebra pineapple")
    assert body not in out
    assert format_retrieved_skill_bodies([]) == ""
    assert session._history[0]["content"].count("METHOD ONLY") == 1


def test_prefix_unchanged_across_two_queries_with_different_hits(
    monkeypatch, tmp_path,
):
    router_body = "ROUTER_BODY_UNIQUE Start from harness/router.py."
    wiki_body = "WIKI_BODY_UNIQUE Ground wiki pages before claiming."
    router = _skill(
        "audit-the-router",
        "How to audit routing decisions.",
        router_body,
    )
    wiki = _skill(
        "wiki-grounding",
        "How to ground wiki pages.",
        wiki_body,
    )
    session = _session_with_skills(monkeypatch, tmp_path, [router, wiki])
    base = session._history[0]["content"]
    frozen = session._ensure_frozen_system_prompt(base)
    prefix = session._history[0]["content"]
    assert prefix == frozen
    assert router_body not in prefix
    assert wiki_body not in prefix

    session.conversation_key = "lineage-ROOT-skills"
    clear_stable_prefixes()
    register_stable_prefix("system_v1", prefix)
    key_before = session._prompt_cache_conversation_key()
    scope_before = prompt_cache_scope(key_before)
    assert find_stable_prefix(session._history[0]["content"]) == "system_v1"

    first = session._append_turn_context_trailer("hello", "please audit routing")
    second = session._append_turn_context_trailer("hello", "please ground wiki pages")

    assert router_body in first
    assert wiki_body not in first
    assert wiki_body in second
    assert router_body not in second
    assert session._history[0]["content"] == prefix
    assert session._frozen_system_prompt == frozen
    assert find_stable_prefix(session._history[0]["content"]) == "system_v1"
    key_after = session._prompt_cache_conversation_key()
    assert key_after == key_before
    assert prompt_cache_scope(key_after) == scope_before
    clear_stable_prefixes()
