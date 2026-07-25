"""HARNESS_REPO publish must not clobber a different live workspace."""

from __future__ import annotations

import os

from harness.repo_env import publish_harness_repo, sync_harness_repo_from_cfg


def test_publish_does_not_clobber_different_live_repo(monkeypatch, tmp_path):
    a = tmp_path / "repo_a"
    b = tmp_path / "repo_b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("HARNESS_REPO", str(a))
    publish_harness_repo(str(b), force=False)
    assert os.environ["HARNESS_REPO"] == str(a)


def test_publish_fills_empty_env(monkeypatch, tmp_path):
    a = tmp_path / "repo_a"
    a.mkdir()
    monkeypatch.delenv("HARNESS_REPO", raising=False)
    publish_harness_repo(str(a), force=False)
    assert os.environ["HARNESS_REPO"] == str(a)


def test_force_sync_from_cfg_overrides(monkeypatch, tmp_path):
    a = tmp_path / "repo_a"
    b = tmp_path / "repo_b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("HARNESS_REPO", str(b))

    class Cfg:
        repo = str(a)

    sync_harness_repo_from_cfg(Cfg())
    assert os.environ["HARNESS_REPO"] == str(a)


def test_session_switch_repo_env_holds_after_deferred_style_clobber(
    monkeypatch, tmp_path
):
    """Simulate the release-gate flake: late runner build must not undo switch."""
    a = tmp_path / "repo_a"
    b = tmp_path / "repo_b"
    a.mkdir()
    b.mkdir()
    # Live workspace after sessions/switch.
    monkeypatch.setenv("HARNESS_REPO", str(a))
    # Late deferred ConversationalSession for the previous workspace.
    publish_harness_repo(str(b), force=False)
    assert os.environ["HARNESS_REPO"] == str(a)
