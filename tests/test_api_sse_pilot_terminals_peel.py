"""Characterization tests for sse/pilot/terminals HTTP peel wave."""
from __future__ import annotations

import base64
import io
import threading
from types import SimpleNamespace

from harness.api.pilot import PilotServices, get_pilot_swap, swap_pilot
from harness.api.sse import (
    SseEventRing,
    SseServices,
    get_chat_events,
)
from harness.api.streams import (
    resolve_stashed_chat_message,
    validate_upload_image_paths,
)
from harness.api.terminals import TerminalServices, stream_terminal


# ---------------------------------------------------------------------------
# GET /api/chat/events
# ---------------------------------------------------------------------------


def _sse_svc(rings=None, gens=None, default_sid=""):
    rings = {} if rings is None else rings
    gens = {} if gens is None else gens

    def lookup(sid, generation=None):
        if generation is not None:
            return rings.get((sid, int(generation)))
        gen = gens.get(sid)
        if gen is None:
            return None
        return rings.get((sid, int(gen)))

    return SseServices(
        ring_lookup=lookup,
        current_generation=lambda sid: gens.get(sid),
        default_session_id=lambda: default_sid,
    )


def test_get_chat_events_ok_replay():
    ring = SseEventRing("s1", 1)
    ring.append("token", {"text": "a"})
    ring.append("token", {"text": "b"})
    svc = _sse_svc(
        rings={("s1", 1): ring},
        gens={"s1": 1},
    )
    code, payload = get_chat_events(svc, "s1", 1, 1)
    assert code == 200
    assert payload["ok"] is True
    assert payload["missed"] is False
    assert payload["available"] is True
    assert len(payload["events"]) == 1
    assert payload["events"][0]["kind"] == "token"


def test_get_chat_events_ring_miss_and_generation_mismatch():
    ring = SseEventRing("s1", 2)
    ring.append("token", {"text": "x"})
    svc = _sse_svc(rings={("s1", 2): ring}, gens={"s1": 2})

    code, miss = get_chat_events(svc, "absent", 0, None)
    assert code == 200
    assert miss["ok"] is False
    assert miss["code"] == "ring_miss"
    assert miss["missed"] is True
    assert miss["available"] is False
    assert miss["events"] == []
    assert miss["cursor"] == 0

    code, mismatch = get_chat_events(svc, "s1", 0, 999)
    assert code == 200
    assert mismatch["code"] == "generation_mismatch"
    assert mismatch["generation"] == 2
    assert mismatch["missed"] is True
    assert mismatch["available"] is False


def test_get_chat_events_cursor_gap():
    ring = SseEventRing("s1", 1, cap=3)
    for i in range(5):
        ring.append("token", {"i": i})
    svc = _sse_svc(rings={("s1", 1): ring}, gens={"s1": 1})
    code, payload = get_chat_events(svc, "s1", 1, 1)
    assert code == 200
    assert payload["ok"] is False
    assert payload["code"] == "cursor_gap"
    assert payload["events"] == []
    assert payload["retained"] == 3
    assert payload["cursor"] == 5


def test_get_chat_events_uses_default_session():
    ring = SseEventRing("active", 1)
    ring.append("done", {})
    svc = _sse_svc(
        rings={("active", 1): ring},
        gens={"active": 1},
        default_sid="active",
    )
    code, payload = get_chat_events(svc, "", 0, None)
    assert code == 200
    assert payload["ok"] is True
    assert payload["session_id"] == "active"


# ---------------------------------------------------------------------------
# GET /api/pilot
# ---------------------------------------------------------------------------


def _pilot_svc(*, busy=False, swap_err=None, repo="/r"):
    lock = threading.Lock()
    if busy:
        lock.acquire()
    calls = {"swap": [], "window": 0, "save": []}
    cfg = SimpleNamespace(driver="old", repo=repo)

    def perform(model):
        if swap_err is not None:
            raise swap_err
        calls["swap"].append(model)
        cfg.driver = model

    svc = PilotServices(
        cfg=cfg,
        get_pilot=lambda: SimpleNamespace(_busy=lock),
        apply_model_context_window=lambda: calls.__setitem__(
            "window", calls["window"] + 1
        ),
        save_workspace_driver=lambda r, m: calls["save"].append((r, m)),
        perform_pilot_swap=perform,
    )
    return svc, cfg, calls, lock


def test_swap_pilot_empty_model():
    svc, _, _, lock = _pilot_svc()
    try:
        code, payload = swap_pilot("", svc)
        assert code == 400
        assert "model" in payload["error"]
    finally:
        if lock.locked():
            lock.release()


def test_swap_pilot_defers_when_busy():
    svc, cfg, calls, lock = _pilot_svc(busy=True)
    try:
        code, payload = get_pilot_swap("new-model", svc)
        assert code == 200
        assert payload == {"ok": True, "driver": "new-model", "deferred": True}
        assert cfg.driver == "new-model"
        assert calls["window"] == 1
        assert calls["save"] == [("/r", "new-model")]
        assert calls["swap"] == []
    finally:
        lock.release()


def test_swap_pilot_idle_rebuild():
    svc, cfg, calls, lock = _pilot_svc(busy=False)
    code, payload = get_pilot_swap("new-model", svc)
    assert code == 200
    assert payload == {"ok": True, "driver": "new-model", "deferred": False}
    assert calls["swap"] == ["new-model"]
    assert cfg.driver == "new-model"


