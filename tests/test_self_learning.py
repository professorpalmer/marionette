"""Self-learning: skill store CRUD + distiller (fake pilot, deterministic)."""
from harness.skill_store import SkillStore, Skill
from harness.skill_distiller import distill_session, _is_duplicate, Candidate


def test_store_save_get_states(tmp_path):
    s = SkillStore(root=str(tmp_path))
    sk = Skill(name="Map auth flow", description="how to trace auth", body="1. grep\n2. read", state="pending")
    s.save(sk)
    got = s.get(sk.slug)
    assert got and got.name == "Map auth flow" and got.state == "pending"
    # promote -> moves dirs, only one copy
    s.set_state(sk.slug, "active")
    assert s.get(sk.slug).state == "active"
    assert not (tmp_path / "pending" / f"{sk.slug}.md").exists()
    assert (tmp_path / "active" / f"{sk.slug}.md").exists()


def test_store_list_and_used(tmp_path):
    s = SkillStore(root=str(tmp_path))
    s.save(Skill(name="A", state="active"))
    s.save(Skill(name="B", state="pending"))
    assert len(s.list()) == 2
    assert len(s.list("active")) == 1
    s.mark_used("a")
    assert s.get("a").used_count == 1


class _Pilot:
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


def test_distill_proposes_pending(tmp_path):
    s = SkillStore(root=str(tmp_path))
    pilot = _Pilot('{"name":"Trace SSE bug","description":"when SSE hangs","body":"1. check headers\n2. flush"}')
    findings = [{"type": "finding", "headline": "SSE needs flush"},
                {"type": "decision", "headline": "use text/event-stream"}]
    r = distill_session(pilot, "fix sse", findings, s)
    assert r["status"] == "proposed"
    cand = s.get(r["slug"])
    assert cand.state == "pending" and "flush" in cand.body


def test_distill_skips_insufficient(tmp_path):
    s = SkillStore(root=str(tmp_path))
    pilot = _Pilot('{"name":"x"}')
    r = distill_session(pilot, "obj", [{"type": "finding", "headline": "one"}], s)
    assert r["status"] == "skipped"


def test_distill_skips_no_lesson(tmp_path):
    s = SkillStore(root=str(tmp_path))
    pilot = _Pilot('{"name":""}')
    findings = [{"type": "finding", "headline": "a"}, {"type": "finding", "headline": "b"}]
    r = distill_session(pilot, "obj", findings, s)
    assert r["status"] == "skipped"


def test_distill_dedup(tmp_path):
    s = SkillStore(root=str(tmp_path))
    s.save(Skill(name="Trace SSE streaming bug", description="when SSE hangs in browser", body="steps", state="active"))
    pilot = _Pilot([
        '{"name":"Trace SSE bug","description":"when SSE hangs","body":"steps"}',
        '{"verdict":"duplicate","slug":"trace-sse-streaming-bug"}'
    ])
    findings = [{"type": "finding", "headline": "a"}, {"type": "finding", "headline": "b"}]
    r = distill_session(pilot, "obj", findings, s)
    assert r["status"] == "duplicate"


def test_verification_findings_excluded(tmp_path):
    s = SkillStore(root=str(tmp_path))
    pilot = _Pilot('{"name":"X","description":"d","body":"b"}')
    # two verification + one real = below MIN_FINDINGS (2 real)
    findings = [{"type": "verification", "headline": "v"}, {"type": "verification", "headline": "v2"},
                {"type": "finding", "headline": "real"}]
    r = distill_session(pilot, "obj", findings, s)
    assert r["status"] == "skipped"



def test_skillstore_path_traversal_blocked(tmp_path):
    """A malicious slug must not escape the skills root for read OR write."""
    import os
    from harness.skill_store import SkillStore
    s = SkillStore(root=str(tmp_path / "skills"))
    outside = tmp_path / "evil.md"
    # attempt to write outside via set_state on a traversal slug -> must not create it
    s.set_state("../../evil", "active")
    assert not outside.exists(), "traversal slug escaped the skills dir"
    # get with a traversal slug must not read an arbitrary file
    (tmp_path / "secret.md").write_text("---\nname: secret\n---\nsensitive")
    got = s.get("../secret")
    # sanitized slug becomes 'secret' under the skills root, which does not exist
    assert got is None
