"""Hermetic tests for worktree seed copy-on-write strategy."""

from __future__ import annotations

import errno
import os
import stat
import subprocess
from pathlib import Path

import pytest

from harness.worktree_seed import (
    SeedCopyStats,
    _cleanup_failed_clone_dst,
    _copy_file_with_strategy,
    _copy_into_worktree,
    _staging_path_for,
    reflink_copy_supported,
    reset_copy_capability_cache,
    resolve_copy_strategy,
    seed_worktree_from_goal,
)


@pytest.fixture(autouse=True)
def _reset_reflink_cache():
    reset_copy_capability_cache()
    yield
    reset_copy_capability_cache()


def test_resolve_copy_strategy_defaults_and_normalizes(monkeypatch):
    monkeypatch.delenv("HARNESS_WORKTREE_COPY_STRATEGY", raising=False)
    assert resolve_copy_strategy() == "auto"
    assert resolve_copy_strategy("COPY") == "copy"
    assert resolve_copy_strategy("reflink") == "reflink"
    assert resolve_copy_strategy("bogus") == "auto"
    monkeypatch.setenv("HARNESS_WORKTREE_COPY_STRATEGY", "reflink")
    assert resolve_copy_strategy() == "reflink"


def test_strategy_copy_uses_copy2_only(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    src = repo / "data.bin"
    payload = b"x" * 128
    src.write_bytes(payload)

    stats = SeedCopyStats()
    dst = wt / "data.bin"
    attempts = {"reflink": 0}

    def _boom(*_a, **_k):
        attempts["reflink"] += 1
        raise AssertionError("reflink must not run for strategy=copy")

    monkeypatch.setattr("harness.worktree_seed._try_reflink_copy", _boom)
    _copy_file_with_strategy(str(src), str(dst), "copy", stats)

    assert dst.read_bytes() == payload
    assert stats.copied_files == 1
    assert stats.copied_bytes == len(payload)
    assert stats.cloned_files == 0
    assert stats.fallback_files == 0
    assert attempts["reflink"] == 0


def test_reflink_success_records_cloned(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    src = repo / "cow.bin"
    payload = b"clone-me" * 16
    src.write_bytes(payload)
    src.chmod(0o640)

    stats = SeedCopyStats()

    def _fake_reflink(s, d):
        Path(d).write_bytes(Path(s).read_bytes())
        os.chmod(d, os.stat(s).st_mode)

    monkeypatch.setattr("harness.worktree_seed._try_reflink_copy", _fake_reflink)
    monkeypatch.setattr("harness.worktree_seed.reflink_copy_supported", lambda force_refresh=False: True)

    dst = wt / "cow.bin"
    _copy_file_with_strategy(str(src), str(dst), "reflink", stats)

    assert dst.read_bytes() == payload
    assert stat.S_IMODE(os.stat(dst).st_mode) == stat.S_IMODE(os.stat(src).st_mode)
    assert stats.cloned_files == 1
    assert stats.cloned_bytes == len(payload)
    assert stats.fallback_files == 0


@pytest.mark.parametrize("err", [errno.EXDEV, errno.EOPNOTSUPP])
def test_reflink_fallback_on_clone_error(tmp_path, monkeypatch, err):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    src = repo / "fallback.bin"
    payload = b"fallback-bytes"
    src.write_bytes(payload)

    stats = SeedCopyStats()

    def _fail_reflink(s, d):
        Path(d).write_bytes(b"partial")
        raise OSError(err, os.strerror(err))

    monkeypatch.setattr("harness.worktree_seed._try_reflink_copy", _fail_reflink)
    monkeypatch.setattr("harness.worktree_seed.reflink_copy_supported", lambda force_refresh=False: True)

    dst = wt / "fallback.bin"
    _copy_file_with_strategy(str(src), str(dst), "reflink", stats)

    assert dst.read_bytes() == payload
    assert stats.fallback_files == 1
    assert stats.fallback_bytes == len(payload)
    assert stats.cloned_files == 0


def test_partial_destination_cleanup_before_fallback(tmp_path):
    dst = tmp_path / "partial.bin"
    dst.write_bytes(b"leftover-partial-bytes")
    _cleanup_failed_clone_dst(str(dst))
    assert not dst.exists()


def test_unsupported_platform_zero_behavior_change(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    target = repo / "plain.txt"
    text = "baseline copy2 behavior\n"
    target.write_text(text, encoding="utf-8")

    monkeypatch.setattr("harness.worktree_seed.reflink_copy_supported", lambda force_refresh=False: False)

    result = seed_worktree_from_goal(
        str(repo), str(wt), "edit plain.txt", copy_strategy="auto",
    )
    assert result.paths == ["plain.txt"]
    assert (wt / "plain.txt").read_text(encoding="utf-8") == text
    assert result.copy_stats.cloned_files == 0
    assert result.copy_stats.copied_files == 1
    assert result.copy_stats.fallback_files == 0


def test_seed_content_and_metadata_equality(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    src = repo / "meta.txt"
    src.write_text("metadata check\n", encoding="utf-8")
    os.chmod(src, 0o600)
    mtime = 1_700_000_000.0
    os.utime(src, (mtime, mtime))

    monkeypatch.setattr("harness.worktree_seed.reflink_copy_supported", lambda force_refresh=False: False)

    result = seed_worktree_from_goal(
        str(repo), str(wt), "update meta.txt", copy_strategy="copy",
    )
    dst = wt / "meta.txt"
    assert result.paths == ["meta.txt"]
    assert dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")
    assert stat.S_IMODE(os.stat(dst).st_mode) == stat.S_IMODE(os.stat(src).st_mode)
    assert os.path.getmtime(dst) == pytest.approx(mtime, rel=1e-3)


def test_symlink_escape_blocked_by_confinement(tmp_path):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    outside = tmp_path / "outside.txt"
    repo.mkdir()
    wt.mkdir()
    outside.write_text("secret\n", encoding="utf-8")
    link = repo / "escape.txt"
    link.symlink_to(outside)

    result = seed_worktree_from_goal(str(repo), str(wt), "edit escape.txt")
    assert result.paths == []
    assert not (wt / "escape.txt").exists()


def test_confinement_rejects_escape(tmp_path):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    safe = repo / "inside.txt"
    safe.write_text("ok\n", encoding="utf-8")

    stats = SeedCopyStats()
    escaped = "../outside.txt"
    assert _copy_into_worktree(str(repo), str(wt), escaped, "copy", stats) is False
    assert stats.copied_files == 0

    assert _copy_into_worktree(str(repo), str(wt), "inside.txt", "copy", stats) is True
    assert stats.copied_files == 1


def test_auto_uses_reflink_when_supported(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    (repo / "auto.txt").write_bytes(b"auto")

    monkeypatch.setattr("harness.worktree_seed.reflink_copy_supported", lambda force_refresh=False: True)

    def _fake_reflink(s, d):
        Path(d).write_bytes(Path(s).read_bytes())

    monkeypatch.setattr("harness.worktree_seed._try_reflink_copy", _fake_reflink)

    result = seed_worktree_from_goal(
        str(repo), str(wt), "edit auto.txt", copy_strategy="auto",
    )
    assert result.paths == ["auto.txt"]
    assert result.copy_stats.cloned_files == 1
    assert result.copy_stats.copied_files == 0


def test_reflink_supported_cache(monkeypatch):
    calls = {"n": 0}

    def _probe():
        calls["n"] += 1
        return True

    monkeypatch.setattr("harness.worktree_seed._probe_linux_ficlone", _probe)
    monkeypatch.setattr("harness.worktree_seed.sys.platform", "linux")
    assert reflink_copy_supported(force_refresh=True) is True
    assert reflink_copy_supported() is True
    assert calls["n"] == 1


def test_staging_path_reserved_but_nonexistent(tmp_path):
    dst = tmp_path / "target.bin"
    dst.parent.mkdir(parents=True, exist_ok=True)
    staging = _staging_path_for(str(dst))
    assert staging.startswith(str(tmp_path))
    assert not os.path.lexists(staging)


def test_reflink_requires_nonexistent_staging_path(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    src = repo / "cow.bin"
    payload = b"clone-me" * 16
    src.write_bytes(payload)

    stats = SeedCopyStats()
    seen: dict[str, bool] = {}

    def _fake_reflink(s, d):
        seen["existed"] = os.path.lexists(d)
        Path(d).write_bytes(Path(s).read_bytes())

    monkeypatch.setattr("harness.worktree_seed._try_reflink_copy", _fake_reflink)
    monkeypatch.setattr("harness.worktree_seed.reflink_copy_supported", lambda force_refresh=False: True)

    dst = wt / "cow.bin"
    _copy_file_with_strategy(str(src), str(dst), "reflink", stats)

    assert seen.get("existed") is False
    assert dst.read_bytes() == payload
    assert stats.cloned_files == 1


def test_real_staging_clone_counters_not_mocked_top_helper(tmp_path, monkeypatch):
    """Exercise staging + clone path without mocking ``_copy_file_with_strategy``."""
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    src = repo / "pkg" / "seed.bin"
    src.parent.mkdir(parents=True)
    payload = b"real-staging-clone"
    src.write_bytes(payload)

    monkeypatch.setattr("harness.worktree_seed.reflink_copy_supported", lambda force_refresh=False: True)

    def _clone_via_staging(s, d):
        assert not os.path.lexists(d)
        Path(d).write_bytes(Path(s).read_bytes())

    monkeypatch.setattr("harness.worktree_seed._try_reflink_copy", _clone_via_staging)

    result = seed_worktree_from_goal(
        str(repo), str(wt), "edit pkg/seed.bin", copy_strategy="reflink",
    )
    assert result.paths == ["pkg/seed.bin"]
    assert (wt / "pkg" / "seed.bin").read_bytes() == payload
    assert result.copy_stats.cloned_files == 1
    assert result.copy_stats.fallback_files == 0
    assert list(wt.glob(".pmseed-*")) == []


def test_atomic_replace_preserves_destination_on_failure(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    src = repo / "keep.bin"
    dst = wt / "keep.bin"
    original = b"original-content"
    src.write_bytes(b"new-content-from-source")
    dst.write_bytes(original)

    stats = SeedCopyStats()
    copy_calls = {"n": 0}

    def _fail_copy2(*_a, **_k):
        copy_calls["n"] += 1
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr("harness.worktree_seed.shutil.copy2", _fail_copy2)

    assert _copy_into_worktree(str(repo), str(wt), "keep.bin", "copy", stats) is False
    assert copy_calls["n"] >= 1
    assert dst.read_bytes() == original
    assert stats.copied_files == 0
    assert list(wt.glob(".pmseed-*")) == []


def test_new_destination_failure_leaves_no_partial_file(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    src = repo / "fresh.bin"
    dst = wt / "fresh.bin"
    src.write_bytes(b"seed-me")

    stats = SeedCopyStats()
    monkeypatch.setattr(
        "harness.worktree_seed.shutil.copy2",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError(errno.ENOSPC, "no space")),
    )

    assert not dst.exists()
    assert _copy_into_worktree(str(repo), str(wt), "fresh.bin", "copy", stats) is False
    assert not dst.exists()
    assert stats.copied_files == 0
    assert list(wt.glob(".pmseed-*")) == []


def test_same_size_same_mtime_differing_content_replaced(tmp_path):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    src = repo / "masked.bin"
    dst = wt / "masked.bin"
    mtime = 1_700_000_000.0
    src.write_bytes(b"aaaa")
    dst.write_bytes(b"bbbb")
    os.utime(src, (mtime, mtime))
    os.utime(dst, (mtime, mtime))

    stats = SeedCopyStats()
    assert _copy_into_worktree(str(repo), str(wt), "masked.bin", "copy", stats) is True
    assert dst.read_bytes() == b"aaaa"
    assert os.path.getmtime(dst) == pytest.approx(mtime, rel=1e-3)
    assert stats.copied_files == 1


def test_atomic_replace_overwrites_via_staging(tmp_path):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    src = repo / "swap.bin"
    dst = wt / "swap.bin"
    src.write_bytes(b"new-bytes")
    dst.write_bytes(b"old-bytes")

    stats = SeedCopyStats()
    assert _copy_into_worktree(str(repo), str(wt), "swap.bin", "copy", stats) is True
    assert dst.read_bytes() == b"new-bytes"
    assert stats.copied_files == 1
    staging_leftovers = list(wt.glob(".pmseed-*"))
    assert staging_leftovers == []


def test_windows_import_without_fcntl(monkeypatch):
    import builtins
    import importlib

    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "fcntl":
            raise ImportError("No module named 'fcntl'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr("harness.worktree_seed.sys.platform", "win32")
    mod = importlib.reload(importlib.import_module("harness.worktree_seed"))
    mod.reset_copy_capability_cache()
    assert mod._import_fcntl() is None
    assert mod.reflink_copy_supported(force_refresh=True) is False


def test_dynamic_seed_with_git_dirty_paths(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    dirty = repo / "addons" / "kotoba" / "ad.html"
    dirty.parent.mkdir(parents=True)
    dirty.write_text("<html>ad</html>\n", encoding="utf-8")

    monkeypatch.setattr("harness.worktree_seed.reflink_copy_supported", lambda force_refresh=False: False)

    result = seed_worktree_from_goal(str(repo), str(wt), "fix the kotoba ad", copy_strategy="copy")
    assert "addons/kotoba/ad.html" in result.paths
    assert result.copy_stats.copied_files >= 1
