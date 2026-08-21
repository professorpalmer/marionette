"""Hermetic Wave 3: visible-schema tool-name repair + invalid-only halt."""
from __future__ import annotations

import copy
import inspect
import json
import re
from pathlib import Path

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.pilot import (
    INVALID_ACTION_KIND,
    INVALID_ONLY_HALT_AFTER,
    InvalidAction,
    PilotAction,
    is_invalid_action,
    is_invalid_only_step,
    next_invalid_only_streak,
    parse_inline_tool_calls,
    parse_tool_calls,
    repair_tool_name,
    tool_names_from_schema,
)
from harness.pilot_tool_recovery import parse_native_tool_turn
from harness.send_loop import SendLoopMixin

_PORTABLE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


SWARM_VISIBLE = ["read_file", "run_command", "run_swarm"]
SWARM_HIDDEN = ["read_file", "run_command"]


def _tc(name, args=None, tc_id="tc1"):
    return {
        "id": tc_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args if args is not None else {}),
        },
    }


def _visible_names_in_error(text: str) -> list[str]:
    if "Closest visible:" in text:
        tail = text.split("Closest visible:", 1)[1]
        return [part.strip(" .") for part in tail.split(",") if part.strip()]
    if "e.g. " in text:
        tail = text.split("e.g. ", 1)[1].rstrip(").")
        return [part.strip() for part in tail.split(",") if part.strip()]
    return []


# --- A / B: repair_tool_name + parse_tool_calls ---------------------------

@pytest.mark.parametrize(
    "raw_name",
    ["RunSwarm", "run-swarm", "functions.run_swarm", "run_swrm", "PuppeteerMaster"],
)
def test_a_visible_run_swarm_repairs_common_aliases(raw_name):
    assert repair_tool_name(raw_name, SWARM_VISIBLE) == "run_swarm"
    payload = {"goal": "map auth", "roles": ["explore"]}
    raw = _tc(raw_name, payload, tc_id="call_swarm")
    actions = parse_tool_calls([raw], allowed_names=SWARM_VISIBLE)
    assert len(actions) == 1
    act = actions[0]
    assert act.kind == "run_swarm"
    assert act.goal == "map auth"
    assert act.roles == ["explore"]
    assert act.tool_call_id == "call_swarm"
    assert raw["function"]["name"] == raw_name
    assert getattr(act, "provider_name", "") == raw_name


def test_b_puppeteermaster_does_not_repair_when_run_swarm_hidden():
    assert repair_tool_name("PuppeteerMaster", SWARM_HIDDEN) is None
    actions = parse_tool_calls(
        [_tc("PuppeteerMaster", {"goal": "x"}, tc_id="tc_hidden")],
        allowed_names=SWARM_HIDDEN,
    )
    assert len(actions) == 1
    assert is_invalid_action(actions[0])
    assert "run_swarm" not in (actions[0].content or "")
    assert actions[0].tool_call_id == "tc_hidden"
    assert actions[0].arguments.get("goal") == "x"


def test_b_aliases_require_visible_run_swarm():
    for alias in ("puppetmaster", "puppeteermaster", "puppetmaster_swarm"):
        assert repair_tool_name(alias, SWARM_VISIBLE) == "run_swarm"
        assert repair_tool_name(alias, SWARM_HIDDEN) is None


# --- C: ambiguous / low-score stay invalid, suggestions bounded -----------

def test_c_ambiguous_and_low_score_stay_invalid_and_bound_suggestions():
    assert repair_tool_name("foo_ba", ["foo_baz", "foo_bat"]) is None
    assert repair_tool_name("zzz", SWARM_HIDDEN) is None
    actions = parse_tool_calls(
        [_tc("xyzzy_nope", {"q": 1}, tc_id="tc_unk")],
        allowed_names=SWARM_HIDDEN,
    )
    assert len(actions) == 1
    assert is_invalid_action(actions[0])
    err = actions[0].content or ""
    assert "is not a visible tool" in err
    suggested = _visible_names_in_error(err)
    assert suggested
    assert len(suggested) <= 3
    assert set(suggested) <= set(SWARM_HIDDEN)
    assert "run_swarm" not in err
    assert "Available tools:" not in err
    assert "run_implement" not in err


# --- D: empty names are missing-name InvalidAction, not skipped -----------

