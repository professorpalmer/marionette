"""Unit tests for ConversationalSession._get_codegraph_context.

The method shells out to `python -m puppetmaster codegraph search <query>`,
parses path:line hits, reads a small source window, and returns a bounded
<codegraph-context> block -- or "" (no exception) when codegraph is missing or
errors. Tests stub subprocess.run so they are deterministic and offline.
"""

import os
import subprocess

from harness.conversation import ConversationalSession
from harness.config import HarnessConfig


def _make_session(repo: str) -> ConversationalSession:
    # Bypass the heavy __init__ (which builds a live pilot); we only need
    # self.config for the method under test.
    sess = ConversationalSession.__new__(ConversationalSession)
    sess.config = HarnessConfig(repo=repo)
    return sess


def test_get_codegraph_context_wellformed_and_bounded(tmp_path, monkeypatch):
    src = tmp_path / "pkg" / "mod.py"
    src.parent.mkdir(parents=True)
    lines = [f"line {i}\n" for i in range(1, 41)]
    lines[19] = "TARGET_SYMBOL = 42\n"  # line 20
    src.write_text("".join(lines))

    fake_stdout = "pkg/mod.py:20  TARGET_SYMBOL definition\n"

    class _FakeCompleted:
        returncode = 0
        stdout = fake_stdout

    def _fake_run(cmd, **kwargs):
        assert kwargs.get("cwd") == str(tmp_path)
        assert "codegraph" in cmd and "search" in cmd
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    sess = _make_session(str(tmp_path))
    block = sess._get_codegraph_context("TARGET_SYMBOL")

    assert block.startswith("<codegraph-context>")
    assert block.rstrip().endswith("</codegraph-context>")
    assert "pkg/mod.py:20" in block
    assert "TARGET_SYMBOL = 42" in block
    # Window is +/-8 lines, so far-away lines must be excluded.
    assert "line 1\n" not in block
    assert "line 40" not in block
    # Bounded well under any reasonable cap.
    assert len(block.encode("utf-8")) <= 4096 + 64


def test_get_codegraph_context_noop_on_failure(tmp_path, monkeypatch):
    def _boom(cmd, **kwargs):
        raise FileNotFoundError("no puppetmaster")

    monkeypatch.setattr(subprocess, "run", _boom)

    sess = _make_session(str(tmp_path))
    # Must not raise; degrades to empty string.
    assert sess._get_codegraph_context("anything") == ""


def test_get_codegraph_context_empty_when_no_hits(tmp_path, monkeypatch):
    class _FakeCompleted:
        returncode = 0
        stdout = "no results found\n"

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _FakeCompleted())

    sess = _make_session(str(tmp_path))
    assert sess._get_codegraph_context("nope") == ""


def test_get_codegraph_context_empty_without_repo(monkeypatch):
    called = {"n": 0}

    def _run(cmd, **kw):
        called["n"] += 1
        raise AssertionError("should not shell out without a repo")

    monkeypatch.setattr(subprocess, "run", _run)
    sess = ConversationalSession.__new__(ConversationalSession)
    sess.config = HarnessConfig(repo="")
    assert sess._get_codegraph_context("q") == ""
    assert called["n"] == 0
