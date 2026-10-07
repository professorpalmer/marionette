"""Tests for real pilot agent tools (read_file, write_file, run_command, list_dir)."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, is_safe_path
from harness.hash_edit import compute_range_hash
from harness.pilot import PilotAction, build_tools_schema
from pmharness.drivers.base import chat_completions_messages


@dataclass
class FakeResponse:
    text: str
    error: str = ""
    tokens_out: int = 0
    tokens_in: int = 0


def test_is_safe_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        # Inside workspace
        assert is_safe_path(os.path.join(real_tmp, "foo.py"), real_tmp) is True
        assert is_safe_path(os.path.join(real_tmp, "sub/bar.py"), real_tmp) is True
        # Workspace itself
        assert is_safe_path(real_tmp, real_tmp) is True
        # Outside workspace
        assert is_safe_path(os.path.join(real_tmp, "../outside.py"), real_tmp) is False
        assert is_safe_path("/etc/passwd", real_tmp) is False


def test_agent_tools_execution():
    # Manual mkdtemp + best-effort cleanup instead of TemporaryDirectory: on
    # Windows under Python 3.9 a still-closing session subprocess handle makes
    # the context manager's rmtree retry loop recurse to death (RecursionError
    # in shutil). ignore_errors leaves the temp dir for the OS to reap instead
    # of failing the test on teardown.
    tmpdir = tempfile.mkdtemp()
    try:
        real_tmp = os.path.realpath(tmpdir)
        cfg = HarnessConfig(repo=real_tmp, swarm_adapter="demo")
        session = ConversationalSession(cfg)

        class FakePilot:
            def __init__(self):
                self.calls = 0
            def complete(self, prompt, system=None):
                self.calls += 1
                if self.calls == 1:
                    return FakeResponse(text=json.dumps({
                        "say": "Writing file now",
                        "actions": [
                            {"kind": "write_file", "path": "hello.txt", "content": "hello world"}
                        ]
                    }))
                elif self.calls == 2:
                    return FakeResponse(text=json.dumps({
                        "say": "Reading file now",
                        "actions": [
                            {"kind": "read_file", "path": "hello.txt"}
                        ]
                    }))
                elif self.calls == 3:
                    return FakeResponse(text=json.dumps({
                        "say": "Running command now",
                        "actions": [
                            {"kind": "run_command", "command": "echo hi"}
                        ]
                    }))
                elif self.calls == 4:
                    return FakeResponse(text=json.dumps({
                        "say": "Listing dir now",
                        "actions": [
                            {"kind": "list_dir", "path": ""}
                        ]
                    }))
                else:
                    return FakeResponse(text=json.dumps({
                        "say": "Done",
                        "actions": []
                    }))

        session.pilot = FakePilot()
        events = list(session.send("start"))

        # Verify that hello.txt was created and has correct content
        target_file = os.path.join(real_tmp, "hello.txt")
        assert os.path.exists(target_file)
        with open(target_file, "r") as f:
            assert f.read() == "hello world"

        # Check that events have action_start and action_result for all kinds
        kinds_started = [e.data.get("kind") for e in events if e.kind == "action_start"]
        assert "write_file" in kinds_started
        assert "read_file" in kinds_started
        assert "run_command" in kinds_started
        assert "list_dir" in kinds_started

        # Verify confinement rejection
        class TraversalPilot:
            def __init__(self):
                self.calls = 0
            def complete(self, prompt, system=None):
                self.calls += 1
                if self.calls == 1:
                    return FakeResponse(text=json.dumps({
                        "say": "Trying traversal",
                        "actions": [
                            {"kind": "read_file", "path": "../../etc/passwd"}
                        ]
                    }))
                return FakeResponse(text=json.dumps({
                    "say": "Done",
                    "actions": []
                }))

        session_traversal = ConversationalSession(cfg)
        session_traversal.pilot = TraversalPilot()
        trav_events = list(session_traversal.send("start"))
        
        # Verify traversal was blocked
        results = [e.data for e in trav_events if e.kind == "action_result"]
        assert len(results) > 0
        assert "rejected" in results[0].get("error", "").lower() or "traversal" in results[0].get("error", "").lower()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_run_command_survives_cancel_poisoned_after_action_start():
    """Regression for the "every shell command dies but reads work" bug.

    A shared _cancel flag set mid-turn (e.g. explicit Stop / interrupt, or the
    historical autopilot path that cancelled on SSE disconnect) used to poison
    the next run_command: the runner launched with the flag already set and was
    killed on the spot -- exit 130, "[interrupted by user]" -- even though nothing
    was stopping THIS command. read_file/list_dir never consult the flag, which is
    why they kept working while every run_command died.

    We reproduce it deterministically: consume the generator and set _cancel the
    instant the run_command action_start is emitted (i.e. right before the runner
    launches). With edge-triggered cancellation the command must still complete."""
    # mkdtemp + ignore_errors teardown, not TemporaryDirectory: on Windows the
    # spawned subprocess can hold the dir handle a beat past completion, and
    # TemporaryDirectory's cleanup raises WinError 32 (flaky CI teardown).
    tmpdir = tempfile.mkdtemp()
    try:
        real_tmp = os.path.realpath(tmpdir)
        cfg = HarnessConfig(repo=real_tmp, swarm_adapter="demo")
        session = ConversationalSession(cfg)

        class CmdPilot:
            def __init__(self):
                self.calls = 0

            def complete(self, prompt, system=None):
                self.calls += 1
                if self.calls == 1:
                    return FakeResponse(text=json.dumps({
                        "say": "Running command now",
                        # Python one-liner sleep: portable across /bin/sh and
                        # cmd.exe (POSIX `sleep` and `;` chaining are not).
                        "actions": [{"kind": "run_command", "command":
                            f'"{sys.executable}" -c "import time; time.sleep(0.3); print(\'alive_marker_42\')"'}],
                    }))
                return FakeResponse(text=json.dumps({"say": "Done", "actions": []}))

        session.pilot = CmdPilot()

        cmd_result = None
        for ev in session.send("start"):
            if ev.kind == "action_start" and ev.data.get("kind") == "run_command":
                # Simulate the sibling-stream / disconnect cancel landing between
                # action_start and the runner launch -- exactly the poison window.
                session._cancel.set()
            if ev.kind == "action_result" and "command" in (ev.data.get("types") or []):
                cmd_result = ev.data

        assert cmd_result is not None, "run_command produced no result -- it was killed before launch"
        assert cmd_result.get("exit_code") == 0, f"command was wrongly cancelled: {cmd_result!r}"
        assert "alive_marker_42" in (cmd_result.get("output") or "")
        headline = cmd_result["artifacts"][0]["headline"]
        assert "0" in headline and (
            headline.startswith("exit 0") or headline == "Command exited with 0"
        ), f"unexpected headline: {headline!r}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _python_command(source):
    args = [sys.executable, "-c", source]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


@pytest.mark.parametrize("rerun_exit_code", [0, 1])
@pytest.mark.parametrize("mutation_kind", ["write_file", "edit_file", "hash_edit"])
def test_cached_run_command_reexecutes_after_native_file_mutation(
    tmp_path, monkeypatch, mutation_kind, rerun_exit_code,
):
    monkeypatch.setenv("HARNESS_HASH_EDIT", "1")
    target = tmp_path / "value.txt"
    target.write_text("old\n", encoding="utf-8")
    command = _python_command(
        "from pathlib import Path; import sys; "
        "value=Path('value.txt').read_text().strip(); print(value); "
        f"sys.exit({rerun_exit_code} if value == 'new' else 0)"
    )
    mutation = {
        "write_file": {
            "kind": "write_file", "path": "value.txt", "content": "new\n",
        },
        "edit_file": {
            "kind": "edit_file", "path": "value.txt", "old_str": "old", "new_str": "new",
        },
        "hash_edit": {
            "kind": "hash_edit",
            "path": "value.txt",
            "arguments": {"ops": [{
                "op": "replace",
                "start_line": 1,
                "end_line": 1,
                "anchor": compute_range_hash(["old"], 1, 1),
                "text": "new",
            }]},
        },
    }[mutation_kind]

    class MutationPilot:
        def __init__(self):
            self.responses = [
                {"say": "Read old value", "actions": [{"kind": "run_command", "command": command}]},
                {"say": "Mutate value", "actions": [mutation]},
                {"say": "Read fresh value", "actions": [{"kind": "run_command", "command": command}]},
                {"say": "Repeat unchanged", "actions": [{"kind": "run_command", "command": command}]},
                {"say": "Done", "actions": []},
            ]

        def complete(self, prompt, system=None):
            return FakeResponse(text=json.dumps(self.responses.pop(0)))

    session = ConversationalSession(HarnessConfig(repo=str(tmp_path), swarm_adapter="demo"))
    session.pilot = MutationPilot()
    events = list(session.send("refresh after mutation"))
    command_results = [
        event.data for event in events
        if event.kind == "action_result" and "command" in (event.data.get("types") or [])
    ]

    assert [result["output"].strip() for result in command_results] == ["old", "new"]
    assert [result["exit_code"] for result in command_results] == [0, rerun_exit_code]
    cached_results = [
        event for event in events
        if event.kind == "action_result" and "cached" in (event.data.get("types") or [])
    ]
    assert len(cached_results) == (1 if rerun_exit_code == 0 else 0)
    if rerun_exit_code:
        assert any(
            event.kind == "action_result"
            and "identical run_command call" in event.data.get("error", "")
            for event in events
        )


@pytest.mark.parametrize("copy_exit_code", [0, 1])
def test_failed_run_command_reexecutes_after_write_and_different_command(tmp_path, copy_exit_code):
    check = _python_command(
        "from pathlib import Path; import sys; p=Path('target.txt'); "
        "print(p.read_text().strip() if p.exists() else 'missing'); "
        "sys.exit(0 if p.exists() else 2)"
    )
    copy = _python_command(
        "from shutil import copyfile; import sys; copyfile('source.txt', 'target.txt'); "
        f"sys.exit({copy_exit_code})"
    )

    class RetryPilot:
        def __init__(self):
            self.responses = [
                {"say": "Check", "actions": [{"kind": "run_command", "command": check}]},
                {"say": "Create source", "actions": [{
                    "kind": "write_file", "path": "source.txt", "content": "ready\n",
                }]},
                {"say": "Copy", "actions": [{"kind": "run_command", "command": copy}]},
                {"say": "Check again", "actions": [{"kind": "run_command", "command": check}]},
                {"say": "Done", "actions": []},
            ]

        def complete(self, prompt, system=None):
            return FakeResponse(text=json.dumps(self.responses.pop(0)))

    session = ConversationalSession(HarnessConfig(repo=str(tmp_path), swarm_adapter="demo"))
    session.pilot = RetryPilot()
    events = list(session.send("retry after repairing inputs"))
    command_results = [
        event.data for event in events
        if event.kind == "action_result" and "command" in (event.data.get("types") or [])
    ]

    assert [result["exit_code"] for result in command_results] == [2, copy_exit_code, 0]
    assert command_results[-1]["output"].strip() == "ready"


@pytest.mark.parametrize("publication_fails", [False, True])
def test_approval_blocked_command_does_not_invalidate_prior_command_cache(
    tmp_path, monkeypatch, publication_fails,
):
    first = "first-command"
    blocked = "blocked-command"

    class BlockedPilot:
        def __init__(self):
            self.responses = [
                {"say": "Run", "actions": [{"kind": "run_command", "command": first}]},
                {"say": "Blocked", "actions": [{"kind": "run_command", "command": blocked}]},
                {"say": "Repeat", "actions": [{"kind": "run_command", "command": first}]},
                {"say": "Done", "actions": []},
            ]

        def complete(self, prompt, system=None):
            return FakeResponse(text=json.dumps(self.responses.pop(0)))

    session = ConversationalSession(HarnessConfig(repo=str(tmp_path), swarm_adapter="demo"))
    session.pilot = BlockedPilot()

    def fake_run_command(act):
        if act.command == blocked:
            return False, "blocked", {
                "message": "approval required",
                "command_hash": "blocked-hash",
                "category": "test",
                "reason": "test block",
            }
        return True, "success", {
            "output": "stable output\n",
            "exit_code": 0,
            "status": "ok",
            "cwd": str(tmp_path),
        }

    monkeypatch.setattr(session, "_do_run_command", fake_run_command)
    if publication_fails:
        from harness import command_jobs

        finish = command_jobs.finish_foreground_command_job

        def fail_blocked_publication(session, job_id, ok, status, val):
            if status == "blocked":
                raise RuntimeError("publication failed after approval block")
            return finish(session, job_id, ok, status, val)

        monkeypatch.setattr(command_jobs, "finish_foreground_command_job", fail_blocked_publication)
    events = list(session.send("do not invalidate on approval block"))

    first_results = [
        event.data for event in events
        if event.kind == "action_result" and event.data.get("command") == first
    ]
    cached_results = [
        event.data for event in events
        if event.kind == "action_result" and "cached" in (event.data.get("types") or [])
    ]
    assert len(first_results) == 1
    assert len(cached_results) == 1


@dataclass
class _ReadAct:
    path: str
    kind: str = "read_file"
    start_line: object = None
    limit: object = None


def test_read_file_can_read_spilled_result_outside_repo():
    """Regression: oversized tool output is persisted to
    {state_dir}/pmharness-results/<id>.txt and the model is told to read it back
    with read_file. That dir lives outside the workspace, so read_file must allow
    it -- otherwise the pilot is told to read a file it is then refused (the
    "reads and sandbox problems" deadlock)."""
    with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as state:
        cfg = HarnessConfig(repo=os.path.realpath(repo), swarm_adapter="demo",
                            state_dir=os.path.realpath(state))
        session = ConversationalSession(cfg)

        spill_dir = os.path.join(os.path.realpath(state), "pmharness-results")
        os.makedirs(spill_dir, exist_ok=True)
        spill_file = os.path.join(spill_dir, "web_fetch_abc.txt")
        with open(spill_file, "w") as f:
            f.write("SPILLED CONTENT\nline two\n")

        ok, status, val = session._do_read_file(_ReadAct(path=spill_file))
        assert ok, f"reading spilled result should be allowed, got {status}: {val}"
        assert "SPILLED CONTENT" in val

        # A path outside both the repo and the spill dir is still rejected.
        bad = session._do_read_file(_ReadAct(path="/etc/passwd"))
        assert bad[0] is False and bad[1] == "path_traversal"


def test_read_file_allows_bound_operator_extra_root(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    extra = tmp_path / "Downloads" / "kit"
    extra.mkdir(parents=True)
    target = extra / "note.md"
    target.write_text("VISITOR\n", encoding="utf-8")
    cfg = HarnessConfig(
        repo=os.path.realpath(str(repo)),
        swarm_adapter="demo",
        state_dir=os.path.realpath(str(tmp_path / "state")),
    )
    session = ConversationalSession(cfg)
    session._extra_read_roots = [os.path.realpath(str(extra))]
    ok, status, val = session._do_read_file(_ReadAct(path=str(target)))
    assert ok, f"expected extra-root read, got {status}: {val}"
    assert "VISITOR" in val
    still_bad = session._do_read_file(_ReadAct(path="/etc/passwd"))
    assert still_bad[0] is False and still_bad[1] == "path_traversal"


def test_read_file_on_directory_returns_listing():
    """read_file on a real directory should succeed with a listing, not IsADirectoryError."""
    with tempfile.TemporaryDirectory() as repo:
        sub = os.path.join(repo, "frontend", "src")
        os.makedirs(sub)
        with open(os.path.join(sub, "app.ts"), "w", encoding="utf-8") as f:
            f.write("export {}\n")
        nested = os.path.join(sub, "components")
        os.makedirs(nested)

        cfg = HarnessConfig(
            repo=os.path.realpath(repo),
            swarm_adapter="demo",
            state_dir=tempfile.mkdtemp(),
        )
        session = ConversationalSession(cfg)

        ok, status, val = session._do_read_file(_ReadAct(path="frontend/src"))
        assert ok is True, f"expected success, got {status}: {val}"
        assert status == "success"
        assert "path is a directory" in val
        assert "use list_dir next time" in val
        assert "app.ts" in val
        assert "components/" in val
        assert "IsADirectoryError" not in val
        assert "Path is a directory:" not in val

        # Narrow file reads remain unchanged.
        ok_file, status_file, val_file = session._do_read_file(
            _ReadAct(path="frontend/src/app.ts")
        )
        assert ok_file is True and status_file == "success"
        assert "export" in val_file
        assert "path is a directory" not in val_file


def test_nested_workspace_read_file_allows_git_toplevel_parent():
    """Workspace nested under a git clone can read_file the parent README.

    Writes stay confined to the open workspace; paths outside the git toplevel
    remain path_traversal.
    """
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as state:
        root = os.path.realpath(tmp)
        subprocess.run(
            ["git", "init"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        nested = os.path.join(root, "Ashita", "addons", "kotoba")
        os.makedirs(nested)
        readme = os.path.join(root, "README.md")
        with open(readme, "w", encoding="utf-8") as f:
            f.write("# ffxiAddons parent readme\n")
        sibling = os.path.join(root, "Ashita", "addons", "other", "note.txt")
        os.makedirs(os.path.dirname(sibling))
        with open(sibling, "w", encoding="utf-8") as f:
            f.write("sibling\n")

        cfg = HarnessConfig(
            repo=os.path.realpath(nested),
            swarm_adapter="demo",
            state_dir=os.path.realpath(state),
        )
        session = ConversationalSession(cfg)

        ok, status, val = session._do_read_file(_ReadAct(path=readme))
        assert ok, f"parent README under git toplevel should be readable, got {status}: {val}"
        assert "ffxiAddons parent readme" in val

        ok_sib, status_sib, val_sib = session._do_read_file(_ReadAct(path=sibling))
        assert ok_sib, f"sibling under git toplevel should be readable, got {status_sib}: {val_sib}"
        assert "sibling" in val_sib

        # True escape outside the git clone is still rejected.
        outside = os.path.join(os.path.dirname(root), "escape-outside.txt")
        bad = session._do_read_file(_ReadAct(path=outside))
        assert bad[0] is False and bad[1] == "path_traversal"

        # Writes/edits stay confined to the nested workspace (not the git root).
        assert not is_safe_path(readme, cfg.repo)
        assert is_safe_path(os.path.join(cfg.repo, "local.txt"), cfg.repo)


def _tool_schema(name):
    return next(item["function"] for item in build_tools_schema()
                if item["function"]["name"] == name)


def test_file_tool_schemas_describe_read_and_write_boundaries(monkeypatch):
    monkeypatch.setenv("HARNESS_HASH_EDIT", "1")

    read_schema = _tool_schema("read_file")
    assert "configured read roots" in read_schema["description"]
    assert "workspace-relative" in read_schema["parameters"]["properties"]["path"]["description"]

    for name in ("write_file", "edit_file", "hash_edit"):
        schema = _tool_schema(name)
        description = schema["description"]
        path_description = schema["parameters"]["properties"]["path"]["description"]
        assert "writable workspace" in description
        assert "writable workspace" in path_description

    write_description = _tool_schema("write_file")["description"]
    assert "explicitly authorized external destination" in write_description
    assert "run_command" in write_description
    assert "preserve the requested location" in write_description


def test_send_loop_native_file_tools_share_external_path_denial(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HASH_EDIT", "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = workspace / "existing.txt"
    sentinel.write_bytes(b"WORKSPACE ORIGINAL\n")
    external = tmp_path / "invoice_tool" / "invoice_totals.py"
    external.parent.mkdir()
    external.write_bytes(b"EXTERNAL ORIGINAL\n")

    class ExternalPathPilot:
        name = "external-path-pilot"

        def __init__(self, tool_name, arguments):
            self.calls = 0
            self.tool_name = tool_name
            self.arguments = arguments

        def chat(self, messages, *, tools=None, system=None):
            from pmharness.drivers.openai_compat import DriverResponse

            self.calls += 1
            if self.calls > 1:
                return DriverResponse(
                    text="The native file tools were rejected.",
                    meta={"tool_calls": [], "finish_reason": "stop"},
                )
            return DriverResponse(
                text="",
                meta={
                    "tool_calls": [{
                        "id": f"external_{self.tool_name}",
                        "type": "function",
                        "function": {
                            "name": self.tool_name,
                            "arguments": json.dumps(self.arguments),
                        },
                    }],
                    "finish_reason": "tool_calls",
                },
            )

    expected = (
        f"Outside the file tool workspace: requested path {str(external)!r}; "
        f"workspace {str(workspace)!r}. Request rejected. Native write_file, "
        "edit_file, and hash_edit are limited to this workspace. For an "
        "explicitly authorized external destination, use run_command subject "
        "to its existing permissions and preserve the requested location."
    )
    calls = (
        ("write_file", {"path": str(external), "content": "RELOCATED\n"}),
        (
            "edit_file",
            {
                "path": str(external),
                "old_str": "EXTERNAL ORIGINAL",
                "new_str": "EDITED",
            },
        ),
        (
            "hash_edit",
            {
                "path": str(external),
                "ops": [{"op": "delete", "start_line": 1, "end_line": 1}],
            },
        ),
    )
    for tool_name, arguments in calls:
        session = ConversationalSession(HarnessConfig(
            repo=str(workspace),
            state_dir=str(tmp_path / f"state-{tool_name}"),
            swarm_adapter="demo",
        ))
        session.pilot = ExternalPathPilot(tool_name, arguments)
        events = list(session.send(
            f"Use {tool_name} in the requested temporary directory; leave my current project untouched."
        ))
        results = [event.data for event in events if event.kind == "action_result"]
        assert [result.get("error") for result in results] == [expected]
        tool_messages = [
            message for message in session._history if message.get("role") == "tool"
        ]
        assert len(tool_messages) == 1
        assert expected in tool_messages[0]["content"]
        assert tool_messages[0].get("is_error") is True
        assert "Host guidance: This tool call failed." in tool_messages[0]["content"]

    assert external.read_bytes() == b"EXTERNAL ORIGINAL\n"
    assert sentinel.read_bytes() == b"WORKSPACE ORIGINAL\n"
    assert sorted(
        (path.relative_to(workspace).as_posix(), path.read_bytes())
        for path in workspace.rglob("*") if path.is_file()
    ) == [("existing.txt", b"WORKSPACE ORIGINAL\n")]


@pytest.mark.parametrize(
    ("case", "tool_name", "arguments"),
    [
        (
            "write_result_failure",
            "write_file",
            {"path": "value.txt", "content": "REPLACED\n"},
        ),
        (
            "write_exception",
            "write_file",
            {"path": "value.txt", "content": "REPLACED\n"},
        ),
        (
            "edit_preview_failure",
            "edit_file",
            {"path": "value.txt", "old_str": "MISSING", "new_str": "REPLACED"},
        ),
        (
            "hash_preview_failure",
            "hash_edit",
            {
                "path": "value.txt",
                "ops": [{
                    "op": "replace",
                    "start_line": 1,
                    "end_line": 1,
                    "anchor": "stale-anchor",
                    "text": "REPLACED",
                }],
            },
        ),
    ],
)
def test_send_loop_native_file_failures_keep_error_semantics(
    tmp_path, monkeypatch, case, tool_name, arguments,
):
    monkeypatch.setenv("HARNESS_HASH_EDIT", "1")
    target = tmp_path / "value.txt"
    target.write_bytes(b"ORIGINAL\n")
    tool_call_id = f"failed_{case}"

    class FailedFilePilot:
        name = "failed-file-pilot"

        def __init__(self):
            self.calls = 0

        def chat(self, messages, *, tools=None, system=None):
            from pmharness.drivers.openai_compat import DriverResponse

            self.calls += 1
            if self.calls > 1:
                return DriverResponse(
                    text="The native file tool failed.",
                    meta={"tool_calls": [], "finish_reason": "stop"},
                )
            return DriverResponse(
                text="",
                meta={
                    "tool_calls": [{
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(arguments),
                        },
                    }],
                    "finish_reason": "tool_calls",
                },
            )

    session = ConversationalSession(HarnessConfig(
        repo=str(tmp_path),
        state_dir=str(tmp_path / f"state-{case}"),
        swarm_adapter="demo",
    ))
    session.pilot = FailedFilePilot()
    if case.startswith("write_"):
        real_write = session._do_write_file

        def fail_actual_write(action, *, write=True):
            if not write:
                return real_write(action, write=False)
            if case == "write_exception":
                raise PermissionError("Permission denied")
            return False, "permission_denied", "Permission denied"

        monkeypatch.setattr(session, "_do_write_file", fail_actual_write)

    events = list(session.send(f"Exercise the {tool_name} failure path."))
    results = [event.data for event in events if event.kind == "action_result"]
    assert len(results) == 1
    assert results[0].get("error")

    tool_messages = [
        message for message in session._history if message.get("role") == "tool"
    ]
    assert len(tool_messages) == 1
    tool_message = tool_messages[0]
    assert tool_message["tool_call_id"] == tool_call_id
    assert tool_message["status"] == "error"
    assert tool_message["is_error"] is True
    assert "Host guidance: This tool call failed." in tool_message["content"]

    projected = chat_completions_messages([tool_message])[0]
    projected_content = json.loads(projected["content"])
    assert projected_content["status"] == "error"
    assert projected_content["is_error"] is True
    assert "Host guidance: This tool call failed." in projected_content["output"]
    assert target.read_bytes() == b"ORIGINAL\n"


def _assert_file_tools_reject_path(session, requested, workspace, original):
    actions = (
        PilotAction(kind="read_file", path=requested),
        PilotAction(kind="write_file", path=requested, content="WRITTEN\n"),
        PilotAction(
            kind="edit_file",
            path=requested,
            old_str="ORIGINAL",
            new_str="EDITED",
        ),
        PilotAction(
            kind="hash_edit",
            path=requested,
            arguments={"ops": [{"op": "delete", "start_line": 1, "end_line": 1}]},
        ),
    )
    handlers = (
        session._do_read_file,
        session._do_write_file,
        session._do_edit_file,
        session._do_hash_edit,
    )

    for handler, action in zip(handlers, actions):
        ok, status, message = handler(action)
        assert ok is False
        assert status == "path_traversal"
        assert repr(requested) in message
        assert repr(str(workspace)) in message
        assert "run_command" in message
        assert "existing permissions" in message
        assert original.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_file_tools_reject_outside_paths_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HASH_EDIT", "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cfg = HarnessConfig(
        repo=str(workspace),
        swarm_adapter="demo",
        state_dir=str(tmp_path / "state"),
    )
    session = ConversationalSession(cfg)

    outside = tmp_path / "outside.txt"
    sibling = tmp_path / "workspace-sibling" / "outside.txt"
    sibling.parent.mkdir()
    for target in (outside, sibling):
        target.write_text("ORIGINAL\n", encoding="utf-8")

    for requested, target in (
        (str(outside), outside),
        (str(sibling), sibling),
        ("../outside.txt", outside),
    ):
        _assert_file_tools_reject_path(session, requested, workspace, target)


def test_file_tools_reject_symlink_escape_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HASH_EDIT", "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = outside_dir / "target.txt"
    outside.write_text("ORIGINAL\n", encoding="utf-8")
    link = workspace / "linked"
    try:
        link.symlink_to(outside_dir, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not available")

    cfg = HarnessConfig(
        repo=str(workspace),
        swarm_adapter="demo",
        state_dir=str(tmp_path / "state"),
    )
    session = ConversationalSession(cfg)
    _assert_file_tools_reject_path(session, "linked/target.txt", workspace, outside)


def test_writable_file_tools_still_work_inside_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HASH_EDIT", "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cfg = HarnessConfig(
        repo=str(workspace),
        swarm_adapter="demo",
        state_dir=str(tmp_path / "state"),
    )
    session = ConversationalSession(cfg)

    target = workspace / "inside.txt"
    write = PilotAction(kind="write_file", path="inside.txt", content="one\n")
    assert session._do_write_file(write)[:2] == (True, "success")

    edit = PilotAction(kind="edit_file", path="inside.txt", old_str="one", new_str="two")
    assert session._do_edit_file(edit)[:2] == (True, "success")

    anchor = compute_range_hash(["two"], 1, 1)
    hashed = PilotAction(
        kind="hash_edit",
        path="inside.txt",
        arguments={"ops": [{
            "op": "replace",
            "start_line": 1,
            "end_line": 1,
            "anchor": anchor,
            "text": "three",
        }]},
    )
    assert session._do_hash_edit(hashed)[:2] == (True, "success")
    assert target.read_text(encoding="utf-8") == "three\n"
