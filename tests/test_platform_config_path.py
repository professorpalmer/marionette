"""Platform-lock path unification: one file for Marionette and Puppetmaster."""

import json
import os

import pytest

from harness import platform_config


@pytest.fixture(autouse=True)
def _clean_platform_env(monkeypatch):
    monkeypatch.delenv(platform_config.TEST_PATH_ENV, raising=False)
    monkeypatch.delenv("PUPPETMASTER_MODELS_PATH", raising=False)
    yield


def test_path_follows_puppetmaster_models_path(monkeypatch, tmp_path):
    """The lock must sit beside the registry Marionette isolated for itself."""
    from puppetmaster.platform_lock import platform_config_path

    models = tmp_path / "pmharness" / "marionette-models.json"
    models.parent.mkdir(parents=True)
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models))

    resolved = platform_config.platform_json_path()
    assert resolved == str(models.parent / "platform.json")
    assert resolved == str(platform_config_path())


def test_test_path_env_overrides_registry(monkeypatch, tmp_path):
    pinned = tmp_path / "pinned-platform.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(tmp_path / "models.json"))
    monkeypatch.setenv(platform_config.TEST_PATH_ENV, str(pinned))
    assert platform_config.platform_json_path() == str(pinned)


def test_server_platform_path_matches_helper(monkeypatch, tmp_path):
    import harness.server as srv

    models = tmp_path / "state" / "marionette-models.json"
    models.parent.mkdir(parents=True)
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models))
    assert srv._get_platform_json_path() == platform_config.platform_json_path()


def test_legacy_migration_seeds_absent_canonical_lock(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy" / "platform.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps({"disabled": ["codex"], "harness_initialized": True}),
        encoding="utf-8",
    )
    canonical = tmp_path / "canonical" / "platform.json"

    report = platform_config.migrate_legacy_platform_config(
        canonical=str(canonical), legacy=str(legacy),
    )
    assert report["migrated"] is True
    assert json.loads(canonical.read_text(encoding="utf-8"))["disabled"] == ["codex"]


def test_legacy_migration_never_overwrites_canonical_choices(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy" / "platform.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"disabled": ["cursor", "agentic"]}), encoding="utf-8")
    canonical = tmp_path / "canonical" / "platform.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_text(json.dumps({"disabled": []}), encoding="utf-8")

    report = platform_config.migrate_legacy_platform_config(
        canonical=str(canonical), legacy=str(legacy),
    )
    assert report["migrated"] is False
    assert report["reason"] == "canonical already configured"
    assert json.loads(canonical.read_text(encoding="utf-8"))["disabled"] == []


def test_toggle_is_visible_to_puppetmaster_lock(monkeypatch, tmp_path):
    """A Settings > Platform toggle must actually narrow the PM adapter set."""
    from puppetmaster import platform_lock

    import harness.server as srv

    models = tmp_path / "state" / "marionette-models.json"
    models.parent.mkdir(parents=True)
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models))
    monkeypatch.delenv(platform_lock.ONLY_ENV, raising=False)

    srv._init_platform_lock()
    enabled = platform_lock.enabled_adapters()
    assert "agentic" in enabled
    assert "cursor" not in enabled
    assert os.path.exists(platform_config.platform_json_path())


def test_read_platform_config_tolerates_corrupt_file(monkeypatch, tmp_path):
    corrupt = tmp_path / "platform.json"
    corrupt.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv(platform_config.TEST_PATH_ENV, str(corrupt))
    assert platform_config.read_platform_config() == {}
