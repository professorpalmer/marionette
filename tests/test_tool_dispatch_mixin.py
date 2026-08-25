"""Smoke tests for the ToolDispatchMixin extraction.

Guards the mechanical move of `_do_*` per-tool handlers out of
harness.conversation into harness.tool_dispatch. If the class-hierarchy
wiring or the MRO ever regresses, these fail loudly.

Also covers Wave 1 chat-loop resilience: `_do_run_command` must surface
``run_cancellable`` status (ok/cancelled/timeout/truncated/error) instead of
flattening every finished process into success.
"""

from types import SimpleNamespace
from unittest.mock import patch

from harness.conversation import ConversationalSession
from harness.pilot import PilotAction
from harness.tool_dispatch import ToolDispatchMixin


MOVED_METHODS = (
    "_do_read_file",
    "_do_view_image",
    "_do_list_dir",
    "_do_lsp",
    "_do_web_search",
    "_do_web_fetch",
    "_do_read_pdf",
    "_do_search_codegraph",
    "_do_search_files",
    "_do_search_state",
    "_do_search_tools",
    "_do_hash_edit",
    "_do_write_file",
    "_do_edit_file",
    "_do_run_command",
    "_do_run_ipython",
)


def _dispatch_session(tmp_repo="/repo"):
    """Minimal ToolDispatchMixin host with the fields `_do_run_command` reads."""
    return SimpleNamespace(
        config=SimpleNamespace(repo=tmp_repo),
        _auto_mode=False,
        _auto_command_guard=False,
        _cancel=None,
        _do_run_command=ToolDispatchMixin._do_run_command,
    )


def test_session_inherits_mixin():
    assert issubclass(ConversationalSession, ToolDispatchMixin)
    # And the mixin appears in the MRO.
    assert ToolDispatchMixin in ConversationalSession.__mro__


def test_moved_methods_present_on_session():
    for name in MOVED_METHODS:
        assert hasattr(ConversationalSession, name), name
        attr = getattr(ConversationalSession, name)
        assert callable(attr), name


def test_moved_methods_resolve_to_mixin():
    # __qualname__ tells us where the method is actually defined; if any of
    # these regress to "ConversationalSession.*" it means the extraction was
    # accidentally partially reverted or shadowed.
    for name in MOVED_METHODS:
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"ToolDispatchMixin.{name}", (
            name,
            attr.__qualname__,
        )


def test_mixin_defines_no_init():
    # The mixin must not carry state or an __init__ of its own -- otherwise
    # it would interfere with ConversationalSession.__init__ via MRO.
    assert "__init__" not in ToolDispatchMixin.__dict__


def test_reexported_helpers_still_importable_from_conversation():
    # Callers that historically imported these from harness.conversation
    # keep working after the move.
    from harness.conversation import is_safe_path, _strip_ansi, _ANSI_ESCAPE  # noqa: F401
    assert callable(is_safe_path)
    assert callable(_strip_ansi)


def test_do_run_command_ok_includes_status():
    session = _dispatch_session()
    act = PilotAction(kind="run_command", command="echo hi")
    with patch(
        "harness.command_policy.run_cancellable",
        return_value=("hi\n", 0, "ok"),
    ):
        ok, status, val = ToolDispatchMixin._do_run_command(session, act)
    assert ok is True
    assert status == "success"
    assert val["exit_code"] == 0
    assert val["status"] == "ok"
    assert "hi" in val["output"]


def test_do_run_command_cancelled_is_not_success():
    session = _dispatch_session()
    act = PilotAction(kind="run_command", command="sleep 30")
    with patch(
        "harness.command_policy.run_cancellable",
        return_value=("partial\n\n[interrupted by user]", 130, "cancelled"),
    ):
        ok, status, val = ToolDispatchMixin._do_run_command(session, act)
    assert ok is False
    assert status == "cancelled"
    assert val["status"] == "cancelled"
    assert val["exit_code"] == 130
    assert "partial" in val["output"]
    assert "interrupted by user" in val["output"]


def test_do_run_command_reaps_tmp_marionette_worktrees_on_cancel():
    session = _dispatch_session()
    act = PilotAction(
        kind="run_command",
        command="git worktree add /tmp/marionette-origin-main-audit.XXXXXX x",
    )
    with patch(
        "harness.command_policy.run_cancellable",
        return_value=("", 130, "cancelled"),
    ), patch(
        "harness.command_jobs.reap_tmp_marionette_worktrees",
        return_value=[],
    ) as reap:
        ok, status, val = ToolDispatchMixin._do_run_command(session, act)
    assert ok is False
    assert status == "cancelled"
    reap.assert_called()
    texts = [str(call.args[0]) for call in reap.call_args_list]
    assert any("marionette-origin-main-audit" in text for text in texts)


