"""The Windows hidden-console default must cover every subprocess site without
clobbering deliberate console choices (wiki_backend's DETACHED_PROCESS, a
debug CREATE_NEW_CONSOLE)."""
import os
import subprocess
import sys

import pytest

from harness import win_console


def test_effective_creationflags_adds_no_window_by_default():
    no_window = win_console._CREATE_NO_WINDOW
    assert win_console.effective_creationflags(0) == no_window
    group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    assert win_console.effective_creationflags(group) == group | no_window


def test_effective_creationflags_respects_explicit_console_choices():
    for explicit in (
        win_console._CREATE_NEW_CONSOLE,
        win_console._DETACHED_PROCESS,
        win_console._CREATE_NO_WINDOW,
        win_console._DETACHED_PROCESS | win_console._CREATE_NO_WINDOW,
    ):
        assert win_console.effective_creationflags(explicit) == explicit


@pytest.mark.skipif(os.name != "nt", reason="Windows console semantics")
def test_hide_child_consoles_is_installed_and_children_still_run():
    # Importing harness (done above) installs the default on Windows.
    assert subprocess.Popen.__init__ is win_console._hidden_popen_init
    proc = subprocess.run(
        [sys.executable, "-c", "print('ok')"], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0
    assert "ok" in proc.stdout
