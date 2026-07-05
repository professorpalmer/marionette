"""Tests for the generalized PATH sanitizer that demotes .app/Contents/
entries to the tail of PATH for ALL Marionette-spawned subprocesses.

Root cause background: when the Electron host (Cursor.app, VS Code.app, ...)
launches the Python backend, its bundled runtimes (node, python, helpers)
live under `/Applications/<App>.app/Contents/...` and end up on PATH ahead of
clean system prefixes. Spawning those cross-app binaries triggers the macOS
TCC "wants to access data from other apps" prompt and pulls in foreign
runtimes. The `sanitized_path()` / `sanitized_env()` helpers MOVE (not
delete) those entries to the end so clean prefixes win but nothing that only
lives inside a bundle becomes unrunnable.
"""

import os

from harness import _exec
from harness._exec import sanitized_path, sanitized_env


CURSOR_HELPERS = "/Applications/Cursor.app/Contents/Resources/app/resources/helpers"
CURSOR_BIN = "/Applications/Cursor.app/Contents/MacOS"
HOMEBREW = "/opt/homebrew/bin"
USR_BIN = "/usr/bin"
USR_LOCAL_BIN = "/usr/local/bin"


def _join(*parts: str) -> str:
    return os.pathsep.join(parts)


def test_sanitized_path_moves_cursor_entry_to_end():
    """The single-entry-in-the-middle case from the task spec."""
    original = _join(HOMEBREW, CURSOR_HELPERS, USR_BIN)
    result = sanitized_path(original)
    parts = result.split(os.pathsep)

    # Clean entries kept in their original relative order, ahead of the moved
    # Cursor.app entry.
    assert parts == [HOMEBREW, USR_BIN, CURSOR_HELPERS]
    # And explicitly: /opt/homebrew/bin and /usr/bin come ahead and in order.
    assert parts.index(HOMEBREW) < parts.index(USR_BIN) < parts.index(CURSOR_HELPERS)


def test_sanitized_path_all_clean_preserves_order():
    """An all-clean PATH is returned with order fully preserved."""
    original = _join(HOMEBREW, USR_LOCAL_BIN, USR_BIN, "/bin")
    result = sanitized_path(original)
    assert result == original


def test_sanitized_path_dedup_keeps_first_occurrence():
    """De-duplication preserves the FIRST occurrence's position."""
    original = _join(HOMEBREW, USR_BIN, HOMEBREW, USR_LOCAL_BIN, USR_BIN)
    result = sanitized_path(original)
    parts = result.split(os.pathsep)
    assert parts == [HOMEBREW, USR_BIN, USR_LOCAL_BIN]


def test_sanitized_path_dedup_across_kept_and_moved_groups():
    """Duplicate app-bundle entries are also collapsed and end up at the tail
    in first-occurrence order."""
    original = _join(
        HOMEBREW, CURSOR_HELPERS, USR_BIN, CURSOR_HELPERS, CURSOR_BIN, HOMEBREW
    )
    parts = sanitized_path(original).split(os.pathsep)
    assert parts == [HOMEBREW, USR_BIN, CURSOR_HELPERS, CURSOR_BIN]


def test_sanitized_path_multiple_bundle_entries_preserve_relative_order():
    """Within the moved group the relative order of the FIRST occurrences is
    preserved."""
    original = _join(CURSOR_HELPERS, HOMEBREW, CURSOR_BIN, USR_BIN)
    parts = sanitized_path(original).split(os.pathsep)
    # Clean entries first (in original relative order), then bundle entries
    # (in original relative order).
    assert parts == [HOMEBREW, USR_BIN, CURSOR_HELPERS, CURSOR_BIN]


def test_sanitized_path_empty_and_none_do_not_raise():
    """Empty / None inputs are handled without raising."""
    assert sanitized_path("") == ""
    # None means "use os.environ['PATH']" -- must not raise regardless of
    # what the ambient PATH is.
    result = sanitized_path(None)
    assert isinstance(result, str)


def test_sanitized_path_uses_os_environ_by_default(monkeypatch):
    monkeypatch.setenv("PATH", _join(HOMEBREW, CURSOR_HELPERS, USR_BIN))
    parts = sanitized_path().split(os.pathsep)
    assert parts == [HOMEBREW, USR_BIN, CURSOR_HELPERS]


def test_sanitized_path_reuses_is_app_bundle_path(monkeypatch):
    """The bundle test MUST reuse _is_app_bundle_path so any future refinement
    (e.g. case handling) applies here too."""
    calls = []
    real = _exec._is_app_bundle_path

    def spy(p):
        calls.append(p)
        return real(p)

    monkeypatch.setattr(_exec, "_is_app_bundle_path", spy)
    sanitized_path(_join(HOMEBREW, CURSOR_HELPERS, USR_BIN))
    assert HOMEBREW in calls and CURSOR_HELPERS in calls and USR_BIN in calls


# -- sanitized_env ---------------------------------------------------------


def test_sanitized_env_returns_copy_with_sanitized_path():
    src = {
        "PATH": _join(HOMEBREW, CURSOR_HELPERS, USR_BIN),
        "FOO": "bar",
        "HOME": "/tmp/whatever",
    }
    out = sanitized_env(src)

    assert isinstance(out, dict)
    # It's a real copy, not the same object.
    assert out is not src
    # Non-PATH keys are preserved verbatim.
    assert out["FOO"] == "bar"
    assert out["HOME"] == "/tmp/whatever"
    # PATH is sanitized.
    assert out["PATH"].split(os.pathsep) == [HOMEBREW, USR_BIN, CURSOR_HELPERS]


def test_sanitized_env_does_not_mutate_input():
    src = {"PATH": _join(HOMEBREW, CURSOR_HELPERS, USR_BIN), "X": "1"}
    original_path = src["PATH"]
    original_snapshot = dict(src)

    _ = sanitized_env(src)

    assert src["PATH"] == original_path
    assert src == original_snapshot


def test_sanitized_env_does_not_mutate_os_environ(monkeypatch):
    monkeypatch.setenv("PATH", _join(HOMEBREW, CURSOR_HELPERS, USR_BIN))
    snapshot = dict(os.environ)
    original_path = os.environ["PATH"]

    out = sanitized_env()

    # os.environ untouched.
    assert os.environ["PATH"] == original_path
    assert dict(os.environ) == snapshot
    # Returned copy has the sanitized PATH.
    assert out["PATH"].split(os.pathsep) == [HOMEBREW, USR_BIN, CURSOR_HELPERS]
    # And the returned dict is not os.environ itself.
    assert out is not os.environ


def test_sanitized_env_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("PATH", _join(HOMEBREW, USR_BIN))
    monkeypatch.setenv("PMHARNESS_TEST_MARKER", "keep-me")
    out = sanitized_env()
    assert out.get("PMHARNESS_TEST_MARKER") == "keep-me"
    assert out["PATH"] == _join(HOMEBREW, USR_BIN)


def test_sanitized_env_handles_missing_path_key():
    """An env dict without PATH must not raise, must not synthesize a PATH,
    and must not leak the ambient os.environ PATH into the returned copy."""
    src = {"FOO": "bar"}
    out = sanitized_env(src)
    assert out["FOO"] == "bar"
    # No PATH key was added out of thin air.
    assert "PATH" not in out
    # Input dict untouched.
    assert "PATH" not in src


def test_sanitized_env_empty_dict_does_not_raise():
    out = sanitized_env({})
    assert isinstance(out, dict)
    # Should not raise; contents are best-effort.
