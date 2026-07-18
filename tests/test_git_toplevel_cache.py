"""Bounded TTL cache for harness.paths.git_toplevel."""
from __future__ import annotations

import os
import subprocess
import time
from unittest import mock

import pytest

from harness import paths


def _git_init(repo: str) -> None:
    subprocess.run(
        ["git", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(autouse=True)
def _clear_git_toplevel_cache():
    with paths._git_toplevel_lock:
        paths._git_toplevel_cache.clear()
    yield
    with paths._git_toplevel_lock:
        paths._git_toplevel_cache.clear()


def test_git_toplevel_cache_evicts_when_over_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_GIT_TOPLEVEL_CACHE_CAP", 2)
    roots = []
    for name in ("a", "b", "c"):
        root = tmp_path / name
        root.mkdir()
        _git_init(str(root))
        roots.append(str(root))

    for root in roots:
        assert paths.git_toplevel(root) is not None

    with paths._git_toplevel_lock:
        assert len(paths._git_toplevel_cache) <= 2


def test_git_toplevel_cache_expires_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_GIT_TOPLEVEL_CACHE_TTL_S", 0.05)
    root = tmp_path / "repo"
    root.mkdir()
    _git_init(str(root))

    first = paths.git_toplevel(str(root))
    assert first is not None

    with mock.patch.object(paths.subprocess, "run", wraps=paths.subprocess.run) as wrapped:
        assert paths.git_toplevel(str(root)) is not None
        assert wrapped.call_count == 0
        time.sleep(0.06)
        assert paths.git_toplevel(str(root)) is not None
        assert wrapped.call_count >= 1


def test_git_toplevel_cache_negative_hit_avoids_subprocess(tmp_path, monkeypatch):
    root = os.path.realpath(str(tmp_path / "not-a-repo"))
    os.makedirs(root, exist_ok=True)
    assert paths.git_toplevel(root) is None
    with mock.patch.object(paths.subprocess, "run") as mocked:
        assert paths.git_toplevel(root) is None
        mocked.assert_not_called()