@pytest.mark.parametrize("blank", ["", "   ", "\n", "\t "])
def test_d_empty_name_is_missing_invalid_action(blank):
    actions = parse_tool_calls(
        [_tc(blank, {"path": "a.py"}, tc_id="tc_blank")],
        allowed_names=SWARM_VISIBLE,
    )
    assert len(actions) == 1
    assert is_invalid_action(actions[0])
    err = actions[0].content or ""
    assert "missing" in err.lower()
    assert actions[0].tool_call_id == "tc_blank"
    listed = _visible_names_in_error(err)
    assert listed
    assert len(listed) <= 5
    assert set(listed) <= set(SWARM_VISIBLE)


# --- E: mixed batch keeps valid actions beside invalid carriers -----------

def test_e_mixed_batch_returns_valid_and_invalid():
    calls = [
        _tc("read_file", {"path": "src/a.py"}, tc_id="tc_ok"),
        _tc("", {"path": "x.py"}, tc_id="tc_blank"),
        _tc("not_a_real_tool", {"goal": "x"}, tc_id="tc_bad"),
    ]
    actions = parse_tool_calls(calls, allowed_names=SWARM_VISIBLE)
    assert len(actions) == 3
    assert actions[0].kind == "read_file"
    assert actions[0].path == "src/a.py"
    assert actions[0].tool_call_id == "tc_ok"
    assert is_invalid_action(actions[1])
    assert "missing" in (actions[1].content or "").lower()
    assert is_invalid_action(actions[2])
    assert "is not a visible tool" in (actions[2].content or "")


# --- helpers: schema extract, MCP, streak, inline -------------------------

def test_tool_names_from_schema_reads_function_and_flat_shapes():
    assert tool_names_from_schema([
        {"type": "function", "function": {"name": "read_file"}},
        {"name": "run_swarm"},
        {"type": "function", "function": {"name": "read_file"}},
    ]) == ["read_file", "run_swarm"]
    assert tool_names_from_schema([]) == []
    assert tool_names_from_schema(None) == []


def test_mcp_exact_and_casing_repair_never_invents():
    visible = ["read_file", "mcp_todo__add_item"]
    assert repair_tool_name("mcp_todo__add_item", visible) == "mcp_todo__add_item"
    assert repair_tool_name("MCP_Todo__Add_Item", visible) == "mcp_todo__add_item"
    assert repair_tool_name("mcp_invented__nope", visible) is None
    assert repair_tool_name("mcp_todo__add_itme", visible) is None
    actions = parse_tool_calls(
        [_tc("mcp_weather_get_forecast", {"location": "NY"}, tc_id="tc_mcp")]
    )
    assert actions[0].kind == "call_mcp"
    assert actions[0].tool == "weather.get_forecast"
    assert actions[0].tool_call_id == "tc_mcp"


def test_inline_fallback_uses_same_candidate_set():
    content = '<function=RunSwarm>\n<parameter=goal>\nmap auth\n</parameter>\n</function>'
    repaired = parse_inline_tool_calls(content, allowed_names=SWARM_VISIBLE)
    assert len(repaired) == 1
    assert repaired[0].kind == "run_swarm"
    hidden = parse_inline_tool_calls(content, allowed_names=SWARM_HIDDEN)
    assert hidden == []


def test_g_invalid_only_streak_helper():
    bad = InvalidAction(kind=INVALID_ACTION_KIND, content="nope")
    good = PilotAction(kind="read_file", path="a.py")
    assert is_invalid_only_step([bad, bad]) is True
    assert is_invalid_only_step([bad, good]) is False
    assert is_invalid_only_step([]) is False
    assert next_invalid_only_streak(0, [bad]) == 1
    assert next_invalid_only_streak(1, [bad]) == 2
    assert next_invalid_only_streak(2, [bad]) == INVALID_ONLY_HALT_AFTER
    assert next_invalid_only_streak(2, [good]) == 0
    assert next_invalid_only_streak(2, [bad, good]) == 0
    assert next_invalid_only_streak(2, []) == 0


def test_send_loop_source_passes_schema_names_and_halts():
    src = Path(inspect.getsourcefile(SendLoopMixin)).read_text(encoding="utf-8")
    recovery_src = Path(inspect.getsourcefile(parse_native_tool_turn)).read_text(
        encoding="utf-8"
    )
    assert "parse_native_tool_turn" in src
    assert "allowed_names=" in recovery_src
    assert "tool_names_from_schema" in recovery_src
    assert "_invalid_only_streak" in src
    assert "invalid_tool_halt" in src
    from harness.send_loop_phases import dispatch_pilot_provider_call
    phase_src = Path(
        inspect.getsourcefile(dispatch_pilot_provider_call)
    ).read_text(encoding="utf-8")
    assert "_step_tools_schema" in phase_src


