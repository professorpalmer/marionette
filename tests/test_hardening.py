"""No-key preflight, eval subcommand, version, config layering."""
import json
import pytest
import os
import tempfile
from pathlib import Path

from harness.config import HarnessConfig
from harness.session import Session
from harness import cli


def test_preflight_flags_missing_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = HarnessConfig(driver="glm-5.2", reach="openrouter",
                        state_dir=tempfile.mkdtemp())
    s = Session(cfg)
    msg = s.preflight()
    assert msg and "OPENROUTER_API_KEY" in msg and "stub-oracle-v2" in msg


def test_preflight_none_for_stub():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    assert Session(cfg).preflight() is None


def test_cli_missing_key_clean_exit(monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    code = cli.main(["--driver", "glm-5.2", "--reach", "openrouter",
                     "--state-dir", tempfile.mkdtemp(), "do something"])
    assert code == 1
    assert "not set" in capsys.readouterr().err


def test_version(capsys):
    code = cli.main(["--version"])
    assert code == 0
    assert "harness" in capsys.readouterr().out


@pytest.mark.swarm
def test_eval_s4_offline(capsys):
    code = cli.main(["eval", "--driver", "stub-oracle-v2", "--stage", "s4"])
    # stub-oracle-v2 isn't the reader; just assert it runs and reports a mean
    assert "mean:" in capsys.readouterr().out
    assert code == 0


def test_config_file_layering(monkeypatch, tmp_path):
    cfgfile = tmp_path / "h.json"
    cfgfile.write_text(json.dumps({"driver": "deepseek-v4-pro", "budget": 5}))
    monkeypatch.setenv("HARNESS_CONFIG", str(cfgfile))
    monkeypatch.delenv("HARNESS_DRIVER", raising=False)
    monkeypatch.delenv("HARNESS_BUDGET", raising=False)
    c = HarnessConfig.from_env()
    assert c.driver == "deepseek-v4-pro" and c.budget == 5
    # env overrides file
    monkeypatch.setenv("HARNESS_DRIVER", "glm-5.2")
    assert HarnessConfig.from_env().driver == "glm-5.2"
