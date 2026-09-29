"""An indexer that hits its timeout is stopped with its whole process tree."""
import os
import sys
import time

import pytest

import harness.api.codegraph_index as cgi

CHILD = (
    "import subprocess, sys, time;"
    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
    "open(sys.argv[1], 'w').write(str(c.pid));"
    "time.sleep(60)"
)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # a zombie reaped by nobody still answers kill(0)
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_timed_out_indexer_takes_its_grandchild_with_it(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    pid_file = tmp_path / "grandchild.pid"
    deps = cgi.CodegraphIndexDeps(
        puppetmaster_available=lambda: True,
        puppetmaster_cmd=lambda *a: [sys.executable, "-c", CHILD, str(pid_file)],
        diag=lambda *a, **k: None,
        get_state_dir=lambda: str(tmp_path),
        get_repo=lambda: str(repo),
    )
    monkeypatch.setattr(cgi, "_deps", deps)
    monkeypatch.setattr(cgi, "INDEX_TIMEOUT_S", 1)
    monkeypatch.setattr(cgi, "prepare_codegraph_scope", lambda path: {"verdict": "ok"})
    monkeypatch.setattr(cgi, "codegraph_index_log_path", lambda: str(tmp_path / "index.log"))
    monkeypatch.setattr(cgi, "codegraph_index_proc", None)
    monkeypatch.setattr(cgi, "codegraph_status", "none")
    monkeypatch.setattr(cgi, "codegraph_status_reason", None)
    monkeypatch.setattr(cgi, "codegraph_fail_until", {})
    monkeypatch.setattr(cgi, "codegraph_status_cache", {})
    cgi.index_codegraph_bg(str(repo))
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while (cgi.codegraph_index_proc is not None or "timed out" not in str(cgi.codegraph_status_reason)) \
            and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    try:
        assert "timed out" in str(cgi.codegraph_status_reason)
        assert not _alive(grandchild)
    finally:
        try:
            os.kill(grandchild, 9)
        except OSError:
            pass
