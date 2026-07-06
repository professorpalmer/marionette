"""Tests for hash-anchored edit foundation (hash_edit / read_file anchors)."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile

import pytest

from harness.hash_edit import (
    HashEditOp,
    annotate_read_content,
    apply_hash_edits,
    apply_hash_edits_to_file,
    compute_range_hash,
    compute_file_hash,
    hash_edit_enabled,
    split_lines,
)
from harness.pilot import build_tools_schema, parse_tool_calls


@pytest.fixture(autouse=True)
def enable_hash_edit(monkeypatch):
    monkeypatch.setenv("HARNESS_HASH_EDIT", "1")


def _lines(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8", newline="") as f:
        return split_lines(f.read())


def test_stale_anchor_rejection(tmp_path):
    fpath = tmp_path / "sample.txt"
    fpath.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    lines = _lines(str(fpath))
    bad = "deadbeef0000"

    ops = [
        HashEditOp(op="replace", anchor=bad, start_line=2, end_line=2, text="BETA"),
    ]
    original = fpath.read_text(encoding="utf-8")
    new_text, result = apply_hash_edits(original, ops)
    assert not result.ok
    assert result.stale_anchors == [bad]
    assert new_text == original
    assert fpath.read_text(encoding="utf-8") == original

    file_result = apply_hash_edits_to_file(str(fpath), ops)
    assert not file_result.ok
    assert fpath.read_text(encoding="utf-8") == original


def test_crlf_normalization(tmp_path):
    fpath = tmp_path / "crlf.txt"
    fpath.write_bytes(b"line one\r\nline two\r\n")

    with open(fpath, "r", encoding="utf-8", newline="") as f:
        original = f.read()
    lines = split_lines(original)
    anchor = compute_range_hash(lines, 1, 1)

    ops = [
        HashEditOp(op="replace", anchor=anchor, start_line=1, end_line=1, text="LINE ONE"),
    ]
    result = apply_hash_edits_to_file(str(fpath), ops)
    assert result.ok

    data = fpath.read_bytes()
    assert b"\r\n" in data
    assert data.decode("utf-8") == "LINE ONE\r\nline two\r\n"


def test_multi_hunk_apply(tmp_path):
    fpath = tmp_path / "multi.txt"
    fpath.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    lines = _lines(str(fpath))

    ops = [
        HashEditOp(
            op="replace",
            anchor=compute_range_hash(lines, 2, 2),
            start_line=2,
            end_line=2,
            text="TWO",
        ),
        HashEditOp(op="insert", after_line=3, text="three-and-a-half"),
        HashEditOp(
            op="delete",
            anchor=compute_range_hash(lines, 4, 4),
            start_line=4,
            end_line=4,
        ),
    ]
    result = apply_hash_edits_to_file(str(fpath), ops)
    assert result.ok
    assert result.applied_ops == 3
    assert fpath.read_text(encoding="utf-8") == "one\nTWO\nthree\nthree-and-a-half\n"


def test_no_partial_writes(tmp_path):
    fpath = tmp_path / "partial.txt"
    fpath.write_text("keep\nchange\nstay\n", encoding="utf-8")
    lines = _lines(str(fpath))
    original = fpath.read_text(encoding="utf-8")

    ops = [
        HashEditOp(
            op="replace",
            anchor=compute_range_hash(lines, 1, 1),
            start_line=1,
            end_line=1,
            text="KEPT",
        ),
        HashEditOp(
            op="replace",
            anchor="000000000000",
            start_line=2,
            end_line=2,
            text="CHANGED",
        ),
    ]
    _, result = apply_hash_edits(original, ops)
    assert not result.ok
    assert fpath.read_text(encoding="utf-8") == original

    file_result = apply_hash_edits_to_file(str(fpath), ops)
    assert not file_result.ok
    assert fpath.read_text(encoding="utf-8") == original


def test_read_file_anchors_when_enabled(tmp_path):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    fpath = tmp_path / "tagged.txt"
    fpath.write_text("a\nb\nc\n", encoding="utf-8")
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp(), repo=str(tmp_path))
    session = ConversationalSession(cfg)

    class _Act:
        kind = "read_file"
        path = "tagged.txt"
        start_line = None
        limit = None

    ok, status, content = session._do_read_file(_Act())
    assert ok
    assert "[@anchor file hash=" in content
    assert "[@/anchor]" in content


def test_hash_edit_schema_and_parse():
    names = {t["function"]["name"] for t in build_tools_schema()}
    assert "hash_edit" in names

    tool_calls = [
        {
            "id": "tc1",
            "type": "function",
            "function": {
                "name": "hash_edit",
                "arguments": json.dumps({
                    "path": "x.py",
                    "ops": [{"op": "insert", "after_line": 0, "text": "# header\n"}],
                }),
            },
        }
    ]
    actions = parse_tool_calls(tool_calls)
    assert len(actions) == 1
    assert actions[0].kind == "hash_edit"
    assert actions[0].path == "x.py"
    assert len(actions[0].arguments["ops"]) == 1


def test_annotate_read_content_range_header():
    body = "[lines 2-3 of 5]\nsecond\nthird\n"
    out = annotate_read_content(body, total_lines=5, start_line=2, end_line=3)
    assert out.startswith("[lines 2-3 of 5]")
    assert "[@anchor range hash=" in out


def test_file_hash_stable():
    lines = ["hello", "world"]
    assert compute_file_hash(lines) == compute_file_hash(lines)


def test_hash_edit_disabled_by_default(monkeypatch):
    monkeypatch.delenv("HARNESS_HASH_EDIT", raising=False)
    assert not hash_edit_enabled()
    names = {t["function"]["name"] for t in build_tools_schema()}
    assert "hash_edit" not in names


def test_hash_edit_session_checkpoint(tmp_path):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession
    from harness.pilot import PilotAction

    repo = tmp_path
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, capture_output=True)
    fpath = repo / "chk.txt"
    fpath.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "chk.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    lines = _lines(str(fpath))
    anchor = compute_range_hash(lines, 1, 1)

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp(), repo=str(repo))
    session = ConversationalSession(cfg)
    assert len(session._checkpoints.list()) == 0

    act = PilotAction(
        kind="hash_edit",
        path="chk.txt",
        arguments={"ops": [{"op": "replace", "anchor": anchor, "start_line": 1, "end_line": 1, "text": "after"}]},
    )

    class _FakePilot:
        name = "fake"
        calls = 0

        def complete(self, task_prompt, *, system=None):
            from pmharness.drivers.openai_compat import DriverResponse
            return DriverResponse(text="")

        def chat(self, messages, *, tools=None, system=None):
            from pmharness.drivers.openai_compat import DriverResponse
            self.calls += 1
            if self.calls == 1:
                return DriverResponse(
                    text="",
                    tokens_out=10,
                    latency_ms=1.0,
                    meta={
                        "tool_calls": [{
                            "id": "tc_h",
                            "type": "function",
                            "function": {
                                "name": "hash_edit",
                                "arguments": json.dumps({"path": act.path, "ops": act.arguments["ops"]}),
                            },
                        }],
                        "finish_reason": "tool_calls",
                    },
                )
            return DriverResponse(text="done", tokens_out=5, latency_ms=1.0, meta={"finish_reason": "stop"})

    session.pilot = _FakePilot()
    events = list(session.send("apply hash edit"))
    assert any(e.kind == "checkpoint" for e in events)
    assert fpath.read_text(encoding="utf-8") == "after\n"
    assert len(session._checkpoints.list()) > 0
