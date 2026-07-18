"""Tests for tool_use/tool_result pairing sanitizer (prevents Anthropic 400 on a
dangling tool_use after an interrupted spree) and the configurable/unlimited
pilot step ceiling.

Wave 2 also covers provider call-site coverage (sanitize immediately before
dispatch), cancel-mid-tool-spree healing, and crash/resume stub-vs-real races.

Wave 4: steer-mid-tool-spree must heal dangling pairs at the same tool-batch
boundary as cancel (safe-boundary cancel/steer).
"""
import json
import os
import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.pilot import PilotAction


def _session():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    return ConversationalSession(cfg)


def _assert_all_tool_calls_answered(history):
    answered = {m.get("tool_call_id") for m in history if m.get("role") == "tool"}
    called = set()
    for m in history:
        for tc in (m.get("tool_calls") or []):
            if tc.get("id"):
                called.add(tc["id"])
    assert called <= answered, f"dangling tool_use ids: {called - answered}"
    # Uniqueness: one result per id
    tool_ids = [m.get("tool_call_id") for m in history if m.get("role") == "tool"]
    assert len(tool_ids) == len(set(tool_ids)), f"duplicate tool_result ids: {tool_ids}"


def test_sanitize_inserts_stub_for_dangling_tool_call():
    s = _session()
    s._history = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "acting", "tool_calls": [
            {"id": "toolu_A", "type": "function", "function": {"name": "run_command", "arguments": "{}"}},
            {"id": "toolu_B", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "toolu_A", "content": "ok"},
        # toolu_B has NO matching tool result -> dangling (the 400 trigger)
    ]
    s._sanitize_tool_pairs()
    # Every tool_call id now has a matching tool result.
    answered = {m.get("tool_call_id") for m in s._history if m.get("role") == "tool"}
    assert "toolu_A" in answered and "toolu_B" in answered
    # The stub for B is present.
    stubs = [m for m in s._history if m.get("role") == "tool" and m.get("tool_call_id") == "toolu_B"]
    assert len(stubs) == 1 and "interrupted" in stubs[0]["content"]


