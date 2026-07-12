from __future__ import annotations

import os
import sys
from unittest import mock

from harness.file_reveal import reveal_in_file_manager


def test_reveal_missing_path():
    assert reveal_in_file_manager("") == "missing path"


def test_reveal_not_found(tmp_path):
    missing = tmp_path / "nope.txt"
    assert reveal_in_file_manager(str(missing)) == "Path not found"


def test_reveal_windows_invokes_explorer(tmp_path):
    target = tmp_path / "keep.txt"
    target.write_text("x", encoding="utf-8")
    with mock.patch.object(sys, "platform", "win32"):
        with mock.patch("harness.file_reveal.subprocess.Popen") as popen:
            assert reveal_in_file_manager(str(target)) is None
            args = popen.call_args[0][0]
            assert args[0] == "explorer"
            assert args[1].startswith("/select,")
            assert os.path.normpath(str(target)) in args[1]
