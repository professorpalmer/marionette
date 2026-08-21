"""Send-loop consults the terminal classifier; close paths stay truthful."""
from __future__ import annotations

import json
from types import SimpleNamespace

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.send_loop_phases import (
    dispatch_pilot_provider_call,
    stamp_sync_complete_terminal,
)
from pmharness.drivers.base import DriverResponse


def _session(tmp_path, monkeypatch, pilot, *, sid="sess-term"):
    monkeypatch.setattr(
        "harness.send_loop.profile_skips_auto_inject",
        lambda session: (True, True),
    )
    cfg = HarnessConfig(
        driver="stub-oracle-v2",
        state_dir=str(tmp_path),
        repo=str(tmp_path),
    )
    session = ConversationalSession(cfg)
    session.harness_session_id = sid
    session.pilot = pilot
    monkeypatch.setattr(session, "_resolve_append_only", lambda: False)
    monkeypatch.setattr(session, "_get_codegraph_context", lambda msg: "")
    monkeypatch.setattr(session, "_maybe_compact_history", lambda **k: iter(()))
    return session


class _Scripted:
    name = "scripted-terminal"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def chat(self, messages, tools=None, system=None):
        self.calls += 1
        if self.replies:
            return self.replies.pop(0)
        return DriverResponse(
            text='{"say": "fallback", "actions": []}',
            tokens_out=1,
            latency_ms=1.0,
            meta={"finish_reason": "stop"},
        )

    def complete(self, prompt, system=None):
        return self.chat([])


class _CompleteOnly:
    """Legacy/synthetic pilot: synchronous complete() only, no chat."""

    name = "complete-only"

    def __init__(self, replies):
        self.replies = list(replies)

    def complete(self, prompt, system=None):
        if self.replies:
            return self.replies.pop(0)
        return DriverResponse(
            text='{"say": "done", "actions": []}',
            tokens_out=1,
            latency_ms=1.0,
        )


def _done(**meta):
    payload = {"finish_reason": "stop"}
    payload.update(meta)
    return DriverResponse(
        text='{"say": "all done", "actions": []}',
        tokens_out=4,
        latency_ms=1.0,
        meta=payload,
    )


def test_length_after_visible_text_never_emits_assistant_done(tmp_path, monkeypatch):
    executed = {"n": 0}

    def boom(*_a, **_k):
        executed["n"] += 1
        raise AssertionError("execute_turn_actions must not run")

    monkeypatch.setattr("harness.send_loop.execute_turn_actions", boom)
    session = _session(tmp_path, monkeypatch, _Scripted([
        DriverResponse(
            text="partial answer cut off",
            tokens_out=8,
            tokens_in=20,
            latency_ms=1.0,
            meta={
                "finish_reason": "length",
                "stream_terminal": "length",
                "stream_started": True,
            },
        ),
    ]))
    events = list(session.send("write a long essay"))
    kinds = [e.kind for e in events]
    assert "assistant_done" not in kinds
    assert "error" in kinds
    err = next(e for e in events if e.kind == "error")
    assert err.data.get("terminal_cause") == "length"
    assert err.data.get("finish_reason") == "length"
    assert executed["n"] == 0


def test_provider_eof_after_visible_text_never_emits_assistant_done(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch, _Scripted([
        DriverResponse(
            text="visible then drop",
            tokens_out=3,
            latency_ms=1.0,
            meta={
                "finish_reason": "",
                "stream_started": True,
            },
        ),
    ]))
    events = list(session.send("hello"))
    kinds = [e.kind for e in events]
    assert "assistant_done" not in kinds
    assert "error" in kinds
    err = next(e for e in events if e.kind == "error")
    assert err.data.get("terminal_cause") == "provider_eof"


def test_named_incomplete_stream_terminal_is_not_provider_eof(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch, _Scripted([
        DriverResponse(
            text="visible then incomplete",
            tokens_out=3,
            latency_ms=1.0,
            meta={
                "finish_reason": "incomplete",
                "stream_terminal": "incomplete",
                "stream_started": True,
            },
        ),
    ]))
    events = list(session.send("hello"))
    err = next(e for e in events if e.kind == "error")
    assert err.data.get("terminal_cause") == "incomplete"
    assert "assistant_done" not in [e.kind for e in events]


