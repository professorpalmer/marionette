"""CI waits out PyPI index lag only for a missing puppetmaster-ai release."""
import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pip_install_retry", ROOT / "scripts" / "pip_install_retry.py")
pip_install_retry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pip_install_retry)


def _runner(results):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        code, err = results[len(calls) - 1]
        return subprocess.CompletedProcess(command, code, "", err)
    return run, calls


def test_index_lag_is_retried_without_cache():
    run, calls = _runner([(1, "ERROR: No matching distribution found for puppetmaster-ai==9.9.9"), (0, "")])
    waits = []
    assert pip_install_retry.main(["puppetmaster-ai==9.9.9"], run=run, sleep=waits.append) == 0
    assert len(calls) == 2 and "--no-cache-dir" not in calls[0] and "--no-cache-dir" in calls[1]
    assert waits == [30]


def test_other_failures_are_not_retried():
    run, calls = _runner([(1, "ERROR: Could not build wheels")])
    assert pip_install_retry.main(["-e", ".[dev]"], run=run, sleep=lambda s: None) == 1
    assert len(calls) == 1


def test_ci_installs_go_through_the_retry():
    for name in ("tests.yml", "tests-full.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text()
        assert "pip install -e" not in text, name
        assert "python scripts/pip_install_retry.py -e" in text, name
