"""Boot spend/savings survive backend restart within the same Electron app run."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


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
    srv._BOOT_CARRY_COST_USD = 1.25
    srv._BOOT_PILOT_BY_MODEL.clear()
    srv._BOOT_PILOT_BY_MODEL["cursor/grok-4-6"] = {
        "model": "cursor/grok-4-6",
        "est_cost_usd": 1.25,
        "tokens_used": 50_000,
        "tokens_in": 40_000,
        "tokens_out": 10_000,
        "tokens_cached": 12_000,
        "pilot_cache_read_tokens": 12_000,
        "worker_cost_usd": 0.25,
        "worker_tokens_cached": 0.0,
        "provider_cost_usd": 0.0,
        "cache_savings_gross_usd": 0.4,
        "price_in": 5.0,
        "price_out": 25.0,
    }
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
    assert data["pilot_by_model"]["cursor/grok-4-6"]["est_cost_usd"] >= 1.25

    # Simulate a fresh backend process in the same Electron app run.
    for attr in srv._BOOT_METER_ATTRS:
        srv._BOOT_METER_CARRY[attr] = 0.0
    srv._BOOT_PILOT_BY_MODEL.clear()
    srv._BOOT_CARRY_COST_USD = 0.0
    srv._BOOT_REPOS.clear()
    srv._COST_EPOCH = datetime.now(timezone.utc)
    srv._BOOT_USAGE_RESTORED = False

    assert srv._restore_boot_usage() is True
    assert srv._BOOT_METER_CARRY["_tokens_used"] == 50_000
    assert srv._BOOT_METER_CARRY["_tokens_cached"] == 12_000
    assert srv._BOOT_METER_CARRY["_worker_cost_usd"] == 0.25
    assert "cursor/grok-4-6" in srv._BOOT_PILOT_BY_MODEL
    assert float(srv._BOOT_PILOT_BY_MODEL["cursor/grok-4-6"]["est_cost_usd"]) >= 1.25
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


def test_persist_fold_live_does_not_self_deadlock(tmp_path, monkeypatch):
    """Restart path: fold_live=True re-enters persist via fold — must use RLock.

    With a plain Lock(), the owning thread blocks forever on itself and every
    /api/usage poll queues behind it. This test fails by timeout if that
    regresses.
    """
    import harness.api.usage_meters as meters
    import harness.server as srv

    assert isinstance(meters._BOOT_USAGE_PERSIST_LOCK, type(threading.RLock()))

    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-fold-live-reenter")
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    srv._cfg.state_dir = str(tmp_path)
    srv._BOOT_USAGE_RESTORED = False
    for attr in srv._BOOT_METER_ATTRS:
        srv._BOOT_METER_CARRY[attr] = 0.0
    srv._BOOT_REPOS.clear()
    srv._BOOT_CARRY_COST_USD = 0.0

    attrs = {attr: 0 for attr in srv._BOOT_METER_ATTRS}
    attrs["_tokens_used"] = 100
    attrs["_tokens_in"] = 80
    attrs["_tokens_out"] = 20
    runner = SimpleNamespace(_plan_billing=False, **attrs)

    class _FakeRunners:
        def runners(self):
            return [runner]

    fake = _FakeRunners()
    monkeypatch.setattr(meters, "_runners", lambda: fake)
    monkeypatch.setattr(meters, "_pilot", lambda: None)
    monkeypatch.setattr(srv, "_runners", lambda: fake)
    monkeypatch.setattr(srv, "_pilot", lambda: None)

    done = threading.Event()
    errors: list[BaseException] = []

    def _target() -> None:
        try:
            srv._persist_boot_usage(fold_live=True, force=True)
        except BaseException as exc:  # noqa: BLE001 — surface in assertion
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    assert done.wait(5.0), (
        "fold_live=True self-deadlocked on _BOOT_USAGE_PERSIST_LOCK "
        "(expected reentrant RLock)"
    )
    assert not errors, f"persist raised: {errors[0]!r}"
    assert Path(srv._boot_usage_path()).is_file()
    assert float(runner._tokens_used or 0) == 0.0  # folded + zeroed


def test_restore_legacy_carry_missing_worker_tokens_cached_peels_conservatively(
    tmp_path, monkeypatch
):
    """Pre-change carries with worker input but no cache key must not double-count.

    Old blobs folded worker cache into ``_tokens_cached`` without persisting
    ``_worker_tokens_cached``. Restoring that key as 0 would re-add store-job
    cache on top of the process total. Peel the whole cache meter as
    worker-overlappable so lane math stays at the restored process total.
    """
    import harness.server as srv
    from harness.api.cost_accounting import _source_owned_cache_lanes

    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-legacy-cache")
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    srv._cfg.state_dir = str(tmp_path)
    srv._BOOT_USAGE_RESTORED = False
    for attr in srv._BOOT_METER_ATTRS:
        srv._BOOT_METER_CARRY[attr] = 0.0

    path = Path(srv._boot_usage_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    # Legacy shape: worker input present, worker cache key absent.
    path.write_text(
        json.dumps(
            {
                "app_run_id": "run-legacy-cache",
                "cost_epoch": datetime.now(timezone.utc).isoformat(),
                "carry": {
                    "_tokens_used": 55_000,
                    "_tokens_in": 80_000,
                    "_tokens_out": 5_000,
                    "_tokens_cached": 50_000,
                    "_worker_tokens_in": 50_000,
                    "_worker_tokens_out": 5_000,
                    # intentionally omit _worker_tokens_cached
                },
                "carry_cost_usd": 0.01,
                "repos": [],
            }
        ),
        encoding="utf-8",
    )

    assert srv._restore_boot_usage() is True
    assert srv._BOOT_METER_CARRY["_tokens_cached"] == 50_000
    assert srv._BOOT_METER_CARRY["_worker_tokens_in"] == 50_000
    # Conservative peel: all restored cache treated as worker-overlappable.
    assert srv._BOOT_METER_CARRY["_worker_tokens_cached"] == 50_000

    lanes = _source_owned_cache_lanes(
        pilot_tokens_in=int(srv._BOOT_METER_CARRY["_tokens_in"]),
        pilot_tokens_cached=int(srv._BOOT_METER_CARRY["_tokens_cached"]),
        worker_tokens_in=int(srv._BOOT_METER_CARRY["_worker_tokens_in"]),
        worker_tokens_cached=int(srv._BOOT_METER_CARRY["_worker_tokens_cached"]),
        swarm_tokens_in=50_000,
        swarm_tokens_cached=20_000,
    )
    # Must not inflate to 50k+20k; process total stays 50k.
    assert lanes["tokens_cached"] == 50_000
    assert lanes["prompt_cache_read_tokens"] == 50_000


def test_restore_legacy_carry_without_worker_input_leaves_worker_cached_zero(
    tmp_path, monkeypatch
):
    """Missing worker-cache key is harmless when there was no worker input."""
    import harness.server as srv

    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-legacy-pilot")
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    srv._cfg.state_dir = str(tmp_path)
    srv._BOOT_USAGE_RESTORED = False
    for attr in srv._BOOT_METER_ATTRS:
        srv._BOOT_METER_CARRY[attr] = 0.0

    path = Path(srv._boot_usage_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "app_run_id": "run-legacy-pilot",
                "cost_epoch": datetime.now(timezone.utc).isoformat(),
                "carry": {
                    "_tokens_used": 10_000,
                    "_tokens_in": 8_000,
                    "_tokens_out": 2_000,
                    "_tokens_cached": 4_000,
                },
                "carry_cost_usd": 0.0,
                "repos": [],
            }
        ),
        encoding="utf-8",
    )

    assert srv._restore_boot_usage() is True
    assert srv._BOOT_METER_CARRY["_tokens_cached"] == 4_000
    assert srv._BOOT_METER_CARRY["_worker_tokens_cached"] == 0.0