def test_swap_pilot_exception_is_500():
    svc, _, _, _ = _pilot_svc(swap_err=RuntimeError("boom"))
    code, payload = swap_pilot("m", svc)
    assert code == 500
    assert "boom" in payload["error"]


# ---------------------------------------------------------------------------
# GET /api/terminal/stream
# ---------------------------------------------------------------------------


class _FakeWfile(io.BytesIO):
    def flush(self):
        pass


class _FakeHandler:
    def __init__(self):
        self.wfile = _FakeWfile()
        self.headers_sent = []
        self.status = None

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.headers_sent.append((k, v))

    def _cors(self):
        pass

    def end_headers(self):
        pass


def test_stream_terminal_missing_session_writes_exit():
    svc = TerminalServices(cfg=SimpleNamespace(repo=None), pty=SimpleNamespace(get=lambda sid: None))
    h = _FakeHandler()
    stream_terminal(h, "gone", svc)
    assert h.status == 200
    assert b'"kind": "exit"' in h.wfile.getvalue()


def test_stream_terminal_data_then_exit():
    class _Sess:
        def __init__(self):
            self._n = 0

        def alive(self):
            self._n += 1
            return self._n == 1

        def read_since(self, offset):
            if offset == 0 and self._n == 1:
                return b"hi", 2
            if offset == 2:
                return b"!", 3
            return b"", offset

    sess = _Sess()
    svc = TerminalServices(
        cfg=SimpleNamespace(repo=None),
        pty=SimpleNamespace(get=lambda sid: sess),
    )
    h = _FakeHandler()
    stream_terminal(h, "t1", svc)
    body = h.wfile.getvalue().decode()
    assert '"kind": "data"' in body
    assert base64.b64encode(b"hi").decode("ascii") in body
    assert '"kind": "exit"' in body


def test_stream_terminal_broken_pipe_is_swallowed():
    class _BoomWfile:
        def write(self, data):
            raise BrokenPipeError()

        def flush(self):
            pass

    class _Sess:
        def alive(self):
            return True

        def read_since(self, offset):
            return b"x", offset + 1

    h = _FakeHandler()
    h.wfile = _BoomWfile()
    svc = TerminalServices(
        cfg=SimpleNamespace(repo=None),
        pty=SimpleNamespace(get=lambda sid: _Sess()),
    )
    stream_terminal(h, "t1", svc)  # must not raise


def test_stream_terminal_exception_still_emits_exit_via_finally():
    """Unexpected read errors must still deliver kind:exit while writable."""

    class _Sess:
        def alive(self):
            return True

        def read_since(self, offset):
            raise RuntimeError("pty read failed")

    h = _FakeHandler()
    svc = TerminalServices(
        cfg=SimpleNamespace(repo=None),
        pty=SimpleNamespace(get=lambda sid: _Sess()),
    )
    stream_terminal(h, "t1", svc)
    assert b'"kind": "exit"' in h.wfile.getvalue()


def test_stream_terminal_broken_pipe_skips_exit_in_finally():
    """Client disconnect must not attempt a post-detach exit frame."""

    class _BoomWfile:
        def __init__(self):
            self.writes = []

        def write(self, data):
            self.writes.append(data)
            raise BrokenPipeError()

        def flush(self):
            pass

    class _Sess:
        def alive(self):
            return True

        def read_since(self, offset):
            return b"x", offset + 1

    boom = _BoomWfile()
    h = _FakeHandler()
    h.wfile = boom
    svc = TerminalServices(
        cfg=SimpleNamespace(repo=None),
        pty=SimpleNamespace(get=lambda sid: _Sess()),
    )
    stream_terminal(h, "t1", svc)
    # First write blows with BrokenPipe; finally must not retry exit.
    assert len(boom.writes) == 1
    assert b'"kind": "exit"' not in boom.writes[0]


# ---------------------------------------------------------------------------
# streams helpers (chat/run thinning)
# ---------------------------------------------------------------------------


def test_validate_upload_image_paths(tmp_path):
    good = tmp_path / "a.png"
    good.write_bytes(b"x")
    imgs, err = validate_upload_image_paths(str(good), str(tmp_path))
    assert err is None
    assert imgs == [str(good)]

    imgs, err = validate_upload_image_paths("/tmp/evil.png", str(tmp_path))
    assert imgs is None
    assert err[0] == 400
    assert "Invalid image path" in err[1]["error"]


def test_resolve_stashed_chat_message():
    stash = {
        "m1": {"message": "hello", "images": ["/u/a.png", "/u/b.png"]},
    }
    msg, imgs = resolve_stashed_chat_message(
        "m1", "ignored", "", lambda mid: stash.get(mid)
    )
    assert msg == "hello"
    assert imgs == "/u/a.png|/u/b.png"

    # Query images win when already present.
    msg, imgs = resolve_stashed_chat_message(
        "m1", "ignored", "/u/q.png", lambda mid: stash.get(mid)
    )
    assert msg == "hello"
    assert imgs == "/u/q.png"

    # Unknown mid falls through.
    msg, imgs = resolve_stashed_chat_message(
        "gone", "keep", "i.png", lambda mid: None
    )
    assert msg == "keep" and imgs == "i.png"
