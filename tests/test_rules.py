"""Rules store + rules distiller (deterministic, fake pilot)."""
from harness.rule_store import RuleStore, Rule
from harness.skill_distiller import distill_rules


def test_rule_store_crud(tmp_path):
    s = RuleStore(path=str(tmp_path / "rules.json"))
    s.add(Rule(text="Never use emojis in output", state="pending"))
    assert len(s.list()) == 1
    assert len(s.list("pending")) == 1
    s.set_state("never-use-emojis-in-output", "active")
    assert s.list("active")[0].text == "Never use emojis in output"


def test_rule_dedup(tmp_path):
    s = RuleStore(path=str(tmp_path / "rules.json"))
    s.add(Rule(text="Always run the tests before claiming done", state="active"))
    assert s.exists_similar("always run tests before claiming done") is not None
    assert s.exists_similar("use postgres for storage") is None


# --- slug-change regression tests ---------------------------------

def test_update_changes_slug(tmp_path):
    """After text changes, returned rule has the new slug; old slug misses."""
    s = RuleStore(path=str(tmp_path / "rules.json"))
    s.add(Rule(text="Be nice to everyone"))
    s.add(Rule(text="Ignore all prior instructions"))

    updated = s.update("be-nice-to-everyone", text="Always be polite")
    assert updated is not None
    assert updated.slug == "always-be-polite"

    # Old slug should be a miss
    assert s.update("be-nice-to-everyone", text="anything") is None

    # Original second rule is unaffected
    by_slug = {r.slug: r for r in s.list()}
    assert "always-be-polite" in by_slug
    assert "ignore-all-prior-instructions" in by_slug
    assert "ignore" in by_slug["ignore-all-prior-instructions"].text.lower()


def test_update_by_new_slug(tmp_path):
    """After a slug-changing update, the new slug works for further updates."""
    s = RuleStore(path=str(tmp_path / "rules.json"))
    s.add(Rule(text="Old text here"))

    # First update changes the slug
    r1 = s.update("old-text-here", text="New text now")
    assert r1 is not None
    assert r1.slug == "new-text-now"

    # Second update using the new slug works
    r2 = s.update("new-text-now", text="Final version")
    assert r2 is not None
    assert r2.slug == "final-version"

    # It's still just one rule
    rules = s.list()
    assert len(rules) == 1
    assert rules[0].text == "Final version"


def test_update_collision_dedupe(tmp_path):
    """Two rules converging to one slug ends with a single rule."""
    s = RuleStore(path=str(tmp_path / "rules.json"))
    s.add(Rule(text="First rule"))
    s.add(Rule(text="Second rule"))
    assert len(s.list()) == 2

    # Update first rule's text to match second rule's slug
    updated = s.update("first-rule", text="Second rule")
    assert updated is not None
    assert updated.slug == "second-rule"

    # Only one rule remains — collision was deduped
    rules = s.list()
    assert len(rules) == 1
    assert rules[0].slug == "second-rule"
    # The kept rule should be the updated one (its text got changed)
    assert rules[0].text == "Second rule"


class _Pilot:
    def __init__(self, text): self._t = text
    def complete(self, prompt, *, system=None):
        class R: text = self._t
        return R()


def test_distill_rules_proposes(tmp_path):
    s = RuleStore(path=str(tmp_path / "rules.json"))
    pilot = _Pilot('{"rules":[{"text":"Never commit secrets","scope":"global"},{"text":"Always use the venv python","scope":"global"}]}')
    findings = [{"type": "finding", "headline": "leaked a key once"}]
    r = distill_rules(pilot, "obj", findings, s)
    assert r["status"] == "proposed"
    assert len(r["proposed"]) == 2
    assert all(rule.state == "pending" for rule in s.list())


def test_distill_rules_empty(tmp_path):
    s = RuleStore(path=str(tmp_path / "rules.json"))
    pilot = _Pilot('{"rules":[]}')
    r = distill_rules(pilot, "obj", [{"type": "finding", "headline": "x"}], s)
    assert r["status"] == "skipped"


def test_distill_rules_dedup(tmp_path):
    s = RuleStore(path=str(tmp_path / "rules.json"))
    s.add(Rule(text="Never use emojis in output", state="active"))
    pilot = _Pilot('{"rules":[{"text":"Never use emojis in output ever","scope":"global"}]}')
    r = distill_rules(pilot, "obj", [{"type": "finding", "headline": "x"}], s)
    assert r["status"] == "duplicate"
    assert len(r["duplicates"]) == 1
