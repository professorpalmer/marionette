"""Versioned distiller skills with independent admit support and rollback."""
from __future__ import annotations

from harness.api.skills import post_skills_approve, post_skills_rollback, get_skill_versions
from harness.skill_store import Skill, SkillStore


def _svc(store: SkillStore):
    from types import SimpleNamespace

    return SimpleNamespace(skills=store, get_pilot=lambda: SimpleNamespace(harness_session_id=""))


def test_distilled_skill_needs_two_independent_admits(tmp_path):
    store = SkillStore(root=str(tmp_path))
    sk = Skill(
        name="Factorio cheat",
        body="spawn resources",
        state="pending",
        source="distilled:session",
        provenance_session="worker-sess",
    )
    store.save(sk)
    svc = _svc(store)
    first, p1 = post_skills_approve({"slug": sk.slug, "session_id": "human-a"}, svc)
    assert first == 200
    assert p1["ok"] is True
    assert p1.get("pending_admit") is True
    assert p1["support"] == 1
    assert store.get(sk.slug).state == "pending"
    second, p2 = post_skills_approve({"slug": sk.slug, "session_id": "human-b"}, svc)
    assert second == 200
    assert p2.get("active") is True
    assert store.get(sk.slug).state == "active"


def test_manual_skill_single_admit(tmp_path):
    store = SkillStore(root=str(tmp_path))
    sk = Skill(name="Manual tip", body="do x", state="pending", source="manual")
    store.save(sk)
    code, payload = post_skills_approve({"slug": sk.slug, "session_id": "human-a"}, _svc(store))
    assert code == 200
    assert payload.get("active") is True
    assert store.get(sk.slug).state == "active"


def test_skill_version_snapshot_and_rollback(tmp_path):
    store = SkillStore(root=str(tmp_path))
    sk = Skill(name="Latch door", body="v1 steps", state="active", source="manual")
    store.save(sk)
    store.snapshot_version(sk)
    sk.body = "v2 steps"
    store.save(sk)
    versions = store.list_versions(sk.slug)
    assert 1 in versions
    restored = store.rollback(sk.slug)
    assert restored and "v1 steps" in restored.body
    code, payload = get_skill_versions(sk.slug, _svc(store))
    assert code == 200
    assert payload["ok"] is True
    assert 1 in payload["versions"]
    rb_code, rb = post_skills_rollback({"slug": sk.slug, "version": 1}, _svc(store))
    assert rb_code == 200
    assert rb["ok"] is True