def test_do_run_command_timeout_is_not_success():
    session = _dispatch_session()
    act = PilotAction(kind="run_command", command="sleep 30")
    with patch(
        "harness.command_policy.run_cancellable",
        return_value=("partial\n\n[TimeoutExpired after 1 seconds]", -1, "timeout"),
    ):
        ok, status, val = ToolDispatchMixin._do_run_command(session, act)
    assert ok is False
    assert status == "timeout"
    assert val["status"] == "timeout"
    assert val["exit_code"] == -1
    assert "partial" in val["output"]


def test_do_run_command_error_is_not_success():
    session = _dispatch_session()
    act = PilotAction(kind="run_command", command="nope")
    with patch(
        "harness.command_policy.run_cancellable",
        return_value=("Failed to execute command: boom", -1, "error"),
    ):
        ok, status, val = ToolDispatchMixin._do_run_command(session, act)
    assert ok is False
    assert status == "error"
    assert val["status"] == "error"
    assert val["exit_code"] == -1
    assert "boom" in val["output"]


def test_do_run_command_truncated_keeps_output_and_marks_status():
    session = _dispatch_session()
    act = PilotAction(kind="run_command", command="yes")
    capped = "x" * 100 + "\n\n[output truncated at 2 MiB cap]"
    with patch(
        "harness.command_policy.run_cancellable",
        return_value=(capped, -1, "truncated"),
    ):
        ok, status, val = ToolDispatchMixin._do_run_command(session, act)
    assert ok is True
    assert status == "success"
    assert val["status"] == "truncated"
    assert val["exit_code"] == -1
    assert "truncated" in val["output"]


def test_do_run_command_nonzero_exit_still_ok_status():
    """Nonzero exit from a finished process is status=ok with exit_code retained."""
    session = _dispatch_session()
    act = PilotAction(kind="run_command", command="exit 3")
    with patch(
        "harness.command_policy.run_cancellable",
        return_value=("", 3, "ok"),
    ):
        ok, status, val = ToolDispatchMixin._do_run_command(session, act)
    assert ok is True
    assert status == "success"
    assert val["status"] == "ok"
    assert val["exit_code"] == 3


def test_read_file_default_window_is_hermes_competitive(tmp_path):
    """Oversized files return ~2000 lines by default (not a 100-line head)."""
    from harness.config import HarnessConfig

    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "big.txt").write_text(
        "\n".join(f"This is line {i}" for i in range(1, 2501)),
        encoding="utf-8",
    )
    session = ConversationalSession(
        HarnessConfig(repo=str(workspace), swarm_adapter="demo", state_dir=str(tmp_path / "state"))
    )
    ok, status, val = session._do_read_file(PilotAction(kind="read_file", path="big.txt"))
    assert ok is True and status == "success"
    assert "This is line 2000\n" in val
    assert "This is line 2001\n" not in val
    assert "[file is large (2500 lines);" in val
    assert "truncated after line 2000" in val
    assert "continue with start_line=2001" in val


def test_read_file_truncation_notice_only_when_content_remains(tmp_path):
    """Byte-triggered large files that fit in the window must not claim a false cutoff."""
    from harness.config import HarnessConfig
    from harness.tool_dispatch import _READ_FILE_LARGE_BYTE_TRIGGER

    workspace = tmp_path / "repo"
    workspace.mkdir()
    # Few long lines: over the byte trigger, under the default window.
    body = ("x" * 400 + "\n") * 300  # ~120KB, 300 lines
    assert len(body) > _READ_FILE_LARGE_BYTE_TRIGGER
    (workspace / "wide.txt").write_text(body, encoding="utf-8")
    session = ConversationalSession(
        HarnessConfig(repo=str(workspace), swarm_adapter="demo", state_dir=str(tmp_path / "state"))
    )
    ok, status, val = session._do_read_file(PilotAction(kind="read_file", path="wide.txt"))
    assert ok is True and status == "success"
    assert "[file is large (300 lines);" in val
    assert "truncated after line" not in val
    assert "continue with start_line=" not in val


def test_read_file_path_confinement_fail_closed(tmp_path):
    """Workspace path confinement still rejects escapes outside allowed roots."""
    from harness.config import HarnessConfig

    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "ok.txt").write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "escape.txt"
    outside.write_text("outside\n", encoding="utf-8")
    session = ConversationalSession(
        HarnessConfig(repo=str(workspace), swarm_adapter="demo", state_dir=str(tmp_path / "state"))
    )
    ok, status, val = session._do_read_file(PilotAction(kind="read_file", path="ok.txt"))
    assert ok is True and "inside" in val
    bad = session._do_read_file(PilotAction(kind="read_file", path=str(outside)))
    assert bad[0] is False and bad[1] == "path_traversal"