def test_text_only_missing_finish_is_unspecified_error(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch, _Scripted([
        DriverResponse(
            text="sync text with no finish",
            tokens_out=3,
            latency_ms=1.0,
            meta={},
        ),
    ]))
    events = list(session.send("hello"))
    kinds = [e.kind for e in events]
    assert "assistant_done" not in kinds
    assert "error" in kinds


def test_natural_stop_emits_assistant_done_with_cause(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch, _Scripted([
        _done(finish_reason="stop", stream_terminal="stop"),
    ]))
    events = list(session.send("hi"))
    done = next(e for e in events if e.kind == "assistant_done")
    assert done.data.get("stop_cause") == "natural"
    assert done.data.get("finish_reason") == "stop"
    assert not any(e.kind == "error" for e in events)


def test_hard_turn_budget_stop_cause(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_TURN_BUDGET", "1")
    session = _session(tmp_path, monkeypatch, _Scripted([
        DriverResponse(
            text=json.dumps({
                "say": "step 1",
                "actions": [{"kind": "run_command", "command": "echo 1"}],
            }),
            tokens_out=60,
            latency_ms=1.0,
            meta={"finish_reason": "stop"},
        ),
        DriverResponse(
            text=json.dumps({"say": "step 2", "actions": []}),
            tokens_out=60,
            latency_ms=1.0,
            meta={"finish_reason": "stop"},
        ),
    ]))
    events = list(session.send("Work on this +100!"))
    done = next(e for e in events if e.kind == "assistant_done")
    assert done.data.get("turn_budget_exhausted") is True
    assert done.data.get("stop_cause") == "turn_budget"


def test_stagnation_stop_cause(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STAGNATION_STREAK_CAP", "3")
    monkeypatch.setenv("HARNESS_MAX_PILOT_STEPS", "0")
    payload = json.dumps({
        "say": "I will keep checking the same thing.",
        "actions": [{"kind": "list_dir", "path": "."}],
    })
    replies = [
        DriverResponse(
            text=payload, tokens_out=5, latency_ms=1.0,
            meta={"finish_reason": "stop"},
        )
        for _ in range(6)
    ]
    session = _session(tmp_path, monkeypatch, _Scripted(replies))
    monkeypatch.setattr(
        "harness.send_loop_actions.dispatch_readonly_action",
        lambda *a, **k: iter(()),
    )
    monkeypatch.setattr(
        "harness.send_loop_actions.run_parallel_prefetch",
        lambda *a, **k: {},
    )
    events = list(session.send("look around"))
    done = next(e for e in events if e.kind == "assistant_done")
    assert done.data.get("stagnation_halt") is True
    assert done.data.get("stop_cause") == "stagnation"


def test_invalid_tool_stop_cause(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_MAX_PILOT_STEPS", "8")

    def _tc(name, args=None, tc_id="tc1"):
        return {
            "id": tc_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args if args is not None else {}),
            },
        }

    class _Native:
        name = "invalid-tool-native"

        def __init__(self, replies):
            self.replies = list(replies)

        def chat(self, messages, tools=None, system=None, **kwargs):
            reply = self.replies.pop(0) if self.replies else {"text": "Done."}
            return DriverResponse(
                text=reply.get("text") or "",
                tokens_out=5,
                latency_ms=1.0,
                meta={
                    "tool_calls": reply.get("tool_calls") or [],
                    "reasoning": "",
                    "finish_reason": (
                        "tool_calls" if reply.get("tool_calls") else "stop"
                    ),
                },
            )

    cfg = HarnessConfig(
        driver="stub-oracle-v2",
        state_dir=str(tmp_path / "state"),
        repo=str(tmp_path / "repo"),
    )
    (tmp_path / "repo").mkdir(exist_ok=True)
    (tmp_path / "state").mkdir(exist_ok=True)
    session = ConversationalSession(cfg)
    session.config.no_delegation = True
    session.pilot = _Native([
        {"tool_calls": [_tc("frobnicate_xyz", {}, tc_id="tc_a")]},
        {"tool_calls": [_tc("frobnicate_xyz", {}, tc_id="tc_b")]},
        {"tool_calls": [_tc("frobnicate_xyz", {}, tc_id="tc_c")]},
    ])
    hidden = ["read_file", "run_command"]
    session._build_visible_tools_schema = lambda: [
        {"type": "function", "function": {"name": name, "parameters": {
            "type": "object", "properties": {},
        }}}
        for name in hidden
    ]
    events = list(session.send("degenerate"))
    done = next(e for e in events if e.kind == "assistant_done")
    assert done.data.get("invalid_tool_halt") is True
    assert done.data.get("stop_cause") == "invalid_tool"


def test_true_step_cap_is_not_empty_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_MAX_PILOT_STEPS", "1")
    replies = [
        DriverResponse(
            text=json.dumps({
                "say": f"step {i}",
                "actions": [{"kind": "run_command", "command": f"echo {i}"}],
            }),
            tokens_out=4,
            latency_ms=1.0,
            meta={"finish_reason": "stop"},
        )
        for i in range(4)
    ]
    session = _session(tmp_path, monkeypatch, _Scripted(replies))
    events = list(session.send("keep going"))
    messages = [
        e.data.get("text") for e in events
        if e.kind == "message" and e.data.get("role") == "assistant"
    ]
    assert any("investigation step limit" in (m or "") for m in messages)
    assert not any("No productive reply" in (m or "") for m in messages)
    done = next(e for e in events if e.kind == "assistant_done")
    assert done.data.get("stop_cause") == "step_cap"


def test_empty_loop_is_not_step_cap(tmp_path, monkeypatch):
    from harness.pilot import PilotTurn
    monkeypatch.setattr(PilotTurn, "has_actions", True)
    replies = [
        DriverResponse(
            text='{"say": "", "actions": []}',
            tokens_out=1,
            latency_ms=1.0,
            meta={"finish_reason": "stop"},
        )
        for _ in range(5)
    ]
    session = _session(tmp_path, monkeypatch, _Scripted(replies))
    events = list(session.send("go"))
    messages = [
        e.data.get("text") for e in events
        if e.kind == "message" and e.data.get("role") == "assistant"
    ]
    assert any("No productive reply this turn" in (m or "") for m in messages)
    assert not any("investigation step limit" in (m or "") for m in messages)
    done = next(e for e in events if e.kind == "assistant_done")
    assert done.data.get("stop_cause") == "empty_loop"


def test_driver_swap_is_not_step_cap(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch, _Scripted([_done()]))
    session.enqueue_prompt("next job", model="deepseek/deepseek-v4-flash")
    events = list(session.send("finish this first"))
    messages = [
        e.data.get("text") for e in events
        if e.kind == "message" and e.data.get("role") == "assistant"
    ]
    assert any("different driver" in (m or "") for m in messages)
    assert not any("investigation step limit" in (m or "") for m in messages)
    done = next(e for e in events if e.kind == "assistant_done")
    assert done.data.get("stop_cause") == "driver_swap"


def test_incomplete_tool_args_never_reach_execute(tmp_path, monkeypatch):
    called = {"n": 0}

    def tracked(*_a, **_k):
        called["n"] += 1
        if False:
            yield None
        return "return", []

    monkeypatch.setattr("harness.send_loop.execute_turn_actions", tracked)
    session = _session(tmp_path, monkeypatch, _Scripted([
        DriverResponse(
            text="",
            tokens_out=2,
            latency_ms=1.0,
            meta={
                "finish_reason": "tool_calls",
                "stream_terminal": "tool_calls",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":',
                    },
                }],
                "incomplete_tool_calls": [{
                    "id": "call_1",
                    "function": {"name": "read_file", "arguments": '{"path":'},
                }],
            },
        ),
    ]))
    events = list(session.send("read it"))
    assert called["n"] == 0
    assert "assistant_done" not in [e.kind for e in events]
    assert any(e.kind == "error" for e in events)