def test_parse_native_tool_turn_allowlist_and_inline_fallback():
    schema = [
        {"type": "function", "function": {"name": name}}
        for name in SWARM_VISIBLE
    ]
    hidden = parse_native_tool_turn(
        "",
        [_tc("run_swrm", {"goal": "map auth"}, tc_id="tc_typo")],
        "thinking",
        schema,
    )
    turn, calls, content = hidden
    assert turn.actions[0].kind == "run_swarm"
    assert turn.thinking == "thinking"
    assert calls[0]["function"]["name"] == "run_swrm"
    assert content == ""

    blocked = parse_native_tool_turn(
        "",
        [_tc("PuppeteerMaster", {"goal": "x"}, tc_id="tc_hidden")],
        "",
        [{"type": "function", "function": {"name": "read_file"}}],
    )
    assert is_invalid_action(blocked[0].actions[0])
    assert "run_swarm" not in (blocked[0].actions[0].content or "")

    inline = (
        "intro "
        "<function=RunSwarm>\n<parameter=goal>\nmap auth\n</parameter>\n"
        "</function>"
    )
    recovered, synth, stripped = parse_native_tool_turn(inline, [], "r", schema)
    assert recovered.actions[0].kind == "run_swarm"
    assert synth and synth[0]["function"]["name"] == "run_swarm"
    assert "function=" not in stripped


# --- F / G: live send loop ------------------------------------------------

class _ScriptedNativePilot:
    name = "scripted-native-pilot"

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen_tool_names = []
        self.calls = 0

    def complete(self, task_prompt, *, system=None):
        from pmharness.drivers.openai_compat import DriverResponse
        return DriverResponse(text="")

    def chat(self, messages, *, tools=None, system=None, **kwargs):
        from pmharness.drivers.openai_compat import DriverResponse
        self.calls += 1
        self.seen_tool_names.append(tool_names_from_schema(tools))
        if self.replies:
            reply = self.replies.pop(0)
        else:
            reply = {"text": "Done."}
        meta = {
            "tool_calls": reply.get("tool_calls") or [],
            "reasoning": "",
            "finish_reason": "tool_calls" if reply.get("tool_calls") else "stop",
        }
        return DriverResponse(
            text=reply.get("text") or "",
            tokens_out=5,
            latency_ms=1.0,
            meta=meta,
        )


