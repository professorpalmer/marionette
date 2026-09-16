"""Real driver transports under abandoned summarizer/live request overlap."""
import copy
import io
import json
import threading
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from pmharness.drivers.compaction import compaction_driver
from pmharness.drivers.openai_compat import OpenAICompatDriver
from pmharness.drivers.anthropic import AnthropicDriver
from pmharness.drivers.codex_responses import CodexResponsesDriver
from pmharness.drivers.gemini import GeminiDriver
from pmharness.drivers.cursor_acp import CursorAcpDriver, WarmAcpSession
from pmharness.drivers.cursor_cli import CursorCliDriver
from pmharness.drivers.base import DriverResponse


def fat_session(tmp_path, monkeypatch, driver):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "summary")
    monkeypatch.setenv("HARNESS_COMPACTION_MODEL", "summary-model")
    session = ConversationalSession(HarnessConfig(
        driver="stub-oracle-v2", state_dir=str(tmp_path), max_context_tokens=4000,
    ))
    session.pilot = driver
    session._history = [{"role": "system", "content": "system"}] + [
        {"role": "user", "content": f"message {i} " + "x" * 500} for i in range(30)
    ]
    return session


def timeout_threads(monkeypatch, entered):
    real = threading.Thread
    workers = []

    class TimeoutThread(real):
        def join(self, timeout=None):
            workers.append(self)
            assert entered.wait(5)

    monkeypatch.setattr("harness.compaction_mixin.threading", SimpleNamespace(Thread=TimeoutThread))
    return workers, real


def make_http(kind):
    args = dict(name="live", model="live-model", base_url="https://example.invalid/v1",
                api_key_env="UNUSED_TEST_KEY", max_tokens=987, timeout=13)
    if kind == "openai":
        return OpenAICompatDriver(**args, extra_headers={"X-Test": "preserved"},
                                  extra_body={"metadata": {"purpose": "original"}})
    if kind == "anthropic":
        return AnthropicDriver(**args, version="2023-06-01", enable_prompt_cache=False)
    if kind == "codex":
        return CodexResponsesDriver(**args, chatgpt_backend=False)
    return GeminiDriver(**args)


