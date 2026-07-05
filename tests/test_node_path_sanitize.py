"""Regression tests for the macOS TCC "access data from other apps" storm.

Root cause: when the Electron host (e.g. Cursor) launches the Python backend,
its bundled node at `/Applications/Cursor.app/Contents/Resources/app/resources/helpers/node`
is discoverable on PATH. The previous `_ensure_node_on_path()` accepted ANY
node that `shutil.which("node")` returned, so every subsequent CodeGraph spawn
ran on Cursor's node -- a cross-app binary launch that macOS TCC flags with
the recurring "wants to access data from other apps" prompt.

The fix: reject a node whose path is inside a `.app/Contents/` bundle and
prepend a clean candidate dir (e.g. /opt/homebrew/bin) so a fresh child's
`shutil.which("node")` resolves to the clean node.
"""

import os
import shutil

import pytest

from harness import _exec


CURSOR_NODE = "/Applications/Cursor.app/Contents/Resources/app/resources/helpers/node"
HOMEBREW_DIR = "/opt/homebrew/bin"
HOMEBREW_NODE = "/opt/homebrew/bin/node"
USR_BIN_NODE = "/usr/bin/node"


@pytest.fixture(autouse=True)
def _reset_ensured_flag():
    _exec._NODE_PATH_ENSURED = False
    yield
    _exec._NODE_PATH_ENSURED = False


def test_is_app_bundle_path_recognizes_cursor_node():
    assert _exec._is_app_bundle_path(CURSOR_NODE) is True


def test_is_app_bundle_path_accepts_clean_paths():
    assert _exec._is_app_bundle_path(HOMEBREW_NODE) is False
    assert _exec._is_app_bundle_path(USR_BIN_NODE) is False
    # Empty / weird inputs must never claim to be app-bundle paths.
    assert _exec._is_app_bundle_path("") is False
    assert _exec._is_app_bundle_path("node") is False


def test_is_app_bundle_path_case_insensitive_and_generic_apps():
    # Any .app/Contents/ segment, not just Cursor.
    assert _exec._is_app_bundle_path(
        "/Applications/Visual Studio Code.app/Contents/Resources/app/node"
    ) is True
    # Case insensitive.
    assert _exec._is_app_bundle_path(
        "/APPLICATIONS/Foo.APP/CONTENTS/Resources/node"
    ) is True


def test_ensure_node_on_path_rejects_cursor_and_prepends_clean(monkeypatch, tmp_path):
    """Core regression: which() returns Cursor.app's node, but a clean
    /opt/homebrew/bin/node exists -> PATH must be rewritten so the clean dir
    comes BEFORE any Cursor.app path."""
    # Simulate the Electron-launched backend PATH: Cursor.app's helpers is on
    # PATH, but not the clean homebrew prefix.
    cursor_helpers_dir = os.path.dirname(CURSOR_NODE)
    monkeypatch.setenv(
        "PATH", os.pathsep.join([cursor_helpers_dir, "/usr/bin", "/bin"])
    )

    # `which("node")` finds Cursor's node (the buggy resolution).
    monkeypatch.setattr(shutil, "which", lambda cmd: CURSOR_NODE if cmd == "node" else None)

    # Candidate dirs: homebrew (clean) exists; also include a .app dir that
    # should be filtered out even if it contains a node binary.
    poisoned_app_dir = "/Applications/Cursor.app/Contents/Resources/app/resources/helpers"
    monkeypatch.setattr(
        _exec, "_node_candidate_dirs", lambda: [poisoned_app_dir, HOMEBREW_DIR]
    )

    def fake_isfile(path):
        # Both the poisoned dir and the clean dir "contain" a node, but the
        # poisoned one must be rejected by _is_app_bundle_path filtering.
        return path in {
            os.path.join(poisoned_app_dir, "node"),
            HOMEBREW_NODE,
        }

    monkeypatch.setattr(os.path, "isfile", fake_isfile)

    _exec._NODE_PATH_ENSURED = False
    _exec._ensure_node_on_path()

    parts = os.environ["PATH"].split(os.pathsep)
    # Homebrew must be prepended.
    assert parts[0] == HOMEBREW_DIR, parts
    # And it must appear BEFORE any .app/Contents/ path.
    hb_idx = parts.index(HOMEBREW_DIR)
    app_indices = [i for i, p in enumerate(parts) if _exec._is_app_bundle_path(p)]
    assert app_indices, "test setup should include an .app path on PATH"
    assert hb_idx < min(app_indices), parts


def test_ensure_node_on_path_leaves_clean_which_alone_and_is_idempotent(monkeypatch):
    """When which() returns a clean node, PATH is untouched and a second
    call is a no-op (idempotency via _NODE_PATH_ENSURED)."""
    original_path = os.pathsep.join([HOMEBREW_DIR, "/usr/bin", "/bin"])
    monkeypatch.setenv("PATH", original_path)

    monkeypatch.setattr(shutil, "which", lambda cmd: HOMEBREW_NODE if cmd == "node" else None)

    # If the function wrongly falls through to candidate scanning, this list
    # would let it prepend something and fail the assertion below.
    candidate_calls = {"n": 0}

    def fake_candidates():
        candidate_calls["n"] += 1
        return [HOMEBREW_DIR]

    monkeypatch.setattr(_exec, "_node_candidate_dirs", fake_candidates)

    _exec._NODE_PATH_ENSURED = False
    _exec._ensure_node_on_path()
    assert os.environ["PATH"] == original_path
    # Second call: idempotent, no PATH mutation and no re-scan.
    _exec._ensure_node_on_path()
    assert os.environ["PATH"] == original_path
    assert candidate_calls["n"] == 0, "clean which() should skip candidate scan entirely"


def test_ensure_node_on_path_skips_app_bundle_candidates(monkeypatch, tmp_path):
    """Even the candidate-scan branch must reject .app/Contents/ dirs."""
    monkeypatch.setattr(shutil, "which", lambda cmd: None)  # no node on PATH at all

    real_clean_dir = tmp_path / "clean-nodebin"
    real_clean_dir.mkdir()
    node_bin = real_clean_dir / ("node.exe" if os.name == "nt" else "node")
    node_bin.write_text("#!/bin/sh\necho v22\n")
    node_bin.chmod(0o755)

    poisoned = "/Applications/Cursor.app/Contents/Resources/app/resources/helpers"
    monkeypatch.setattr(
        _exec, "_node_candidate_dirs", lambda: [poisoned, str(real_clean_dir)]
    )
    # Pretend the poisoned dir also has a node binary; the filter must skip it.
    real_isfile = os.path.isfile

    def fake_isfile(path):
        if path.startswith(poisoned):
            return True
        return real_isfile(path)

    monkeypatch.setattr(os.path, "isfile", fake_isfile)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    _exec._NODE_PATH_ENSURED = False
    _exec._ensure_node_on_path()

    parts = os.environ["PATH"].split(os.pathsep)
    assert parts[0] == str(real_clean_dir), parts
    # No .app path was injected.
    assert not any(_exec._is_app_bundle_path(p) for p in parts[:1])
