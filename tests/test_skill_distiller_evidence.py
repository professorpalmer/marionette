from __future__ import annotations

"""A distilled skill must name its cue and cite evidence from the run.
A candidate without grounding is a guess, and a wrong skill is worse than none."""

import json

from harness.skill_distiller import distill_session
from harness.skill_store import SkillStore, Skill


class _Pilot:
    def __init__(self, text):
        self.text = text

    def complete(self, prompt, *, system=None):
        text = self.text

        class R:
            pass
        r = R()
        r.text = text
        return r


FINDINGS = [
    {"type": "finding", "headline": "pytest fails with ModuleNotFoundError: No module named 'harness'"},
    {"type": "decision", "headline": "run from repo root with .venv/bin/python -m pytest"},
]


def _envelope(**over):
    obj = {
        "name": "Run tests from repo root",
        "description": "when imports fail under pytest",
        "cue": "pytest cannot import the harness package",
        "evidence": ["ModuleNotFoundError: No module named 'harness'",
                     ".venv/bin/python -m pytest"],
        "body": "1. cd to the repo root\n2. run .venv/bin/python -m pytest",
    }
    obj.update(over)
    return json.dumps(obj)


def test_grounded_candidate_saved_with_cue_and_evidence(tmp_path):
    store = SkillStore(root=str(tmp_path))
    r = distill_session(_Pilot(_envelope()), "fix tests", FINDINGS, store)
    assert r["status"] == "proposed"
    sk = store.get(r["slug"])
    assert sk.state == "pending"
    assert sk.cue == "pytest cannot import the harness package"
    assert sk.evidence == ["ModuleNotFoundError: No module named 'harness'",
                           ".venv/bin/python -m pytest"]
    # The fields survive a fresh read from disk.
    again = SkillStore(root=str(tmp_path)).get(r["slug"])
    assert again.cue == sk.cue and again.evidence == sk.evidence


def test_ungrounded_items_are_dropped_from_saved_evidence(tmp_path):
    store = SkillStore(root=str(tmp_path))
    env = _envelope(evidence=[".venv/bin/python -m pytest",
                              "kubernetes pod crashloop in staging"])
    r = distill_session(_Pilot(env), "fix tests", FINDINGS, store)
    assert r["status"] == "proposed"
    assert store.get(r["slug"]).evidence == [".venv/bin/python -m pytest"]


def test_missing_cue_skipped(tmp_path):
    store = SkillStore(root=str(tmp_path))
    r = distill_session(_Pilot(_envelope(cue="  ")), "fix tests", FINDINGS, store)
    assert r["status"] == "skipped"
    assert r["reason"] == "no cue"
    assert store.list() == []


def test_empty_evidence_skipped(tmp_path):
    store = SkillStore(root=str(tmp_path))
    r = distill_session(_Pilot(_envelope(evidence=[])), "fix tests", FINDINGS, store)
    assert r["status"] == "skipped"
    assert r["reason"] == "no evidence"
    assert store.list() == []


def test_ungrounded_evidence_skipped(tmp_path):
    store = SkillStore(root=str(tmp_path))
    env = _envelope(evidence=["kubernetes pod crashloop in staging", "a"])
    r = distill_session(_Pilot(env), "fix tests", FINDINGS, store)
    assert r["status"] == "skipped"
    assert r["reason"] == "evidence not grounded in findings"
    assert store.list() == []


def test_old_skill_without_fields_still_loads(tmp_path):
    store = SkillStore(root=str(tmp_path))
    (tmp_path / "active" / "old-skill.md").write_text(
        "---\nname: Old skill\ndescription: legacy\nstate: active\n"
        "source: manual\ncreated_at: 1\nused_count: 0\nlast_used: 0\n---\n\n1. step\n",
        encoding="utf-8")
    sk = store.get("old-skill")
    assert sk is not None and sk.name == "Old skill" and sk.body == "1. step"
    assert sk.cue == "" and sk.evidence == []
    # A save without the fields writes no empty keys.
    store.save(sk)
    text = (tmp_path / "active" / "old-skill.md").read_text(encoding="utf-8")
    assert "cue:" not in text and "evidence:" not in text


def test_bad_evidence_frontmatter_loads_as_empty(tmp_path):
    store = SkillStore(root=str(tmp_path))
    store.save(Skill(name="X skill", state="pending", cue="c", evidence=["e"]))
    p = tmp_path / "pending" / "x-skill.md"
    p.write_text(p.read_text(encoding="utf-8").replace('["e"]', "[not json"),
                 encoding="utf-8")
    assert store.get("x-skill").evidence == []
