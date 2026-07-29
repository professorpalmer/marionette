"""Prompt-cache affinity: stable harness_session_id → pilot session_id kwarg."""
from __future__ import annotations

import queue
from types import SimpleNamespace
from typing import Dict, List, Optional

from harness.send_loop_phases import (
    maybe_attach_pilot_session_id,
    pilot_accepts_session_id,
    run_stream,
)
from pmharness.drivers.codex_responses import CodexResponsesDriver


def _stream_session(pilot, harness_session_id: str, *, messages=None):
    history = messages or [{"role": "user", "content": "hi"}]
    return SimpleNamespace(
        pilot=pilot,
        harness_session_id=harness_session_id,
        _messages_for_provider=lambda: history,
    )


def test_pilot_accepts_session_id_explicit_param():
    def chat_stream(messages, *, session_id=None, on_delta):
        pass

    assert pilot_accepts_session_id(chat_stream) is True


def test_pilot_accepts_session_id_kwargs_driver():
    def chat_stream(messages, **kwargs):
        pass

    assert pilot_accepts_session_id(chat_stream) is True


def test_pilot_accepts_session_id_legacy_driver():
    def chat_stream(
        messages,
        *,
        tools=None,
        system=None,
        on_delta,
        on_reasoning_delta=None,
        on_tool_hint=None,
    ):
        pass

    assert pilot_accepts_session_id(chat_stream) is False


def test_maybe_attach_skips_empty_session_id():
    kwargs: dict = {}
    maybe_attach_pilot_session_id(
        kwargs,
        lambda messages, *, session_id=None: None,
        "",
    )
    assert "session_id" not in kwargs


def test_maybe_attach_skips_legacy_driver():
    kwargs: dict = {}

    def legacy(messages, *, tools=None, system=None):
        pass

    maybe_attach_pilot_session_id(kwargs, legacy, "sess-a")
    assert "session_id" not in kwargs


def test_run_stream_passes_stable_session_id_across_turns():
    seen: List[Optional[str]] = []

    def chat_stream(messages, **kwargs):
        seen.append(kwargs.get("session_id"))
        return SimpleNamespace(text="ok")

    pilot = SimpleNamespace(chat_stream=chat_stream, supports_streaming=True)
    session = _stream_session(pilot, "marionette-chat-7")

    for _ in range(2):
        q: queue.Queue = queue.Queue()
        run_stream(session, q, [], "sys")
        while not q.empty():
            q.get_nowait()

    assert seen == ["marionette-chat-7", "marionette-chat-7"]


def test_run_stream_resume_turn_reuses_same_session_id():
    """Resume keeps harness_session_id on the session object — same cache key."""
    seen: List[Optional[str]] = []

    def chat_stream(messages, **kwargs):
        seen.append(kwargs.get("session_id"))
        return SimpleNamespace(text="ok")

    pilot = SimpleNamespace(chat_stream=chat_stream, supports_streaming=True)
    session = _stream_session(pilot, "resume-sess-1")

    q1: queue.Queue = queue.Queue()
    run_stream(session, q1, [], "sys")
    while not q1.empty():
        q1.get_nowait()

    # Resume path reuses the same session runner; harness_session_id unchanged.
    q2: queue.Queue = queue.Queue()
    run_stream(session, q2, [], "sys")
    while not q2.empty():
        q2.get_nowait()

    assert seen == ["resume-sess-1", "resume-sess-1"]


def test_run_stream_different_chat_sessions_get_different_ids():
    captured: Dict[str, Optional[str]] = {}

    def make_chat_stream(label):
        def chat_stream(messages, **kwargs):
            captured[label] = kwargs.get("session_id")
            return SimpleNamespace(text="ok")
        return chat_stream

    for label, sid in (("a", "chat-alpha"), ("b", "chat-beta")):
        pilot = SimpleNamespace(
            chat_stream=make_chat_stream(label),
            supports_streaming=True,
        )
        session = _stream_session(pilot, sid)
        q: queue.Queue = queue.Queue()
        run_stream(session, q, [], "sys")
        while not q.empty():
            q.get_nowait()

    assert captured == {"a": "chat-alpha", "b": "chat-beta"}
    assert captured["a"] != captured["b"]


def test_run_stream_legacy_driver_receives_no_session_id_kwarg():
    """Anthropic-style signature must not get an unexpected session_id kwarg."""
    q: queue.Queue = queue.Queue()

    def chat_stream(
        messages,
        *,
        tools=None,
        system=None,
        on_delta,
        on_reasoning_delta=None,
        on_tool_hint=None,
    ):
        return SimpleNamespace(text="ok")

    session = _stream_session(
        SimpleNamespace(chat_stream=chat_stream),
        "sess-legacy",
    )
    run_stream(session, q, [], "sys")
    kind, val = q.get_nowait()
    assert kind == "done"
    assert val.text == "ok"


def test_run_stream_kwargs_driver_receives_session_id():
    captured: dict = {}

    def chat_stream(messages, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(text="ok")

    session = _stream_session(
        SimpleNamespace(chat_stream=chat_stream),
        "kwargs-sess",
    )
    q: queue.Queue = queue.Queue()
    run_stream(session, q, [], "sys")
    while not q.empty():
        q.get_nowait()
    assert captured["session_id"] == "kwargs-sess"


def test_sync_chat_path_attaches_session_id():
    captured: dict = {}

    def chat(messages, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(text="ok", error=None)

    kwargs: dict = {"tools": [], "system": "sys"}
    maybe_attach_pilot_session_id(kwargs, chat, "sync-sess-9")
    chat([{"role": "user", "content": "hi"}], **kwargs)
    assert captured["session_id"] == "sync-sess-9"


def test_codex_build_body_sets_prompt_cache_key_from_session_id():
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5")
    body = driver._build_body(
        [{"role": "user", "content": "hello"}],
        session_id="codex-stable-sess",
    )
    assert body["prompt_cache_key"] == "codex-stable-sess"


def test_codex_build_body_omits_prompt_cache_key_without_session_id():
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5")
    body = driver._build_body(
        [{"role": "user", "content": "hello"}],
        session_id=None,
    )
    assert "prompt_cache_key" not in body


def test_codex_chat_stream_wires_session_id_to_body(monkeypatch):
    """End-to-end: run_stream session_id → CodexResponsesDriver._build_body."""
    posted: list[dict] = []

    class _CaptureDriver(CodexResponsesDriver):
        def _post_stream(self, body, **kwargs):
            posted.append(body)
            return SimpleNamespace(
                text="ok",
                tokens_in=0,
                tokens_out=0,
                latency_ms=0,
                model=self.name,
                meta={},
            )

    driver = _CaptureDriver(name="openai-codex/test", model="gpt-5")
    session = _stream_session(
        SimpleNamespace(chat_stream=driver.chat_stream, supports_streaming=True),
        "codex-wire-sess",
    )
    q: queue.Queue = queue.Queue()
    run_stream(session, q, [], "sys")
    while not q.empty():
        q.get_nowait()

    assert posted, "expected Codex driver to post a body"
    assert posted[0]["prompt_cache_key"] == "codex-wire-sess"
