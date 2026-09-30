"""Tests must not hand a scripted stdlib fake to the whole process.

"<module>.subprocess.Popen" or a patched threading.Thread edits the shared
stdlib module, so other threads (a leftover server, a scheduler) consume the
fake: Windows CI hung at exit and a scripted Popen sequence ran dry. Use
tests._isolation.isolate_module_attr. Process-wide *guards* whose fake only
raises (``setattr(subprocess, "Popen", boom)``) stay allowed.
"""
from __future__ import annotations

import re
from pathlib import Path

_TESTS = Path(__file__).parent
_FORBIDDEN = re.compile(
    r'''["'](?:[a-z_]+\.)+(?:subprocess\.(?:Popen|run|check_output|call)|threading\.Thread)["']'''
    r'''|setattr\(\s*threading\s*,\s*["']Thread["']'''
)


def test_no_process_wide_scripted_stdlib_fakes():
    hits = [
        f"{path.name}:{n}: {line.strip()}"
        for path in sorted(_TESTS.glob("test_*.py"))
        if path.name != Path(__file__).name
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _FORBIDDEN.search(line)
    ]
    assert hits == []
