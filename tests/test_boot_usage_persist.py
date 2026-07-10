"""Boot spend/savings survive backend restart within the same Electron app run."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def test_persist_and_restore_boot_usage_same_app_run(tmp_path, monkeypatch):
    import harness.server as srv

    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-test-abc")
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    srv._cfg.state_dir = str(tmp_path)
    srv._BOOT_USAGE_RESTORED = False
    for attr in srv._BOOT_METER_ATTRS:
        srv._BOOT_METER_CARRY[attr] = 0.0
    srv._BOOT_REPOS.clear()

    srv._BOOT_METER_CARRY["_tokens_used"] = 50_000
    srv._BOOT_METER_CARRY["_tokens_in"] = 40_000
    srv._BOOT_METER_CARRY["_tokens_out"] = 10_000
    srv._BOOT_METER_CARRY["_tokens_cached"] = 12_000
    srv._BOOT_METER_CARRY["_worker_cost_usd"] = 0.25
    epoch = datetime.now(timezone.utc) - timedelta(hours=1)
    srv._COST_EPOCH = epoch
    repo = tmp_path / "repo"
    repo.mkdir()
    srv._BOOT_REPOS.add(str(repo.resolve()))

    srv._persist_boot_usage(fold_live=False, force=True)
    path = Path(srv._boot_usage_path())
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["app_run_id"] == "run-test-abc"
    assert data["carry"]["_tokens_cached"] == 12_000

    # Simulate a fresh backend process in the same Electron app run.
    for attr in srv._BOOT_METER_ATTRS:
        srv._BOOT_METER_CARRY[attr] = 0.0
    srv._BOOT_REPOS.clear()
    srv._COST_EPOCH = datetime.now(timezone.utc)
    srv._BOOT_USAGE_RESTORED = False

    assert srv._restore_boot_usage() is True
    assert srv._BOOT_METER_CARRY["_tokens_used"] == 50_000
    assert srv._BOOT_METER_CARRY["_tokens_cached"] == 12_000
    assert srv._BOOT_METER_CARRY["_worker_cost_usd"] == 0.25
    assert abs((srv._COST_EPOCH - epoch).total_seconds()) < 1.0
    assert str(repo.resolve()) in srv._BOOT_REPOS


def test_restore_skips_mismatched_app_run(tmp_path, monkeypatch):
    import harness.server as srv

    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-old")
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    srv._cfg.state_dir = str(tmp_path)
    srv._BOOT_USAGE_RESTORED = False
    for attr in srv._BOOT_METER_ATTRS:
        srv._BOOT_METER_CARRY[attr] = 0.0

    srv._BOOT_METER_CARRY["_tokens_used"] = 9_000
    srv._persist_boot_usage(fold_live=False, force=True)

    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-new")
    for attr in srv._BOOT_METER_ATTRS:
        srv._BOOT_METER_CARRY[attr] = 0.0
    srv._BOOT_USAGE_RESTORED = False

    assert srv._restore_boot_usage() is False
    assert srv._BOOT_METER_CARRY["_tokens_used"] == 0.0


def test_persist_noop_without_app_run_id(tmp_path, monkeypatch):
    import harness.server as srv

    monkeypatch.delenv("HARNESS_APP_RUN_ID", raising=False)
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    srv._cfg.state_dir = str(tmp_path)
    srv._BOOT_METER_CARRY["_tokens_used"] = 100
    srv._persist_boot_usage(fold_live=False, force=True)
    assert not Path(srv._boot_usage_path()).is_file()
