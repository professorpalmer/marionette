"""ConvEvent / SSE ring wire-shape characterization.

Asserts JSON key shapes — not handler dispatch or send_loop behavior.
Chat ConvEvent frames omit ``turn``; SessionEvent /run frames include it;
``SseEventRing.append`` only adds ``turn`` when provided (getattr path).
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import get_args, get_type_hints

from harness.api.sse import SseEventRing, SseRingEvent, StreamEventDict, sse_pump
from harness.api.streams import _encode_chat_sse_frame, _encode_run_sse_frame
from harness.conversation import VALID_CONV_EVENT_KINDS, ConvEvent, ConvEventKind
from harness.session import VALID_SESSION_EVENT_KINDS, SessionEvent, SessionEventKind


def test_conv_event_kind_literal_matches_valid_set():
    assert frozenset(get_args(ConvEventKind)) == VALID_CONV_EVENT_KINDS
    assert "done" not in VALID_CONV_EVENT_KINDS  # framing-only; not a ConvEvent
    assert "vault_cite" in VALID_CONV_EVENT_KINDS
    hints = get_type_hints(ConvEvent)
    assert hints["kind"] is ConvEventKind


def test_session_event_kind_literal_matches_valid_set():
    assert frozenset(get_args(SessionEventKind)) == VALID_SESSION_EVENT_KINDS
    hints = get_type_hints(SessionEvent)
    assert hints["kind"] is SessionEventKind
    # Shapes stay split: SessionEvent has turn; ConvEvent does not.
    assert "turn" in SessionEvent.__dataclass_fields__
    assert "turn" not in ConvEvent.__dataclass_fields__


def test_chat_sse_frame_omits_turn():
    ev = ConvEvent("message", {"role": "assistant", "text": "hi"})
    raw = _encode_chat_sse_frame(ev)
    assert raw.startswith(b"data: ")
    payload = json.loads(raw[len(b"data: "):].split(b"\n\n", 1)[0])
    assert set(payload.keys()) == {"kind", "data"}
    assert payload["kind"] == "message"
    assert payload["data"]["text"] == "hi"
    assert "turn" not in payload


def test_chat_sse_frame_includes_ring_identity_when_stamped():
    ev = SimpleNamespace(
        kind="thinking",
        data={"text": "inspect", "delta": True},
        cursor=7,
        generation=3,
    )
    raw = _encode_chat_sse_frame(ev)
    payload = json.loads(raw[len(b"data: "):].split(b"\n\n", 1)[0])
    assert payload["cursor"] == 7
    assert payload["generation"] == 3


def test_chat_sse_pump_stamps_primary_frame_with_retained_identity():
    class Wfile:
        def __init__(self):
            self.chunks = []
            self.ring_events_at_write = []

        def write(self, payload):
            order.append("write")
            self.ring_events_at_write = ring.since(0)["events"]
            self.chunks.append(payload)

        def flush(self):
            pass

    ring = SseEventRing("sess-primary", generation=4)
    wfile = Wfile()
    event = ConvEvent(
        "thinking",
        {"text": "", "delta": False, "count": 0, "optional": None},
    )
    order = []
    generator_yields = []
    observed_events = []

    def one_event():
        generator_yields.append(event)
        yield event

    def checkpoint(ev):
        order.append("checkpoint")
        observed_events.append(ev)

    sse_pump(
        wfile,
        one_event(),
        _encode_chat_sse_frame,
        on_event=checkpoint,
        write_done=False,
        ring=ring,
    )
    assert generator_yields == [event]
    assert len(wfile.chunks) == 1
    assert order == ["write", "checkpoint"]
    assert observed_events[0] is event
    assert wfile.ring_events_at_write == [{
        "cursor": 1,
        "kind": "thinking",
        "data": {"text": "", "delta": False, "count": 0, "optional": None},
    }]
    payload = json.loads(wfile.chunks[0][len(b"data: "):].split(b"\n\n", 1)[0])
    assert payload["cursor"] == ring.since(0)["events"][0]["cursor"] == 1
    assert payload["generation"] == ring.generation == 4
    assert payload["data"] == event.data


def test_chat_sse_message_preserves_streamed_metadata():
    """send_loop flags already-deltaed prose with streamed=true; wire must keep it."""
    ev = ConvEvent(
        "message",
        {"role": "assistant", "text": "Looking at the read path first.", "streamed": True},
    )
    raw = _encode_chat_sse_frame(ev)
    payload = json.loads(raw[len(b"data: "):].split(b"\n\n", 1)[0])
    assert payload["kind"] == "message"
    assert payload["data"]["streamed"] is True
    assert payload["data"]["text"] == "Looking at the read path first."

    # Ring replay must also retain the flag for cursor_gap hydrate+replay.
    ring = SseEventRing("sess-streamed", 1)
    ring.append("message", {"role": "assistant", "text": "hi", "streamed": True})
    snap = ring.since(0)
    assert snap["events"][0]["data"]["streamed"] is True


def test_run_sse_frame_includes_turn():
    ev = SessionEvent("intent", 2, {"action": "answer"})
    raw = _encode_run_sse_frame(ev)
    payload = json.loads(raw[len(b"data: "):].split(b"\n\n", 1)[0])
    assert set(payload.keys()) == {"kind", "turn", "data"}
    assert payload["kind"] == "intent"
    assert payload["turn"] == 2


def test_ring_append_turn_optional_json_shape():
    ring = SseEventRing("sess", 1)
    c1 = ring.append("message", {"text": "a"})
    c2 = ring.append("intent", {"action": "answer"}, turn=3)
    c3 = ring.append("done", {})
    assert (c1, c2, c3) == (1, 2, 3)

    snap = ring.since(0)
    events = snap["events"]
    assert len(events) == 3

    chat_ev = events[0]
    assert chat_ev["cursor"] == 1
    assert chat_ev["kind"] == "message"
    assert chat_ev["data"] == {"text": "a"}
    assert "turn" not in chat_ev

    run_ev = events[1]
    assert run_ev["kind"] == "intent"
    assert run_ev["turn"] == 3
    assert run_ev["data"] == {"action": "answer"}

    done_ev = events[2]
    assert done_ev["kind"] == "done"
    assert done_ev["data"] == {}
    assert "turn" not in done_ev


def test_stream_and_ring_typeddict_required_keys():
    """TypedDict contracts stay aligned with webapp StreamEvent / ChatEventFrame."""
    stream_required = StreamEventDict.__required_keys__
    stream_optional = StreamEventDict.__optional_keys__
    assert "kind" in stream_required
    assert "data" in stream_optional
    assert "turn" in stream_optional
    assert "cursor" in stream_optional
    assert "generation" in stream_optional

    ring_required = SseRingEvent.__required_keys__
    ring_optional = SseRingEvent.__optional_keys__
    assert {"cursor", "kind", "data"} <= ring_required
    assert "turn" in ring_optional
