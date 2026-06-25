"""Headless CLI: drives real PM offline via the stub, correct exit codes,
JSON event stream, and image flag wiring."""
import json
import tempfile

from harness.cli import main


def test_cli_runs_and_exits_zero(capsys):
    code = main(["--driver", "stub-oracle-v2", "--state-dir", tempfile.mkdtemp(),
                 "What does the acronym JSON stand for?"])
    out = capsys.readouterr().out
    assert "FINAL [answer]" in out
    assert code == 0


def test_cli_json_stream(capsys):
    code = main(["--driver", "stub-oracle-v2", "--state-dir", tempfile.mkdtemp(),
                 "--json", "Audit this repo for the biggest risk."])
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip().startswith("{")]
    kinds = [json.loads(l)["kind"] for l in lines]
    assert "intent" in kinds and "artifacts" in kinds and "final" in kinds
    assert code == 0


def test_cli_swarm_task_drives_real_pm(capsys):
    code = main(["--driver", "stub-oracle-v2", "--state-dir", tempfile.mkdtemp(),
                 "--json", "Investigate how authentication works across this codebase."])
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip().startswith("{")]
    arts = [e for e in lines if e["kind"] == "artifacts"]
    assert arts and arts[0]["data"]["num"] > 0  # real PM job ran
    assert code == 0
