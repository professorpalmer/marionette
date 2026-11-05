from __future__ import annotations

"""read_file keeps only the window it shows, not the whole file."""

import tempfile
import tracemalloc

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.pilot import PilotAction


def _session(repo):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp(), repo=str(repo))
    return ConversationalSession(cfg)


def _read(session, **kw):
    return session._do_read_file(PilotAction(kind="read_file", path="big.log", **kw))


def test_a_window_of_a_large_file_does_not_load_the_file(tmp_path):
    line = "event " + "x" * 90 + "\n"
    with open(tmp_path / "big.log", "w", encoding="utf-8") as f:
        for _ in range(400_000):
            f.write(line)
    session = _session(tmp_path)
    tracemalloc.start()
    try:
        ok, _status, content = _read(session, start_line=200_000, limit=5)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert ok and content.startswith("[lines 200000-200004 of 400000;")
    # The file is about 39 MB. The old read held it twice.
    assert peak < 8 * 1024 * 1024, peak


def test_a_file_without_newlines_is_cut_at_the_byte_ceiling(tmp_path):
    (tmp_path / "big.log").write_text("z" * 3_000_000, encoding="utf-8")
    ok, _status, content = _read(_session(tmp_path))
    assert ok
    assert content.startswith("z" * 1000)
    assert "... (file truncated to 200KB) ..." in content
