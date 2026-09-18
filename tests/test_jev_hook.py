from __future__ import annotations

import json
from types import SimpleNamespace

from harness.jev.hook import (
    SILLY_CONTEXT,
    hook_context,
    hook_response,
    prompt_text,
    run_stdin,
    silly_humans_wanted,
)
from harness.jev.judge import Judgment


def test_prompt_text_reads_common_keys():
    assert "hello" in prompt_text({"prompt": "hello"})
    assert "inner" in prompt_text({"attachments": [{"text": "inner"}]})
    assert prompt_text("nope") == ""


def test_hook_injects_skill_suggestion():
    judged = Judgment(skill="recall-working-set", lane="skill", depth="STANDARD", gate=0.7)
    context = hook_context("catch me up", judged=judged)
    assert "recall-working-set" in context
    assert SILLY_CONTEXT not in context


def test_hook_silly_humans_refined_gate():
    quiet = Judgment(skill="technical-writing", silly_humans=0.06, gate=0.4, depth="STANDARD")
    assert silly_humans_wanted(quiet, "write a blog post about GPUs") is False
    assert SILLY_CONTEXT not in hook_context("write a blog post about GPUs", judged=quiet)

    kernel = Judgment(skill="silly-humans", silly_humans=0.88, gate=0.7, depth="DEEP")
    assert silly_humans_wanted(kernel, "beat the llama.cpp world record") is True
    assert SILLY_CONTEXT in hook_context("beat the llama.cpp world record", judged=kernel)

    mid = Judgment(skill="how-the-system", silly_humans=0.31, gate=0.5, depth="STANDARD")
    assert silly_humans_wanted(mid, "cuda kernel leftover") is False
    strong = Judgment(skill="how-the-system", silly_humans=0.61, gate=0.5, depth="STANDARD")
    assert silly_humans_wanted(strong, "cuda kernel leftover") is True


def test_hook_fail_open_on_bad_json():
    assert run_stdin("not-json") == "{}"
    assert hook_response(None) == {}


def test_hook_response_empty_prompt():
    assert hook_response({"prompt": "   "}) == {}


def test_hook_response_uses_decide(monkeypatch):
    def decide(_state, questions):
        if "fits_recall-working-set" in questions:
            return {
                "answers": {
                    "which": {"choice": "recall-working-set"},
                    "fits_recall-working-set": {"noul": 0.8},
                }
            }
        return {
            "answers": {
                "which": {
                    "choice": "recall-working-set",
                    "probabilities": {"recall-working-set": 0.9},
                },
                "playbook": {"choice": "none"},
                "depth": {"choice": "STANDARD"},
                "lane": {"choice": "skill"},
                "silly_humans": {"noul": 0.02},
                "gate_acts": {"noul": 0.7},
                "gate_procedure": {"noul": 0.7},
                "gate_prose": {"noul": 0.2},
                "gate_oneliner": {"noul": 0.05},
            }
        }

    roster = [SimpleNamespace(name="recall-working-set", description="Catch me up", body="wiki first")]
    out = hook_response({"prompt": "catch me up on the current work"}, roster=roster, decide=decide)
    assert out.get("continue") is True
    assert "recall-working-set" in out.get("additional_context", "")
    payload = json.loads(run_stdin("{}"))
    assert payload == {}
