from __future__ import annotations

from types import SimpleNamespace

from harness.jev.judge import (
    Judgment,
    clear_cache,
    enabled,
    judge_turn,
    opted_in,
    prefetch_turn_judgment,
    suggestion_block,
)
from harness.jev.questions import GATE_THRESHOLD
from harness.task_profile import DEEP, MICRO, STANDARD, classify_task_profile


def _answers(which_probs, **nouls):
    answers = {
        "which": {
            "choice": next(iter(which_probs)),
            "probabilities": dict(which_probs),
        },
        "playbook": {"choice": nouls.pop("playbook", "none")},
        "depth": {"choice": nouls.pop("depth", "STANDARD")},
        "lane": {"choice": nouls.pop("lane", "skill")},
        "silly_humans": {"noul": nouls.pop("silly_humans", 0.01)},
        "gate_acts": {"noul": nouls.pop("gate_acts", 0.8)},
        "gate_procedure": {"noul": nouls.pop("gate_procedure", 0.8)},
        "gate_prose": {"noul": nouls.pop("gate_prose", 0.1)},
        "gate_oneliner": {"noul": nouls.pop("gate_oneliner", 0.05)},
    }
    return {"answers": answers}


def test_judge_two_call_picks_fit_skill():
    clear_cache()
    calls = []

    def decide(state, questions):
        calls.append(questions)
        if "fits_session-wrap" in questions:
            return {
                "answers": {
                    "which": {"choice": "session-wrap"},
                    "fits_session-wrap": {"noul": 0.91},
                    "fits_other": {"noul": 0.1},
                }
            }
        return _answers({"session-wrap": 0.7, "other": 0.2})

    roster = [
        SimpleNamespace(name="session-wrap", description="End of session wrap", body="wrap steps"),
        SimpleNamespace(name="other", description="Something else", body="no"),
    ]
    judged = judge_turn("wrap up and commit", roster, decide=decide)
    assert judged.skill == "session-wrap"
    assert judged.skill_fits >= 0.9
    assert judged.depth == "STANDARD"
    assert judged.gate >= GATE_THRESHOLD
    assert judged.oneliner < 0.5
    assert len(calls) == 2


def test_judge_skips_rerank_on_micro_depth():
    clear_cache()
    calls = []

    def decide(state, questions):
        calls.append(questions)
        body = _answers({"technical-writing": 0.9, "oss-pr-issue-absorb": 0.1}, depth="MICRO", gate_oneliner=0.2)
        return body

    roster = [
        SimpleNamespace(name="technical-writing", description="Write docs", body="docs"),
        SimpleNamespace(name="oss-pr-issue-absorb", description="Absorb a PR", body="pr"),
    ]
    judged = judge_turn("typo in README.md", roster, decide=decide)
    assert judged.skill is None
    assert judged.depth == "MICRO"
    assert len(calls) == 1


def test_judge_skips_rerank_on_oneliner():
    clear_cache()
    calls = []

    def decide(state, questions):
        calls.append(questions)
        return _answers({"technical-writing": 0.9}, depth="STANDARD", gate_oneliner=0.82)

    roster = [SimpleNamespace(name="technical-writing", description="Write docs", body="docs")]
    judged = judge_turn("fix a typo in the README title", roster, decide=decide)
    assert judged.skill is None
    assert judged.oneliner >= 0.5
    assert len(calls) == 1


def test_judge_skips_trivial_ack_without_network():
    clear_cache()

    def boom(_state, _questions):
        raise AssertionError("trivial ack must not call Jev")

    judged = judge_turn("thanks", [SimpleNamespace(name="x", description="x", body="x")], decide=boom)
    assert judged.skill is None
    assert judged.depth == ""


def test_judge_fail_open_on_none():
    clear_cache()
    judged = judge_turn("audit the router", [SimpleNamespace(name="x", description="x", body="x")], decide=lambda *_a, **_k: None)
    assert judged == Judgment() or judged.skill is None
    assert judged.depth == ""


def test_enabled_off_unless_explicit_opt_in(monkeypatch):
    monkeypatch.setattr("harness.jev.client.resolve_openrouter_key", lambda: "sk-test")
    monkeypatch.delenv("HARNESS_JEV", raising=False)
    assert opted_in() is False
    assert enabled() is False
    for raw in ("", "auto", "0", "false", "off", "no", "maybe"):
        monkeypatch.setenv("HARNESS_JEV", raw)
        assert opted_in() is False, raw
        assert enabled() is False, raw
    monkeypatch.setenv("HARNESS_JEV", "1")
    monkeypatch.setattr("harness.jev.client.resolve_openrouter_key", lambda: "")
    assert opted_in() is True
    assert enabled() is False
    monkeypatch.setattr("harness.jev.client.resolve_openrouter_key", lambda: "sk-test")
    assert enabled() is True


def test_judge_does_not_network_without_opt_in(monkeypatch):
    clear_cache()
    monkeypatch.delenv("HARNESS_JEV", raising=False)
    monkeypatch.setattr("harness.jev.client.resolve_openrouter_key", lambda: "sk-test")

    def boom(_state, _questions):
        raise AssertionError("Decisions must not run unless HARNESS_JEV is on")

    monkeypatch.setattr("harness.jev.client.decide", boom)
    judged = judge_turn(
        "audit the router please",
        [SimpleNamespace(name="x", description="audit routing", body="x")],
    )
    assert judged.skill is None
    assert judged.depth == ""


def test_suggestion_block_names_skill_and_lane():
    text = suggestion_block(
        Judgment(skill="how-the-system", lane="skill", playbook="investigation", depth="DEEP")
    )
    assert "how-the-system" in text
    assert "lane skill" in text
    assert "playbook investigation" in text
    assert suggestion_block(None) == ""
    assert suggestion_block(Judgment()) == ""


def test_prefetch_stashes_when_enabled(monkeypatch):
    clear_cache()
    monkeypatch.setenv("HARNESS_JEV", "1")
    monkeypatch.setattr("harness.jev.client.resolve_openrouter_key", lambda: "sk-test")
    monkeypatch.setattr(
        "harness.jev.judge.judge_turn",
        lambda *_a, **_k: Judgment(skill="recall-working-set", depth="STANDARD"),
    )
    session = SimpleNamespace(_retrievable_skills=[], _jev_judgment=None)
    judged = prefetch_turn_judgment(session, "catch me up")
    assert judged.skill == "recall-working-set"
    assert session._jev_judgment.skill == "recall-working-set"


def test_classify_jev_deep_wins_without_regex():
    judged = Judgment(depth="DEEP")
    assert classify_task_profile("how does compaction work in this repo", judgment=judged) == DEEP


def test_classify_jev_micro_needs_deterministic_cue():
    judged = Judgment(depth="MICRO")
    assert classify_task_profile("add OAuth support across accounts", judgment=judged) == STANDARD
    assert classify_task_profile("typo in README.md", judgment=judged) == MICRO


def test_classify_library_filename_with_world_record_is_not_micro():
    assert classify_task_profile("beat the world record on llama.cpp decode") == STANDARD
