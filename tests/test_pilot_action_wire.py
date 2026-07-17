"""PR1: ActionKind + from_wire field parity (envelope vs native tool calls)."""
from typing import get_args

from harness.pilot import (
    ActionKind,
    INVALID_ACTION_KIND,
    InvalidAction,
    PilotAction,
    VALID_ACTION_KINDS,
    _coerce_actions,
    _tool_name_to_action,
    from_wire,
    parse_tool_calls,
)


def _assert_field_parity(envelope_act: PilotAction, native_act: PilotAction, fields):
    for name in fields:
        assert getattr(envelope_act, name) == getattr(native_act, name), name


def test_action_kind_literal_matches_valid_set():
    assert frozenset(get_args(ActionKind)) == VALID_ACTION_KINDS
    assert INVALID_ACTION_KIND not in VALID_ACTION_KINDS


def test_invalid_action_skips_kind_membership():
    act = InvalidAction(
        kind=INVALID_ACTION_KIND,
        tool="write_file",
        content="TRUNCATED",
        tool_call_id="tc_x",
    ).validate()
    assert isinstance(act, PilotAction)
    assert act.kind == INVALID_ACTION_KIND
    assert act.content == "TRUNCATED"


def test_envelope_native_parity_edit_file():
    payload = {
        "path": "src/a.py",
        "old_str": "foo",
        "new_str": "bar",
        "tool_call_id": "tc_edit",
    }
    env = _coerce_actions([{"kind": "edit_file", **payload}])[0]
    nat = _tool_name_to_action("edit_file", payload, tool_call_id="tc_edit")
    _assert_field_parity(env, nat, ("kind", "path", "old_str", "new_str", "tool_call_id"))
    assert env.old_str == "foo" and env.new_str == "bar"


def test_envelope_native_parity_edit_file_aliases():
    env = from_wire(
        "edit_file",
        {"path": "f.py", "old_string": "a", "new_string": "b"},
    )
    nat = _tool_name_to_action(
        "edit_file", {"file_path": "f.py", "old_string": "a", "new_string": "b"}
    )
    assert env.path == nat.path == "f.py"
    assert env.old_str == nat.old_str == "a"
    assert env.new_str == nat.new_str == "b"


def test_envelope_native_parity_memory():
    payload = {
        "action": "add",
        "content": "prefer dark mode",
        "category": "preference",
        "entry_id": "",
    }
    env = _coerce_actions([{"kind": "memory", **payload}])[0]
    nat = _tool_name_to_action("memory", payload)
    _assert_field_parity(
        env,
        nat,
        ("kind", "memory_action", "memory_content", "memory_category", "memory_id"),
    )
    assert env.memory_action == "add"
    assert env.memory_content == "prefer dark mode"


def test_envelope_native_parity_run_implement_repo():
    payload = {"goal": "add tests", "repo": "/tmp/other/repo", "adapter": "agentic"}
    env = _coerce_actions([{"kind": "run_implement", **payload}])[0]
    nat = _tool_name_to_action("run_implement", payload, tool_call_id="tc_ri")
    assert env.repo == nat.repo == "/tmp/other/repo"
    assert env.goal == nat.goal == "add tests"
    assert env.adapter == nat.adapter == "agentic"
    assert nat.tool_call_id == "tc_ri"


def test_envelope_native_parity_read_file_range():
    payload = {"path": "big.py", "start_line": 10, "limit": 40}
    env = _coerce_actions([{"kind": "read_file", **payload}])[0]
    nat = _tool_name_to_action("read_file", payload)
    _assert_field_parity(env, nat, ("path", "start_line", "limit"))
    assert env.start_line == 10 and env.limit == 40


def test_envelope_native_parity_browser_type():
    payload = {"ref": "@e3", "text": "hello"}
    env = _coerce_actions([{"kind": "browser_type", **payload}])[0]
    nat = _tool_name_to_action("browser_type", payload)
    _assert_field_parity(env, nat, ("ref", "text"))
    assert env.ref == "@e3" and env.text == "hello"


def test_from_wire_target_dir_alias_for_repo():
    act = from_wire(
        "run_parallel",
        {"goals": ["a", "b"], "target_dir": "/tmp/target"},
    )
    assert act.repo == "/tmp/target"
    assert act.goals == ["a", "b"]


def test_parse_tool_calls_emits_invalid_action_type():
    actions = parse_tool_calls([{
        "id": "tc_bad",
        "type": "function",
        "function": {"name": "write_file", "arguments": '{"path": "x.py", "content": "hi'},
    }])
    assert len(actions) == 1
    assert isinstance(actions[0], InvalidAction)
    assert actions[0].kind == INVALID_ACTION_KIND


def test_envelope_edit_file_no_longer_drops_old_str():
    """Regression: pre-PR1 _coerce_actions dropped old_str/new_str so validate failed."""
    acts = _coerce_actions([{
        "kind": "edit_file",
        "path": "x.py",
        "old_str": "before",
        "new_str": "after",
    }])
    assert acts[0].old_str == "before"
    assert acts[0].new_str == "after"