def response_bytes(kind, text):
    if kind == "openai":
        raw = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
    elif kind == "anthropic":
        raw = {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
    elif kind == "gemini":
        raw = {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}
    else:
        raw = {"type": "response.completed", "response": {"status": "completed",
               "output": [{"type": "message", "role": "assistant", "content": [
                   {"type": "output_text", "text": text}]}]}}
        delta = {"type": "response.output_text.delta", "delta": text}
        return ("data: " + json.dumps(delta) + "\n\ndata: " + json.dumps(raw) + "\n\n").encode()
    return json.dumps(raw).encode()


@pytest.mark.parametrize("kind", ["openai", "anthropic", "codex", "gemini"])
@pytest.mark.parametrize("late_error", [False, True])
def test_http_timeout_overlap(tmp_path, monkeypatch, kind, late_error):
    driver = make_http(kind)
    monkeypatch.setattr(type(driver), "_key", lambda self: "local-test-token")
    entered, finish = threading.Event(), threading.Event()
    seen, observed = [], []
    driver._request_body_observer = observed.append

    def transport(request, timeout):
        body = json.loads(request.data)
        model = body.get("model") or request.full_url.split("/models/")[1].split(":")[0]
        seen.append((model, body, dict(request.header_items()), timeout))
        if model == "summary-model":
            entered.set()
            finish.wait()  # Released by the test's finally, not a transport deadline.
            if late_error:
                raise ValueError("late local transport failure")
        return io.BytesIO(response_bytes(kind, model))

    monkeypatch.setattr("urllib.request.urlopen", transport)
    session = fat_session(tmp_path, monkeypatch, driver)
    workers, real = timeout_threads(monkeypatch, entered)
    try:
        events = list(session._maybe_compact_history(force=True))
        assert events[-1].data["mode"] == "extractive"
        session._busy_gen += 1
        session._history.append({"role": "user", "content": "new owner"})
        after = copy.deepcopy(session._history)
        driver.model = "new-model"
        result = driver.chat([{"role": "user", "content": "new owner"}], system="new system")
        assert result.error is None
        assert result.text == "new-model"
    finally:
        finish.set()
        for worker in workers:
            real.join(worker, 5)
            assert not worker.is_alive()
    assert driver.model == "new-model"
    assert session._history == after
    assert [item[0] for item in seen] == ["summary-model", "new-model"]
    assert all(item[3] == 13 for item in seen)
    if kind != "gemini":
        assert len(observed) == 1
        assert json.loads(observed[0])["model"] == "new-model"
    if kind == "openai":
        assert seen[0][1]["metadata"] == {"purpose": "original"}
        assert seen[0][2]["X-test"] == "preserved"
    if kind == "codex":
        assert seen[0][1]["max_output_tokens"] == 987
        assert "client_metadata" not in seen[0][1]


@pytest.mark.parametrize("late_error", [False, True])
def test_acp_timeout_cannot_clear_new_callback_or_close_live_transport(tmp_path, monkeypatch, late_error):
    entered, finish = threading.Event(), threading.Event()
    transports = []

    class Transport:
        def __init__(self):
            self.handler = None
            self.closed = False
            self.model = None
            transports.append(self)

        def alive(self):
            return not self.closed

        def close(self):
            self.closed = True

        def notify(self, *args):
            pass

        def set_session_update_handler(self, handler):
            self.handler = handler

        def request(self, method, params, timeout):
            if method == "session/new":
                self.model = params.get("model")
                return {"result": {"sessionId": str(len(transports))}}
            if method != "session/prompt":
                return {"result": {}}
            if "new owner" not in params["prompt"][0]["text"]:
                entered.set()
                finish.wait()  # Released after the live-owner assertions, even on failure.
                if late_error:
                    raise ValueError("late ACP failure")
            self.handler({"update": {"sessionUpdate": "agent_message_chunk",
                                     "content": {"type": "text", "text": "local reply"}}})
            return {"result": {"stopReason": "end_turn"}}

    warm = WarmAcpSession(model="auto", cwd=str(tmp_path), transport_factory=Transport)
    pilot = CursorAcpDriver(name="cursor", model="auto", session=warm, cwd=str(tmp_path))
    monkeypatch.setattr("pmharness.drivers.cursor_acp.cursor_acp_enabled", lambda model: True)
    fallbacks = []
    def fallback(self, *args, **kwargs):
        fallbacks.append(self.model)
        return DriverResponse(text="local fallback")
    monkeypatch.setattr(CursorCliDriver, "_run_stream", fallback)
    session = fat_session(tmp_path, monkeypatch, pilot)
    workers, real = timeout_threads(monkeypatch, entered)
    chunks = []
    try:
        list(session._maybe_compact_history(force=True))
        pilot.model = "new-model"
        result = pilot.chat_stream([{"role": "user", "content": "new owner"}], on_delta=chunks.append)
        assert result.error is None
        live = warm.transport
        assert live is not None
        marker = lambda value: None
        live.set_session_update_handler(marker)
        after = copy.deepcopy(session._history)
    finally:
        finish.set()
        for worker in workers:
            real.join(worker, 5)
            assert not worker.is_alive()
    assert len(transports) == 2
    assert transports[0].closed
    assert not live.closed
    assert live.handler is marker
    assert chunks == ["local reply"]
    assert pilot.model == "new-model"
    assert pilot._fallback.model == "auto"
    assert fallbacks == (["summary-model"] if late_error else [])
    assert session._history == after
    warm.close()


def test_mutable_config_and_frozen_body_are_separate():
    from harness.request_snapshot import FrozenRequest
    driver = make_http("openai")
    frozen = FrozenRequest.capture(driver.chat, [{"role": "user", "content": "old"}], {}, driver.model)
    with ExitStack() as resources:
        local = compaction_driver(frozen.method.__self__, "summary-model", resources)
        driver.extra_body["metadata"]["purpose"] = "changed"
        driver.extra_headers["X-Test"] = "changed"
        body = local._build_chat_body([{"role": "user", "content": "summary"}])
        assert body["model"] == "summary-model"
        assert body["messages"][-1]["content"] == "summary"
        assert local.extra_body["metadata"]["purpose"] == "original"
        assert local.extra_headers["X-Test"] == "preserved"
        assert not hasattr(local, "_request_body_observer")
        assert frozen.boundary.attempts == 0


def test_moa_children_and_cassette_record_are_separate(tmp_path, monkeypatch):
    from pmharness.drivers.moa import MoADriver
    from pmharness.drivers.cassette import CassetteDriver
    moa = MoADriver("moa", ["a", "b"], "c", builder=lambda *a, **k: make_http("openai"))
    cassette = CassetteDriver(moa, mode="record", cassette_dir=str(tmp_path))
    with ExitStack() as resources:
        local = compaction_driver(cassette, "", resources)
        assert local._inner is not moa
        for a, b in zip(local._inner.proposer_drivers, moa.proposer_drivers):
            assert a is not b
            assert a.extra_body is not b.extra_body
        assert local._inner.aggregator_driver is not moa.aggregator_driver
        assert local._data is not cassette._data
        local._record("summary", "chat", {}, DriverResponse(text="summary"))
        cassette._record("live", "chat", {}, DriverResponse(text="live"))
        assert [i["request_hash"] for i in json.loads(open(cassette._path).read())["interactions"]] == ["summary", "live"]
        with pytest.raises(ValueError, match="no single model"):
            compaction_driver(moa, "summary-model", resources)


def test_unknown_driver_fails_closed():
    with ExitStack() as resources, pytest.raises(TypeError, match="request-local"):
        compaction_driver(object(), "summary-model", resources)


@pytest.mark.parametrize("late_error", [False, True])
def test_bedrock_timeout_overlap(tmp_path, monkeypatch, late_error):
    from pmharness.drivers.bedrock import BedrockDriver
    pilot = BedrockDriver("bedrock", "live-model", max_tokens=987,
                          timeout=13, temperature=0.25, send_temperature=True)
    monkeypatch.setattr(BedrockDriver, "_ensure_auth", lambda self: None)
    entered, finish = threading.Event(), threading.Event()
    seen = []

    def transport(**kwargs):
        seen.append(kwargs)
        if kwargs["model"] == "summary-model":
            entered.set()
            assert finish.wait(5)
            if late_error:
                raise ValueError("late Bedrock failure")
        return SimpleNamespace(text=kwargs["model"], usage={}, tool_calls=[], finish_reason="stop")

    monkeypatch.setattr("puppetmaster.bedrock.bedrock_chat", transport)
    session = fat_session(tmp_path, monkeypatch, pilot)
    workers, real = timeout_threads(monkeypatch, entered)
    try:
        list(session._maybe_compact_history(force=True))
        pilot.model = "new-model"
        result = pilot.chat([{"role": "user", "content": "new owner"}])
        assert result.text == "new-model"
        after = copy.deepcopy(session._history)
    finally:
        finish.set()
        for worker in workers:
            real.join(worker, 5)
            assert not worker.is_alive()
    assert [call["model"] for call in seen] == ["summary-model", "new-model"]
    assert seen[0]["extra"] == {"max_tokens": 987, "temperature": 0.25}
    assert seen[0]["timeout"] == 13
    assert pilot.model == "new-model"
    assert session._history == after


@pytest.mark.parametrize("late_error", [False, True])
def test_cursor_cli_timeout_preserves_resume_binding(tmp_path, monkeypatch, late_error):
    entered, finish = threading.Event(), threading.Event()
    cmds = []
    monkeypatch.setattr("pmharness.drivers.cursor_cli.resolve_agent_exec", lambda *a: ["local-agent"])
    monkeypatch.setattr(CursorCliDriver, "_binary", lambda self: "local-agent")

    class Proc:
        returncode = 0
        def __init__(self, model):
            self.stdout = io.StringIO(json.dumps({
                "type": "result", "is_error": False, "result": model,
                "model": model, "session_id": "new-native", "usage": {},
            }) + "\n")
            self.stderr = io.StringIO("")
        def wait(self, timeout=None):
            return 0
        def kill(self):
            pass

    def popen(cmd, **kwargs):
        cmds.append(cmd)
        model = cmd[cmd.index("--model") + 1]
        if model == "summary-model":
            entered.set()
            assert finish.wait(5)
            if late_error:
                raise ValueError("late CLI failure")
        return Proc(model)

    monkeypatch.setattr("pmharness.drivers.cursor_cli.subprocess.Popen", popen)
    pilot = CursorCliDriver("cursor", "live-model", cwd=str(tmp_path))
    pilot._harness_session_id = "live-session"
    pilot._native_chat_id = "old-native"
    pilot._bound_model = "live-model"
    pilot._bound_workspace = str(tmp_path)
    session = fat_session(tmp_path, monkeypatch, pilot)
    workers, real = timeout_threads(monkeypatch, entered)
    try:
        list(session._maybe_compact_history(force=True))
        chunks = []
        result = pilot.chat_stream([{"role": "user", "content": "new owner"}],
                                   session_id="live-session", on_delta=chunks.append)
        assert result.error is None
        assert pilot._native_chat_id == "new-native"
        after = copy.deepcopy(session._history)
    finally:
        finish.set()
        for worker in workers:
            real.join(worker, 5)
            assert not worker.is_alive()
    assert "--resume" not in cmds[0]
    assert cmds[1][cmds[1].index("--resume") + 1] == "old-native"
    assert cmds[0][cmds[0].index("--model") + 1] == "summary-model"
    assert pilot.model == "live-model"
    assert pilot._native_chat_id == "new-native"
    assert session._history == after


def test_cassette_override_records_and_replays_actual_model(tmp_path, monkeypatch):
    from pmharness.drivers.cassette import CassetteDriver
    pilot = make_http("openai")
    monkeypatch.setattr(OpenAICompatDriver, "_key", lambda self: "local-test-token")
    captured = []
    def transport(request, timeout):
        captured.append(json.loads(request.data)["model"])
        return io.BytesIO(response_bytes("openai", "recorded summary"))
    monkeypatch.setattr("urllib.request.urlopen", transport)
    recorder = CassetteDriver(pilot, mode="record", cassette_dir=str(tmp_path))
    messages = [{"role": "user", "content": "summarize"}]
    with ExitStack() as resources:
        local = compaction_driver(recorder, "summary-model", resources)
        assert local.chat(messages).text == "recorded summary"
    replay = CassetteDriver(pilot, mode="replay", cassette_dir=str(tmp_path))
    with ExitStack() as resources:
        local = compaction_driver(replay, "summary-model", resources)
        assert local.chat(messages).text == "recorded summary"
    assert captured == ["summary-model"]
    assert recorder.model == "live-model"