def test_context_usage_cannot_be_confused_with_output_terminal(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch, _Scripted([
        DriverResponse(
            text='{"say": "ok", "actions": []}',
            tokens_in=14000,
            tokens_out=20,
            latency_ms=1.0,
            meta={
                "finish_reason": "stop",
                "stream_terminal": "stop",
                "raw_usage": {"prompt_tokens": 14000, "completion_tokens": 20},
                "context_used_pct": 14,
            },
        ),
    ]))
    events = list(session.send("status?"))
    done = next(e for e in events if e.kind == "assistant_done")
    assert done.data.get("stop_cause") == "natural"
    assert done.data.get("finish_reason") == "stop"
    assert not any(e.kind == "error" for e in events)


def test_stamp_sync_complete_skips_errors_and_preserves_finish():
    errored = DriverResponse(text="nope", error="boom", meta={})
    stamp_sync_complete_terminal(errored)
    assert errored.meta == {}

    explicit = DriverResponse(text="cut", meta={"finish_reason": "length"})
    stamp_sync_complete_terminal(explicit)
    assert explicit.meta["finish_reason"] == "length"
    assert explicit.meta["wire_mode"] == "sync_complete"

    blank = DriverResponse(text='{"say": "ok", "actions": []}', meta={})
    stamp_sync_complete_terminal(blank)
    assert blank.meta["wire_mode"] == "sync_complete"
    assert blank.meta["finish_reason"] == "completed"


