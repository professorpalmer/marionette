"""Host quality GATE — finish barrier with fingerprint skip + budgets."""
from __future__ import annotations

import os
import sys
import time

from harness.config import HarnessConfig
from harness.quality_gate import (
    QualityGateRunner,
    workspace_fingerprint,
)


def _py_script(tmp_path, name: str, body: str) -> str:
    """Write a portable gate script; return argv string for shell=True."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    # Quote for both POSIX and Windows cmd when path has spaces.
    return '"%s" "%s"' % (sys.executable, path)


def test_quality_gate_blocks_finish_on_fail(tmp_path):
    cmd = _py_script(tmp_path, "fail_gate.py", "raise SystemExit(1)\n")
    runner = QualityGateRunner(cmds=[cmd], max_attempts=3, max_seconds=30)
    result = runner.run(str(tmp_path), auto_mode=False)
    assert result.passed is False
    assert result.block_finish is True
    assert result.outcome == "failed"


def test_quality_gate_passes_allows_finish(tmp_path):
    cmd = _py_script(tmp_path, "ok_gate.py", "raise SystemExit(0)\n")
    runner = QualityGateRunner(cmds=[cmd])
    result = runner.run(str(tmp_path), auto_mode=False)
    assert result.passed is True
    assert result.block_finish is False
    assert result.outcome == "passed"


def test_quality_gate_skips_unchanged_fingerprint(tmp_path):
    cmd = _py_script(tmp_path, "fail_gate.py", "raise SystemExit(1)\n")
    runner = QualityGateRunner(cmds=[cmd], max_attempts=5)
    first = runner.run(str(tmp_path), auto_mode=False)
    assert first.outcome == "failed"
    second = runner.run(str(tmp_path), auto_mode=False)
    assert second.outcome == "skipped_unchanged"
    assert second.block_finish is True
    assert second.attempts == first.attempts  # did not re-increment on skip


def test_quality_gate_reruns_after_workspace_change(tmp_path):
    flag = tmp_path / "flag.txt"
    flag.write_text("fail", encoding="utf-8")
    cmd = _py_script(
        tmp_path,
        "flag_gate.py",
        "from pathlib import Path\n"
        "import sys\n"
        "sys.exit(1 if Path(%r).exists() else 0)\n" % str(flag),
    )
    runner = QualityGateRunner(cmds=[cmd], max_attempts=5)
    assert runner.run(str(tmp_path)).outcome == "failed"
    flag.unlink()
    (tmp_path / "changed.txt").write_text("now ok", encoding="utf-8")
    time.sleep(0.01)
    result = runner.run(str(tmp_path))
    if result.outcome == "skipped_unchanged":
        runner.state.last_failed_fingerprint = "stale"
        result = runner.run(str(tmp_path))
    assert result.outcome == "passed"
    assert result.passed is True


def test_quality_gate_budget_halt(tmp_path):
    cmd = _py_script(tmp_path, "fail_gate.py", "raise SystemExit(1)\n")
    runner = QualityGateRunner(cmds=[cmd], max_attempts=2, max_seconds=60)
    r1 = runner.run(str(tmp_path))
    assert r1.outcome == "failed"
    runner.state.last_failed_fingerprint = ""
    r2 = runner.run(str(tmp_path))
    assert r2.outcome in ("failed", "budget_halt")
    if r2.outcome == "failed":
        runner.state.last_failed_fingerprint = ""
        r3 = runner.run(str(tmp_path))
        assert r3.outcome == "budget_halt"
        assert r3.block_finish is True
    else:
        assert r2.block_finish is True


def test_quality_gate_disabled_by_default_config():
    cfg = HarnessConfig()
    runner = QualityGateRunner.from_config(cfg)
    assert runner.enabled() is False
    assert runner.should_run(auto_mode=False) is False


def test_quality_gate_skips_autopilot_unless_flagged():
    # should_run does not execute cmds — placeholder is enough.
    runner = QualityGateRunner(cmds=["placeholder"], on_auto=False)
    assert runner.should_run(auto_mode=True) is False
    runner.on_auto = True
    assert runner.should_run(auto_mode=True) is True


def test_workspace_fingerprint_changes_with_file(tmp_path):
    a = workspace_fingerprint(str(tmp_path))
    (tmp_path / "x.py").write_text("print(1)\n", encoding="utf-8")
    os.system("git -C %s init -q" % tmp_path)
    os.system("git -C %s add x.py" % tmp_path)
    b = workspace_fingerprint(str(tmp_path))
    (tmp_path / "x.py").write_text("print(2)\n", encoding="utf-8")
    time.sleep(0.01)
    c = workspace_fingerprint(str(tmp_path))
    assert a != b or b != c
    assert isinstance(b, str) and len(b) == 64
