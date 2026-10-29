"""Driver-eval swarms run on the local substrate (pmharness/eval_substrate.py).

Without the guard, a live driver's run_swarm was refused by the product bridge
(no HARNESS_ALLOW_DEMO_SWARM) or routed to real workers (a repo in play), and
`harness eval` scored the driver as premature with swarms=0 and no visible error.
"""

import os

from pmharness.eval_substrate import eval_intent, local_substrate
from pmharness.intent import DriverIntent


def test_local_substrate_sets_flag_drops_repo_and_restores(monkeypatch, tmp_path):
    monkeypatch.delenv("HARNESS_ALLOW_DEMO_SWARM", raising=False)
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    with local_substrate():
        assert os.environ.get("HARNESS_ALLOW_DEMO_SWARM") == "1"
        assert "HARNESS_REPO" not in os.environ
    assert "HARNESS_ALLOW_DEMO_SWARM" not in os.environ
    assert os.environ.get("HARNESS_REPO") == str(tmp_path)


def test_local_substrate_restores_after_an_error(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_DEMO_SWARM", "0")
    try:
        with local_substrate():
            raise RuntimeError("swarm failed")
    except RuntimeError:
        pass
    assert os.environ.get("HARNESS_ALLOW_DEMO_SWARM") == "0"


def test_eval_intent_drops_a_driver_supplied_repo():
    intent = DriverIntent(action="run_swarm", goal="audit", repo="C:/somewhere/else")
    assert eval_intent(intent).repo is None
    assert eval_intent(intent).goal == "audit"
    plain = DriverIntent(action="run_swarm", goal="audit")
    assert eval_intent(plain) is plain


def test_harness_eval_runs_swarms_without_the_env_flag(monkeypatch, capsys, tmp_path):
    # the user's shell has neither the eval flag nor a clean repo pointer
    monkeypatch.delenv("HARNESS_ALLOW_DEMO_SWARM", raising=False)
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    from harness.eval_cmd import run_eval

    assert run_eval(["--stage", "v2"]) == 0
    out = capsys.readouterr().out
    invest = next(line for line in out.splitlines() if line.strip().startswith("invest_vague"))
    assert "swarms=1" in invest, out
    assert "error:" not in out, out
    assert "mean: 100.0%" in out, out
    assert os.environ.get("HARNESS_REPO") == str(tmp_path)