def _exhaust_dispatch(gen):
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def test_dispatch_complete_stamps_sync_complete_marker():
    resp = DriverResponse(text='{"say": "hi", "actions": []}', meta={})

    def complete(prompt, **kwargs):
        return resp

    session = SimpleNamespace(
        pilot=SimpleNamespace(complete=complete),
        config=SimpleNamespace(no_delegation=False),
        harness_session_id=None,
    )
    streamed, out = _exhaust_dispatch(dispatch_pilot_provider_call(
        session,
        plan=False,
        sys_prompt="sys",
        prompt="ping",
        synthesis_nudge_active=False,
    ))
    assert streamed == ""
    assert out is resp
    assert out.meta["wire_mode"] == "sync_complete"
    assert out.meta["finish_reason"] == "completed"


def test_dispatch_chat_does_not_stamp_sync_complete():
    resp = DriverResponse(text="sync text with no finish", meta={})

    def chat(messages, **kwargs):
        return resp

    history = [{"role": "system", "content": "sys"}]
    session = SimpleNamespace(
        pilot=SimpleNamespace(chat=chat, supports_streaming=False),
        config=SimpleNamespace(no_delegation=False),
        harness_session_id=None,
        _history=history,
        _messages_for_provider=lambda: list(history),
        _build_visible_tools_schema=lambda: [],
    )
    streamed, out = _exhaust_dispatch(dispatch_pilot_provider_call(
        session,
        plan=False,
        sys_prompt="sys",
        prompt="ping",
        synthesis_nudge_active=False,
    ))
    assert streamed == ""
    assert out is resp
    assert out.meta.get("wire_mode") != "sync_complete"
    assert out.meta.get("finish_reason") not in ("completed", "natural")


def test_sync_complete_envelope_actions_execute(tmp_path, monkeypatch):
    executed = {"n": 0}

    def track(*_a, **_k):
        executed["n"] += 1
        if False:
            yield None
        return "continue", []

    monkeypatch.setattr("harness.send_loop.execute_turn_actions", track)
    session = _session(tmp_path, monkeypatch, _CompleteOnly([
        DriverResponse(
            text=json.dumps({
                "say": "running a command",
                "actions": [{"kind": "run_command", "command": "echo 1"}],
            }),
            tokens_out=4,
            latency_ms=1.0,
        ),
        DriverResponse(
            text=json.dumps({"say": "all done", "actions": []}),
            tokens_out=2,
            latency_ms=1.0,
        ),
    ]))
    events = list(session.send("go"))
    assert executed["n"] == 1
    done = next(e for e in events if e.kind == "assistant_done")
    assert done.data.get("stop_cause") == "natural"
    assert done.data.get("finish_reason") == "completed"
    assert not any(e.kind == "error" for e in events)


def test_sync_complete_final_envelope_is_natural(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch, _CompleteOnly([
        DriverResponse(
            text='{"say": "all done", "actions": []}',
            tokens_out=2,
            latency_ms=1.0,
        ),
    ]))
    events = list(session.send("hi"))
    done = next(e for e in events if e.kind == "assistant_done")
    assert done.data.get("stop_cause") == "natural"
    assert done.data.get("finish_reason") == "completed"
    assert not any(e.kind == "error" for e in events)