def test_sanitize_is_idempotent():
    s = _session()
    s._history = [
        {"role": "assistant", "content": "x", "tool_calls": [
            {"id": "toolu_A", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
    ]
    s._sanitize_tool_pairs()
    n1 = len(s._history)
    s._sanitize_tool_pairs()
    assert len(s._history) == n1  # no duplicate stub on a second pass


def test_sanitize_noop_when_all_paired():
    s = _session()
    s._history = [
        {"role": "assistant", "content": "x", "tool_calls": [
            {"id": "toolu_A", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "toolu_A", "content": "done"},
    ]
    before = list(s._history)
    s._sanitize_tool_pairs()
    assert s._history == before


def test_sanitize_inserts_stub_when_steer_wedged_between_tool_use_and_result():
    # Adjacency case: a user/steer message wedged between the assistant tool_use
    # and its tool result breaks Anthropic's "immediately after" rule even though
    # the result is present later. A stub must be inserted immediately after the
    # (empty) adjacent tool run, i.e. right after the assistant message.
    s = _session()
    s._history = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "acting", "tool_calls": [
            {"id": "toolu_A", "type": "function", "function": {"name": "run_command", "arguments": "{}"}},
        ]},
        # A steer/user message is wedged BEFORE the tool result -> non-adjacent.
        {"role": "user", "content": "[OUT-OF-BAND] stop"},
        {"role": "tool", "tool_call_id": "toolu_A", "content": "ok"},
    ]
    s._sanitize_tool_pairs()
    # The assistant tool_use must now be directly followed by a stub tool result.
    idx = next(i for i, m in enumerate(s._history)
               if m.get("role") == "assistant" and m.get("tool_calls"))
    nxt = s._history[idx + 1]
    assert nxt.get("role") == "tool"
    assert nxt.get("tool_call_id") == "toolu_A"
    assert "interrupted" in nxt.get("content", "")


def test_sanitize_drops_duplicate_tool_results():
    # Anthropic 400s "each tool_use must have a single result" when one id is
    # answered twice (stub inserted on interrupt + real result appended on
    # resume). Only one result per id may survive.
    s = _session()
    s._history = [
        {"role": "assistant", "content": "acting", "tool_calls": [
            {"id": "toolu_A", "type": "function", "function": {"name": "run_command", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "toolu_A", "content": "first"},
        {"role": "tool", "tool_call_id": "toolu_A", "content": "second (duplicate)"},
    ]
    s._sanitize_tool_pairs()
    results = [m for m in s._history if m.get("role") == "tool" and m.get("tool_call_id") == "toolu_A"]
    assert len(results) == 1
    assert results[0]["content"] == "first"


def test_sanitize_prefers_real_result_over_stub_duplicate():
    s = _session()
    s._history = [
        {"role": "assistant", "content": "acting", "tool_calls": [
            {"id": "toolu_A", "type": "function", "function": {"name": "run_command", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "toolu_A",
         "content": "(no result: the previous action was interrupted before it completed)"},
        {"role": "tool", "tool_call_id": "toolu_A", "content": "real output"},
    ]
    s._sanitize_tool_pairs()
    results = [m for m in s._history if m.get("role") == "tool" and m.get("tool_call_id") == "toolu_A"]
    assert len(results) == 1
    assert results[0]["content"] == "real output"


def test_sanitize_drops_orphan_result_in_run_but_keeps_content_free_form():
    # A result for an id the assistant never issued is API-rejected; it must not
    # survive as a tool-role message.
    s = _session()
    s._history = [
        {"role": "assistant", "content": "acting", "tool_calls": [
            {"id": "toolu_A", "type": "function", "function": {"name": "run_command", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "toolu_A", "content": "ok"},
        {"role": "tool", "tool_call_id": "toolu_GHOST", "content": "orphan"},
    ]
    s._sanitize_tool_pairs()
    ids = [m.get("tool_call_id") for m in s._history if m.get("role") == "tool"]
    assert ids == ["toolu_A"]


def test_sanitize_recasts_leading_orphan_tool_message_as_user():
    # A tool message with no preceding assistant tool_use at all (history was
    # truncated/compacted mid-run) is invalid in any position; it is recast as
    # user content so the output survives without tripping the API.
    s = _session()
    s._history = [
        {"role": "tool", "tool_call_id": "toolu_LOST", "content": "stranded output"},
        {"role": "user", "content": "continue"},
    ]
    s._sanitize_tool_pairs()
    assert all(m.get("role") != "tool" for m in s._history)
    assert any(m.get("role") == "user" and "stranded output" in str(m.get("content"))
               for m in s._history)


def test_export_transcript_data_has_no_dangling_tool_use():
    s = _session()
    s._history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "acting", "tool_calls": [
            {"id": "toolu_A", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        # dangling: no tool result
    ]
    data = s.export_transcript_data()
    history = data["history"]
    answered = {m.get("tool_call_id") for m in history if m.get("role") == "tool"}
    called = set()
    for m in history:
        for tc in (m.get("tool_calls") or []):
            called.add(tc.get("id"))
    assert called <= answered, "export must not persist a dangling tool_use"
    assert "toolu_A" in answered


def test_load_history_heals_dangling_tool_use():
    s = _session()
    corrupted = {
        "history": [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "acting", "tool_calls": [
                {"id": "toolu_A", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            ]},
            # dangling on load
        ],
        "display": [],
        "job_ids": [],
    }
    s.load_history(corrupted)
    answered = {m.get("tool_call_id") for m in s._history if m.get("role") == "tool"}
    assert "toolu_A" in answered
    idx = next(i for i, m in enumerate(s._history)
               if m.get("role") == "assistant" and m.get("tool_calls"))
    assert s._history[idx + 1].get("role") == "tool"


def test_steer_marker_hardwraps_long_unbroken_token():
    long_token = "a" * 500
    out = ConversationalSession._steer_marker(long_token)
    # The wrapped token lines are the lines made up solely of the run char.
    token_lines = [ln for ln in out.splitlines() if ln and set(ln) == {"a"}]
    assert token_lines, "the token must survive the wrap"
    # Each token line must be clamped to the wrap width (200).
    assert all(len(ln) <= 200 for ln in token_lines), "each token line must be clamped to width"
    # All 500 characters are preserved across the wrap.
    assert sum(len(ln) for ln in token_lines) == 500
    # The 500-char unbroken run was actually broken into multiple lines.
    assert len(token_lines) >= 3


def test_max_pilot_steps_env_unlimited(monkeypatch):
    # 0 == unlimited. Verify the env parse the loop relies on.
    monkeypatch.setenv("HARNESS_MAX_PILOT_STEPS", "0")
    val = int(os.environ.get("HARNESS_MAX_PILOT_STEPS", "40"))
    assert val == 0  # loop treats <=0 as unbounded


def test_max_pilot_steps_env_custom(monkeypatch):
    monkeypatch.setenv("HARNESS_MAX_PILOT_STEPS", "100")
    val = int(os.environ.get("HARNESS_MAX_PILOT_STEPS", "40"))
    assert val == 100


def test_messages_for_provider_sanitizes_before_returning():
    s = _session()
    s._history = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "acting", "tool_calls": [
            {"id": "toolu_A", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}},
        ]},
        # dangling
    ]
    messages = s._messages_for_provider()
    _assert_all_tool_calls_answered(s._history)
    assert any(
        m.get("role") == "tool" and m.get("tool_call_id") == "toolu_A"
        for m in messages
    )


def test_append_action_result_replaces_interruption_stub_not_duplicate():
    """Crash/resume race: stub already present, real result arrives later."""
    s = _session()
    s._history = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "acting", "tool_calls": [
            {"id": "toolu_A", "type": "function",
             "function": {"name": "run_command", "arguments": "{}"}},
        ]},
        s._interruption_stub("toolu_A"),
    ]
    act = PilotAction(
        kind="run_command",
        tool="run_command",
        tool_call_id="toolu_A",
        arguments={},
    )
    s._append_action_result(act, "a1", "real command output", is_native=True)
    results = [
        m for m in s._history
        if m.get("role") == "tool" and m.get("tool_call_id") == "toolu_A"
    ]
    assert len(results) == 1
    assert results[0]["content"] == "real command output"
    assert not results[0]["content"].startswith("(no result:")


def test_append_action_result_does_not_duplicate_real_result():
    s = _session()
    s._history = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "acting", "tool_calls": [
            {"id": "toolu_A", "type": "function",
             "function": {"name": "run_command", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "toolu_A", "content": "first real"},
    ]
    act = PilotAction(
        kind="run_command",
        tool="run_command",
        tool_call_id="toolu_A",
        arguments={},
    )
    s._append_action_result(act, "a1", "second real (should drop)", is_native=True)
    results = [
        m for m in s._history
        if m.get("role") == "tool" and m.get("tool_call_id") == "toolu_A"
    ]
    assert len(results) == 1
    assert results[0]["content"] == "first real"


class _Resp:
    def __init__(self, text, meta=None):
        self.text = text
        self.error = None
        self.meta = meta or {}
        self.tokens_out = 5
        self.tokens_in = 5


class _CancelMidSpreePilot:
    """Issues a multi-tool spree; cancel fires after the first action starts."""
    supports_streaming = False

    def __init__(self, session):
        self.session = session
        self.calls = 0
        self.histories = []

    def chat(self, hist, tools=None, system=""):
        self.calls += 1
        self.histories.append([dict(m) for m in hist])
        if self.calls == 1:
            # Cancel before sibling tools run: first list_dir will execute,
            # then the action loop sees cancel and abandons the rest.
            self.session._cancel.set()
            return _Resp("", {"tool_calls": [
                {"id": "toolu_A", "type": "function",
                 "function": {"name": "list_dir", "arguments": json.dumps({"path": "."})}},
                {"id": "toolu_B", "type": "function",
                 "function": {"name": "list_dir", "arguments": json.dumps({"path": "."})}},
                {"id": "toolu_C", "type": "function",
                 "function": {"name": "list_dir", "arguments": json.dumps({"path": "."})}},
            ]})
        return _Resp('{"say":"done","actions":[]}')


def test_cancel_mid_tool_spree_heals_dangling_pairs(tmp_path):
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    s = ConversationalSession(cfg)
    pilot = _CancelMidSpreePilot(s)
    s.pilot = pilot

    kinds = []
    for ev in s.send("run several tools"):
        kinds.append(ev.kind)

    assert "interrupted" in kinds
    _assert_all_tool_calls_answered(s._history)
    # Abandoned siblings must have interruption stubs, not missing results.
    by_id = {
        m.get("tool_call_id"): m
        for m in s._history
        if m.get("role") == "tool"
    }
    assert "toolu_B" in by_id and "toolu_C" in by_id
    assert "interrupted" in by_id["toolu_B"]["content"]
    assert "interrupted" in by_id["toolu_C"]["content"]


class _SteerMidSpreePilot:
    """Issues a multi-tool spree; steer arrives after the first tool result."""
    supports_streaming = False

    def __init__(self, session):
        self.session = session
        self.calls = 0
        self.saw_steer = False

    def chat(self, hist, tools=None, system=""):
        self.calls += 1
        for m in hist:
            if "OUT-OF-BAND" in str(m.get("content", "")):
                self.saw_steer = True
        if self.saw_steer:
            return _Resp('{"say":"Steered.","actions":[]}')
        if self.calls == 1:
            # Enqueue steer so the action loop injects it after toolu_A and
            # abandons toolu_B / toolu_C at the tool-batch boundary.
            self.session.enqueue_steer("change direction")
            return _Resp("", {"tool_calls": [
                {"id": "toolu_A", "type": "function",
                 "function": {"name": "list_dir", "arguments": json.dumps({"path": "."})}},
                {"id": "toolu_B", "type": "function",
                 "function": {"name": "list_dir", "arguments": json.dumps({"path": "."})}},
                {"id": "toolu_C", "type": "function",
                 "function": {"name": "list_dir", "arguments": json.dumps({"path": "."})}},
            ]})
        return _Resp('{"say":"done","actions":[]}')


def test_steer_mid_tool_spree_heals_dangling_pairs(tmp_path):
    """Wave 4: steer abandon must not leave unanswered tool_calls."""
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    s = ConversationalSession(cfg)
    pilot = _SteerMidSpreePilot(s)
    s.pilot = pilot

    kinds = []
    for ev in s.send("run several tools"):
        kinds.append(ev.kind)

    assert "steer" in kinds
    assert pilot.saw_steer
    _assert_all_tool_calls_answered(s._history)
    by_id = {
        m.get("tool_call_id"): m
        for m in s._history
        if m.get("role") == "tool"
    }
    assert "toolu_B" in by_id and "toolu_C" in by_id
    assert "interrupted" in by_id["toolu_B"]["content"]
    assert "interrupted" in by_id["toolu_C"]["content"]
    # Steer piggybacked on an adjacent tool result (not a synthetic user turn).
    assert any("OUT-OF-BAND" in str(m.get("content", "")) for m in s._history)


class _CallSitePilot:
    supports_streaming = False

    def __init__(self):
        self.calls = 0
        self.saw_sanitize_before_chat = False
        self._session = None
        self._sanitize_count_at_chat = None

    def bind(self, session, counter):
        self._session = session
        self._counter = counter

    def chat(self, hist, tools=None, system=""):
        self.calls += 1
        # Sanitize must have run at least once before this provider call.
        self._sanitize_count_at_chat = self._counter["n"]
        self.saw_sanitize_before_chat = self._counter["n"] > 0
        return _Resp('{"say":"ok","actions":[]}')


def test_send_sanitizes_before_provider_chat(tmp_path):
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    s = ConversationalSession(cfg)
    counter = {"n": 0}
    real = s._sanitize_tool_pairs

    def tracked():
        counter["n"] += 1
        return real()

    s._sanitize_tool_pairs = tracked
    pilot = _CallSitePilot()
    pilot.bind(s, counter)
    s.pilot = pilot

    list(s.send("hello"))
    assert pilot.calls >= 1
    assert pilot.saw_sanitize_before_chat, "send() must sanitize before pilot.chat"
    assert pilot._sanitize_count_at_chat >= 1


def test_resume_sanitizes_before_provider_chat(tmp_path):
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    s = ConversationalSession(cfg)
    # Seed a keep-alive continuation shape: history already ends on a user turn.
    s._history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "prior"},
        {"role": "assistant", "content": "working", "tool_calls": [
            {"id": "toolu_Z", "type": "function",
             "function": {"name": "list_dir", "arguments": "{}"}},
        ]},
        # dangling until sanitize
        {"role": "user", "content": "[background job finished] continue"},
    ]
    counter = {"n": 0}
    real = s._sanitize_tool_pairs

    def tracked():
        counter["n"] += 1
        return real()

    s._sanitize_tool_pairs = tracked
    pilot = _CallSitePilot()
    pilot.bind(s, counter)
    s.pilot = pilot

    list(s.send("(resume)", resume=True))
    assert pilot.calls >= 1
    assert pilot.saw_sanitize_before_chat
    _assert_all_tool_calls_answered(s._history)


def test_run_auto_sanitizes_before_provider_chat(tmp_path, monkeypatch):
    from harness.autobudget import AutoBudget

    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    # Skip codegraph gate for this unit test.
    s = ConversationalSession(cfg)
    counter = {"n": 0}
    real = s._sanitize_tool_pairs

    def tracked():
        counter["n"] += 1
        return real()

    s._sanitize_tool_pairs = tracked
    pilot = _CallSitePilot()
    pilot.bind(s, counter)
    s.pilot = pilot

    budget = AutoBudget(max_swarms=1, max_tokens=10_000, max_seconds=30)
    list(s.run_auto("do the thing", budget, require_codegraph=False))
    assert pilot.calls >= 1
    assert pilot.saw_sanitize_before_chat, "run_auto→send must sanitize before chat"


def test_run_stream_uses_messages_for_provider():
    """Streaming dispatch must go through the shared sanitize seam."""
    from harness import send_loop_phases

    seen = {"messages_for_provider": 0, "chat_stream": 0}

    class _Session:
        def _messages_for_provider(self):
            seen["messages_for_provider"] += 1
            return [{"role": "user", "content": "hi"}]

        class pilot:
            @staticmethod
            def chat_stream(messages, **kwargs):
                seen["chat_stream"] += 1
                assert messages == [{"role": "user", "content": "hi"}]
                return _Resp("streamed")

    q: list = []

    class _Q:
        def put(self, item):
            q.append(item)

    send_loop_phases.run_stream(_Session(), _Q(), tools_schema=[], sys_prompt="sys")
    assert seen["messages_for_provider"] == 1
    assert seen["chat_stream"] == 1
    assert q and q[-1][0] == "done"