def _session(tmp_path, replies, visible=None, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv("HARNESS_MAX_PILOT_STEPS", "8")
    cfg = HarnessConfig(
        driver="stub-oracle-v2",
        state_dir=str(tmp_path / "state"),
        repo=str(tmp_path / "repo"),
    )
    (tmp_path / "repo").mkdir(exist_ok=True)
    (tmp_path / "state").mkdir(exist_ok=True)
    session = ConversationalSession(cfg)
    session.config.no_delegation = True
    session.pilot = _ScriptedNativePilot(replies)
    if visible is not None:
        schema = [
            {"type": "function", "function": {"name": name, "parameters": {
                "type": "object", "properties": {},
            }}}
            for name in visible
        ]
        session._build_visible_tools_schema = lambda: list(schema)
    return session


def test_f_live_send_loop_uses_schema_and_does_not_prime_hidden(tmp_path, monkeypatch):
    session = _session(
        tmp_path,
        [
            {"tool_calls": [_tc("run_swrm", {"goal": "x"}, tc_id="tc_typo")]},
            {"text": "Giving up on that tool."},
        ],
        visible=SWARM_HIDDEN,
        monkeypatch=monkeypatch,
    )
    events = list(session.send("please swarm"))
    assert session.pilot.seen_tool_names
    assert session.pilot.seen_tool_names[0] == SWARM_HIDDEN
    assert getattr(session, "_step_tools_schema", None) is not None
    assert tool_names_from_schema(session._step_tools_schema) == SWARM_HIDDEN
    results = [e for e in events if e.kind == "action_result"]
    assert results
    err = results[0].data.get("error") or ""
    assert "is not a visible tool" in err
    assert "run_swarm" not in err
    assert "run_implement" not in err
    suggested = _visible_names_in_error(err)
    assert len(suggested) <= 3
    assert set(suggested) <= set(SWARM_HIDDEN)


def test_g_three_invalid_only_steps_halt_after_results(tmp_path, monkeypatch):
    session = _session(
        tmp_path,
        [
            {"tool_calls": [_tc("frobnicate_xyz", {}, tc_id="tc_a")]},
            {"tool_calls": [_tc("frobnicate_xyz", {}, tc_id="tc_b")]},
            {"tool_calls": [_tc("frobnicate_xyz", {}, tc_id="tc_c")]},
            {"text": "should not be reached"},
        ],
        visible=SWARM_HIDDEN,
        monkeypatch=monkeypatch,
    )
    events = list(session.send("degenerate"))
    kinds = [e.kind for e in events]
    assert kinds.count("auto_halt") == 1
    assert "error" in kinds
    halt = next(e for e in events if e.kind == "auto_halt")
    assert "invalid tool" in (halt.data.get("reason") or "").lower()
    done = next(e for e in events if e.kind == "assistant_done")
    assert done.data.get("invalid_tool_halt") is True
    assert done.data.get("stop_cause") == "invalid_tool"
    tool_msgs = [m for m in session._history if m.get("role") == "tool"]
    assert len(tool_msgs) == 3
    assert {m.get("tool_call_id") for m in tool_msgs} == {"tc_a", "tc_b", "tc_c"}
    assert session.pilot.calls == 3
    assert "should not be reached" not in " ".join(
        e.data.get("text") or "" for e in events if e.kind == "message"
    )


def test_g_two_invalid_only_steps_do_not_halt(tmp_path, monkeypatch):
    session = _session(
        tmp_path,
        [
            {"tool_calls": [_tc("frobnicate_xyz", {}, tc_id="tc_a")]},
            {"tool_calls": [_tc("frobnicate_xyz", {}, tc_id="tc_b")]},
            {"text": "Recovered in plain text."},
        ],
        visible=SWARM_HIDDEN,
        monkeypatch=monkeypatch,
    )
    events = list(session.send("almost"))
    assert not any(e.kind == "auto_halt" for e in events)
    tool_msgs = [m for m in session._history if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert session.pilot.calls == 3
    assert any(
        "Recovered in plain text." in (e.data.get("text") or "")
        for e in events if e.kind == "message"
    )


def test_g_valid_action_resets_invalid_only_streak(tmp_path, monkeypatch):
    note = tmp_path / "repo" / "note.txt"
    session = _session(
        tmp_path,
        [
            {"tool_calls": [_tc("frobnicate_xyz", {}, tc_id="tc_a")]},
            {"tool_calls": [_tc("frobnicate_xyz", {}, tc_id="tc_b")]},
            {"tool_calls": [_tc("read_file", {"path": "note.txt"}, tc_id="tc_ok")]},
            {"tool_calls": [_tc("frobnicate_xyz", {}, tc_id="tc_c")]},
            {"tool_calls": [_tc("frobnicate_xyz", {}, tc_id="tc_d")]},
            {"text": "Still going."},
        ],
        visible=SWARM_HIDDEN,
        monkeypatch=monkeypatch,
    )
    note.write_text("hi", encoding="utf-8")
    events = list(session.send("keep going"))
    assert not any(e.kind == "auto_halt" for e in events)
    tool_msgs = [m for m in session._history if m.get("role") == "tool"]
    assert len(tool_msgs) == 5
    assert session.pilot.calls == 6


def test_g_new_user_turn_resets_invalid_only_streak(tmp_path, monkeypatch):
    session = _session(
        tmp_path,
        [
            {"text": "first turn done"},
            {"tool_calls": [_tc("frobnicate_xyz", {}, tc_id="tc_c")]},
            {"tool_calls": [_tc("frobnicate_xyz", {}, tc_id="tc_d")]},
            {"text": "second turn done"},
        ],
        visible=SWARM_HIDDEN,
        monkeypatch=monkeypatch,
    )
    events1 = list(session.send("first"))
    assert not any(e.kind == "auto_halt" for e in events1)
    session._invalid_only_streak = 2
    events2 = list(session.send("second"))
    assert not any(e.kind == "auto_halt" for e in events2)
    assert session.pilot.calls == 4


def _assert_adjacent_pairs_match(messages):
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            expected = [tc.get("id") for tc in m["tool_calls"] if tc.get("id")]
            seen = []
            j = i + 1
            while j < n and messages[j].get("role") == "tool":
                tcid = messages[j].get("tool_call_id")
                if tcid:
                    seen.append(tcid)
                j += 1
            assert seen == expected, (expected, seen)
            i = j
            continue
        i += 1


def test_parse_native_tool_turn_fills_empty_ids_without_mutating_provider():
    schema = [
        {"type": "function", "function": {"name": "read_file"}},
    ]
    keep = _tc("read_file", {"path": "k.txt"}, tc_id="keep_me")
    empty = _tc("read_file", {"path": "e.txt"}, tc_id="")
    missing = {
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": json.dumps({"path": "m.txt"}),
        },
    }
    raw = [keep, empty, missing]
    snapshot = copy.deepcopy(raw)

    turn, calls, _ = parse_native_tool_turn("", raw, "", schema)
    assert raw == snapshot
    assert calls is not raw
    assert calls[0]["id"] == "keep_me"
    assert calls[1]["id"] and calls[1]["id"] != calls[2]["id"]
    assert calls[2]["id"]
    assert all(_PORTABLE_ID.match(tc["id"]) for tc in calls)
    assert [act.tool_call_id for act in turn.actions] == [
        calls[0]["id"], calls[1]["id"], calls[2]["id"],
    ]

    session = type("S", (), {"_history": [], "_synthetic_tool_call_seq": 0})()
    _t1, c1, _ = parse_native_tool_turn(
        "", [_tc("read_file", {"path": "a.txt"}, tc_id="")], "", schema,
        session=session,
    )
    _t2, c2, _ = parse_native_tool_turn(
        "",
        [{"type": "function", "function": {
            "name": "read_file",
            "arguments": json.dumps({"path": "b.txt"}),
        }}],
        "",
        schema,
        session=session,
    )
    assert c1[0]["id"] != c2[0]["id"]
    assert all(_PORTABLE_ID.match(i) for i in (c1[0]["id"], c2[0]["id"]))


def test_missing_provider_tool_call_ids_persist_paired_and_portable(
    tmp_path, monkeypatch,
):
    (tmp_path / "repo").mkdir(exist_ok=True)
    (tmp_path / "repo" / "a.txt").write_text("A", encoding="utf-8")
    (tmp_path / "repo" / "b.txt").write_text("B", encoding="utf-8")
    (tmp_path / "repo" / "c.txt").write_text("C", encoding="utf-8")
    (tmp_path / "repo" / "d.txt").write_text("D", encoding="utf-8")

    step1 = [
        _tc("read_file", {"path": "a.txt"}, tc_id=""),
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"path": "b.txt"}),
            },
        },
    ]
    step2 = [
        _tc("read_file", {"path": "c.txt"}, tc_id=""),
        _tc("read_file", {"path": "d.txt"}, tc_id="keep_me"),
    ]
    snapshot1 = copy.deepcopy(step1)
    snapshot2 = copy.deepcopy(step2)

    session = _session(
        tmp_path,
        [
            {"tool_calls": step1},
            {"tool_calls": step2},
            {"text": "Done reading."},
        ],
        visible=["read_file"],
        monkeypatch=monkeypatch,
    )
    list(session.send("read the notes"))

    assert step1 == snapshot1
    assert step2 == snapshot2

    assistants = [
        m for m in session._history
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert len(assistants) == 2
    ids1 = [tc["id"] for tc in assistants[0]["tool_calls"]]
    ids2 = [tc["id"] for tc in assistants[1]["tool_calls"]]
    all_ids = ids1 + ids2
    assert len(all_ids) == 4
    assert len(set(all_ids)) == 4
    assert ids2[1] == "keep_me"
    assert all(_PORTABLE_ID.match(i) for i in all_ids)

    tools = [m for m in session._history if m.get("role") == "tool"]
    assert [m.get("tool_call_id") for m in tools] == all_ids

    session._sanitize_tool_pairs()
    tools_after = [m for m in session._history if m.get("role") == "tool"]
    assert [m.get("tool_call_id") for m in tools_after] == all_ids
    persisted = [
        tc["id"]
        for m in session._history
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    ]
    assert persisted == all_ids

    outbound = session._messages_for_provider()
    out_ids = [
        tc["id"]
        for m in outbound
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    ]
    assert len(out_ids) == 4
    assert len(set(out_ids)) == 4
    assert all(_PORTABLE_ID.match(i) for i in out_ids)
    _assert_adjacent_pairs_match(outbound)