def test_sync_chat_json_envelope_without_finish_executes(tmp_path, monkeypatch):
    executed = {"n": 0}

    def track(*_a, **_k):
        executed["n"] += 1
        if False:
            yield None
        return "continue", []

    monkeypatch.setattr("harness.send_loop.execute_turn_actions", track)
    session = _session(tmp_path, monkeypatch, _Scripted([
        DriverResponse(
            text=json.dumps({
                "say": "running a command",
                "actions": [{"kind": "run_command", "command": "echo 1"}],
            }),
            tokens_out=4,
            latency_ms=1.0,
            meta={},
        ),
        DriverResponse(
            text=json.dumps({"say": "all done", "actions": []}),
            tokens_out=2,
            latency_ms=1.0,
            meta={},
        ),
    ]))
    events = list(session.send("go"))
    assert executed["n"] == 1
    done = next(e for e in events if e.kind == "assistant_done")
    assert done.data.get("stop_cause") == "natural"
    assert not any(e.kind == "error" for e in events)


def test_sync_complete_preserves_explicit_length(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch, _CompleteOnly([
        DriverResponse(
            text="partial cut off",
            tokens_out=2,
            latency_ms=1.0,
            meta={"finish_reason": "length"},
        ),
    ]))
    events = list(session.send("hi"))
    assert "assistant_done" not in [e.kind for e in events]
    assert any(e.kind == "error" for e in events)


class _CursorLike:
    """Production-shaped Cursor CLI chat path: explicit terminal, native prose."""

    name = "cursor-cli:auto"
    requires_explicit_terminal = True

    def __init__(self, replies):
        self.replies = list(replies)

    def chat(self, messages, tools=None, system=None):
        if self.replies:
            return self.replies.pop(0)
        return DriverResponse(text="", error="empty script")


def test_cursor_cli_stamped_success_emits_assistant_done(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch, _CursorLike([
        DriverResponse(
            text="The handler should land here.",
            tokens_out=8,
            latency_ms=12.0,
            meta={
                "finish_reason": "completed",
                "stream_terminal": "stop",
                "stream_started": True,
                "api_mode": "cursor_cli",
                "wire_mode": "cursor_cli_stream",
                "last_provider_event": "result",
                "cursor_cli": True,
                "tool_calls": [],
            },
        ),
    ]))
    events = list(session.send("summarize"))
    done = next(e for e in events if e.kind == "assistant_done")
    assert done.data.get("stop_cause") == "natural"
    assert done.data.get("finish_reason") == "completed"
    assert not any(e.kind == "error" for e in events)
    assert session._last_provider_terminal.cause == "natural"


class _CursorAcpLike:
    """Production-shaped Cursor ACP chat path: explicit terminal, native prose."""

    name = "cursor-cli:auto"
    requires_explicit_terminal = True

    def __init__(self, replies):
        self.replies = list(replies)

    def chat(self, messages, tools=None, system=None):
        if self.replies:
            return self.replies.pop(0)
        return DriverResponse(text="", error="empty script")


def test_cursor_acp_stamped_success_emits_assistant_done_and_records_wire(
    tmp_path, monkeypatch,
):
    from harness.stream_performance_store import StreamPerformanceReceiptStore

    session = _session(tmp_path, monkeypatch, _CursorAcpLike([
        DriverResponse(
            text="The handler should land here.",
            tokens_out=8,
            latency_ms=12.0,
            meta={
                "finish_reason": "completed",
                "stream_terminal": "stop",
                "stream_started": True,
                "api_mode": "cursor_acp",
                "wire_mode": "cursor_acp",
                "last_provider_event": "session/prompt",
                "stop_reason": "end_turn",
                "cursor_acp": True,
                "tool_calls": [],
            },
        ),
    ]))
    events = list(session.send("summarize"))
    done = next(e for e in events if e.kind == "assistant_done")
    assert done.data.get("stop_cause") == "natural"
    assert done.data.get("finish_reason") == "completed"
    assert not any(e.kind == "error" for e in events)
    assert session._last_provider_terminal.cause == "natural"
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-term")
    assert rows
    assert rows[0]["wire_mode"] == "cursor_acp"
    assert rows[0]["api_mode"] == "cursor_acp"
    assert rows[0]["stream_started"] is True
    assert rows[0]["terminal_cause"] == "natural"


def test_cursor_acp_incomplete_and_error_do_not_emit_assistant_done(
    tmp_path, monkeypatch,
):
    executed = {"n": 0}

    def boom(*_a, **_k):
        executed["n"] += 1
        raise AssertionError("execute_turn_actions must not run")

    monkeypatch.setattr("harness.send_loop.execute_turn_actions", boom)
    length = _session(tmp_path, monkeypatch, _CursorAcpLike([
        DriverResponse(
            text="partial ACP cut off",
            error="ACP prompt finished with stopReason=max_tokens",
            tokens_out=8,
            latency_ms=12.0,
            meta={
                "finish_reason": "length",
                "stream_terminal": "length",
                "stream_started": True,
                "api_mode": "cursor_acp",
                "wire_mode": "cursor_acp",
                "last_provider_event": "session/prompt",
                "stop_reason": "max_tokens",
                "cursor_acp": True,
                "tool_calls": [],
            },
        ),
    ]), sid="sess-acp-len")
    length_events = list(length.send("write a long essay"))
    assert "assistant_done" not in [e.kind for e in length_events]
    length_err = next(e for e in length_events if e.kind == "error")
    assert length_err.data.get("terminal_cause") == "length"
    assert executed["n"] == 0

    failed = _session(tmp_path, monkeypatch, _CursorAcpLike([
        DriverResponse(
            text="agent died mid-turn",
            error="ACP prompt finished with stopReason=error",
            tokens_out=3,
            latency_ms=8.0,
            meta={
                "finish_reason": "failed",
                "stream_terminal": "error",
                "stream_started": True,
                "api_mode": "cursor_acp",
                "wire_mode": "cursor_acp",
                "last_provider_event": "session/prompt",
                "stop_reason": "error",
                "cursor_acp": True,
                "tool_calls": [],
            },
        ),
    ]), sid="sess-acp-err")
    fail_events = list(failed.send("continue"))
    assert "assistant_done" not in [e.kind for e in fail_events]
    fail_err = next(e for e in fail_events if e.kind == "error")
    assert fail_err.data.get("terminal_cause") == "transport_error"
    assert executed["n"] == 0


def test_requires_explicit_terminal_rejects_implicit_envelope_natural(tmp_path, monkeypatch):
    class _NetworkPilot:
        name = "openai-compat:test"
        requires_explicit_terminal = True

        def chat(self, messages, tools=None, system=None):
            return DriverResponse(
                text='{"say": "implicit close", "actions": []}',
                tokens_out=3,
                latency_ms=1.0,
                meta={},
            )

    session = _session(tmp_path, monkeypatch, _NetworkPilot())
    events = list(session.send("hi"))
    kinds = [e.kind for e in events]
    assert "assistant_done" not in kinds
    err = next(e for e in events if e.kind == "error")
    assert err.data.get("terminal_cause") == "unspecified"


def test_named_model_error_carries_terminal_cause_not_generic(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch, _Scripted([
        DriverResponse(
            text="partial answer cut off",
            error="OpenAI chat finished with finish_reason=length",
            tokens_out=8,
            latency_ms=1.0,
            meta={
                "finish_reason": "length",
                "stream_terminal": "length",
                "stream_started": True,
            },
        ),
    ]))
    events = list(session.send("write a long essay"))
    err = next(e for e in events if e.kind == "error")
    assert err.data.get("terminal_cause") == "length"
    assert err.data.get("finish_reason") == "length"
    assert "connection" not in str(err.data.get("error") or "").lower()
    assert "assistant_done" not in [e.kind for e in events]


def test_production_drivers_use_explicit_terminal_flag_not_class_names():
    from pmharness.drivers.codex_responses import CodexResponsesDriver
    from pmharness.drivers.cursor_acp import CursorAcpDriver
    from pmharness.drivers.cursor_cli import CursorCliDriver
    from pmharness.drivers.openai_compat import OpenAICompatDriver

    assert OpenAICompatDriver.requires_explicit_terminal is True
    assert CodexResponsesDriver.requires_explicit_terminal is True
    assert CursorCliDriver.requires_explicit_terminal is True
    assert CursorAcpDriver.requires_explicit_terminal is True
    assert getattr(_Scripted([]), "requires_explicit_terminal", False) is not True
    assert getattr(_CompleteOnly([]), "requires_explicit_terminal", False) is not True
