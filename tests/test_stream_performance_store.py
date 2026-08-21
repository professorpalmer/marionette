"""Durable stream-performance receipts + GET /api/session/performance."""

from __future__ import annotations

import json
import math
import multiprocessing
import os
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.api.session_control import SessionControlServices
from harness.api.session_performance import get_session_performance
from harness.api.sessions import handle_session_delete, remove_session_transcript
from harness.stream_performance import (
    FIRST_CONTENT_CALLBACK_MS,
    PROVIDER_CALL_TOTAL_MS,
    PROVIDER_OUTPUT_TPS,
    STREAM_PERFORMANCE_KEY,
    THROUGHPUT_BASIS,
    THROUGHPUT_BASIS_KEY,
)
from harness.stream_performance_store import (
    MAX_RECEIPTS_PER_SESSION,
    RECEIPT_SCHEMA_VERSION,
    StreamPerformanceReceiptStore,
    build_receipt,
    copy_stream_performance,
    receipt_file_path,
    remove_session_performance_receipts,
    safe_session_id,
)
from harness.sessions import SessionStore, save_transcript
from pmharness.drivers.base import DriverResponse


def _receipt(session_id: str, **kwargs):
    kwargs.setdefault("status", "success")
    kwargs.setdefault("stream_performance", {"content_delta_count": 1})
    return build_receipt(session_id=session_id, **kwargs)


def _perf_svc(*, state_dir, sessions=None, pilot=None, repo=""):
    return SessionControlServices(
        cfg=SimpleNamespace(
            driver="openai/gpt-test",
            state_dir=str(state_dir),
            repo=repo,
        ),
        get_pilot=lambda: pilot or SimpleNamespace(harness_session_id=""),
        get_runners=lambda: SimpleNamespace(get=lambda sid: None, statuses=lambda: {}, active_view_id=""),
        gate_active_pilot_ready=lambda: None,
        stash_put=lambda msg, imgs: "mid",
        save_active_transcript=lambda: None,
        upload_dir="/uploads",
        diag=lambda *a, **k: None,
        get_sessions=lambda: sessions or SimpleNamespace(active=None, rows=lambda: []),
    )


def _make_symlink(target: Path, link: Path) -> None:
    try:
        if target.is_dir():
            link.symlink_to(target, target_is_directory=True)
        else:
            link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("symlinks not supported: {0}".format(exc))


def _mp_record_one(state_dir: str, session_id: str, step: int) -> None:
    """Spawn-safe worker for the cross-process RMW regression."""
    from harness.stream_performance_store import (
        StreamPerformanceReceiptStore,
        build_receipt,
    )

    store = StreamPerformanceReceiptStore(state_dir)
    store.record(
        session_id,
        build_receipt(
            session_id=session_id,
            provider_step=step,
            captured_at=float(step),
            stream_performance={"content_delta_count": 1},
            status="success",
        ),
    )


def test_safe_session_id_strips_traversal():
    assert safe_session_id("../etc/passwd") == "etcpasswd"
    assert safe_session_id("sess-1_ab") == "sess-1_ab"
    assert safe_session_id("") == ""


def test_receipt_path_stays_inside_performance_dir(tmp_path):
    sneaky = receipt_file_path(str(tmp_path), "../../etc/passwd")
    root = (tmp_path / "stream_performance").resolve()
    if sneaky:
        resolved = Path(sneaky).resolve()
        assert os.path.commonpath([str(root), str(resolved)]) == str(root)
        assert resolved.name == "etcpasswd.json"
    assert not (tmp_path / "etc").exists()
    assert receipt_file_path(str(tmp_path), "") == ""


def test_atomic_bounded_newest_chronological(tmp_path):
    store = StreamPerformanceReceiptStore(str(tmp_path), max_receipts=3)
    sid = "sess-bound"
    for i in range(5):
        store.record(sid, _receipt(sid, provider_step=i, captured_at=1000.0 + i))
    rows = store.list_receipts(sid)
    assert [r["provider_step"] for r in rows] == [2, 3, 4]
    assert all(rows[i]["captured_at"] <= rows[i + 1]["captured_at"] for i in range(len(rows) - 1))
    path = Path(receipt_file_path(str(tmp_path), sid))
    assert path.is_file()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["schema_version"] == RECEIPT_SCHEMA_VERSION
    assert len(doc["receipts"]) == 3


def test_session_isolation(tmp_path):
    store = StreamPerformanceReceiptStore(str(tmp_path))
    store.record("alpha", _receipt("alpha", provider_step=1))
    store.record("beta", _receipt("beta", provider_step=9))
    assert [r["provider_step"] for r in store.list_receipts("alpha")] == [1]
    assert [r["provider_step"] for r in store.list_receipts("beta")] == [9]
    assert store.list_receipts("missing") == []


def test_corrupt_file_fails_soft_and_next_write_repairs(tmp_path):
    store = StreamPerformanceReceiptStore(str(tmp_path))
    sid = "sess-corrupt"
    path = Path(receipt_file_path(str(tmp_path), sid))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")
    assert store.list_receipts(sid) == []
    store.record(sid, _receipt(sid, provider_step=3))
    rows = store.list_receipts(sid)
    assert len(rows) == 1
    assert rows[0]["provider_step"] == 3
    json.loads(path.read_text(encoding="utf-8"))


def test_json_safety_omits_nonfinite_and_unknown_keys():
    copied = copy_stream_performance({
        "content_delta_count": 2,
        PROVIDER_CALL_TOTAL_MS: 12.5,
        PROVIDER_OUTPUT_TPS: float("inf"),
        FIRST_CONTENT_CALLBACK_MS: float("nan"),
        "decode_window_ms": -0.0,
        "secret_prompt": "do not persist",
        "boom": RuntimeError("nope"),
        "nested": {"x": 1},
    })
    assert copied["content_delta_count"] == 2
    assert copied[PROVIDER_CALL_TOTAL_MS] == 12.5
    assert PROVIDER_OUTPUT_TPS not in copied
    assert FIRST_CONTENT_CALLBACK_MS not in copied
    assert "secret_prompt" not in copied
    assert "boom" not in copied
    receipt = build_receipt(
        session_id="s1",
        stream_performance=copied,
        status="success",
        driver="openai/gpt-test",
        captured_at=float("nan"),
    )
    dumped = json.dumps(receipt, allow_nan=False)
    assert "secret_prompt" not in dumped
    assert "Infinity" not in dumped
    assert "NaN" not in dumped
    assert receipt["model"] == "gpt-test"
    assert math.isfinite(receipt["captured_at"])


def test_restart_durability_new_store_instance(tmp_path):
    sid = "sess-restart"
    StreamPerformanceReceiptStore(str(tmp_path)).record(
        sid, _receipt(sid, provider_step=4, captured_at=50.0),
    )
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts(sid)
    assert len(rows) == 1
    assert rows[0]["provider_step"] == 4
    assert rows[0]["captured_at"] == 50.0


def test_concurrent_records_are_thread_safe(tmp_path):
    store = StreamPerformanceReceiptStore(str(tmp_path), max_receipts=50)
    sid = "sess-threads"
    errors = []

    def _write(n):
        try:
            store.record(sid, _receipt(sid, provider_step=n, captured_at=float(n)))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    rows = store.list_receipts(sid)
    assert len(rows) == 12
    assert sorted(r["provider_step"] for r in rows) == list(range(12))


def test_delete_session_removes_receipt_file(tmp_path):
    store = StreamPerformanceReceiptStore(str(tmp_path))
    sid = "sess-del"
    store.record(sid, _receipt(sid))
    path = Path(receipt_file_path(str(tmp_path), sid))
    assert path.is_file()
    remove_session_performance_receipts(str(tmp_path), sid)
    assert not path.exists()
    remove_session_performance_receipts(str(tmp_path), sid)


def test_remove_session_transcript_clears_performance_sidecar(tmp_path):
    state_dir = str(tmp_path)
    sid = "sess_clear_perf"
    StreamPerformanceReceiptStore(state_dir).record(sid, _receipt(sid))
    path = Path(receipt_file_path(state_dir, sid))
    assert path.is_file()
    save_transcript(state_dir, sid, {"history": [], "display": [], "job_ids": []})
    remove_session_transcript(sid, state_dir=state_dir)
    assert not path.exists()


def test_handle_session_delete_clears_performance(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = SessionStore(str(tmp_path / "harness_sessions.json"))
    meta = store.create("Doomed", repo=str(tmp_path), workspace_root=str(tmp_path))
    sid = meta["id"]
    StreamPerformanceReceiptStore(str(state_dir)).record(sid, _receipt(sid))
    path = Path(receipt_file_path(str(state_dir), sid))
    assert path.is_file()
    svc = SimpleNamespace(
        sessions=store,
        runners=SimpleNamespace(drop=lambda _sid: None),
        sessions_state_dir=lambda: str(state_dir),
        get_pilot=lambda: SimpleNamespace(load_history=lambda _h: None),
        attach_view=lambda *_a, **_k: None,
        sync_pilot_session_id=lambda: None,
        diag=lambda *_a, **_k: None,
    )
    code, body = handle_session_delete(sid, svc)
    assert code == 200 and body["ok"] is True
    assert not path.exists()


def test_record_helper_does_not_mutate_resp_meta(tmp_path):
    from harness.send_loop_phases import record_provider_stream_receipt

    perf = {PROVIDER_CALL_TOTAL_MS: 11.0, "content_delta_count": 0}
    meta = {STREAM_PERFORMANCE_KEY: dict(perf), "foo": "bar"}
    resp = DriverResponse(text="ok", tokens_out=1, latency_ms=1.0, meta=meta)
    session = SimpleNamespace(
        harness_session_id="sess-meta",
        state_dir=str(tmp_path),
        config=SimpleNamespace(driver="stub/model-x"),
        _current_user_ordinal=lambda: 0,
    )
    record_provider_stream_receipt(session, resp, provider_step=0, provider_attempt=0)
    assert resp.meta is meta
    assert resp.meta[STREAM_PERFORMANCE_KEY] == perf
    assert resp.meta["foo"] == "bar"
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-meta")
    assert len(rows) == 1
    assert rows[0]["stream_performance"][PROVIDER_CALL_TOTAL_MS] == 11.0
    assert not hasattr(session, "_stream_timing")
    assert not hasattr(session, "StreamTimingAccumulator")


def test_receipt_persists_requested_served_identity_and_tokens(tmp_path):
    from harness.send_loop_phases import record_provider_stream_receipt
    from harness.stream_performance_store import sanitize_receipt

    meta = {
        STREAM_PERFORMANCE_KEY: {PROVIDER_CALL_TOTAL_MS: 8.0},
        "requested_model": "claude-fable-5-high",
        "served_model": "Claude Fable 5 High (200K)",
        "identity_status": "verified",
        "token_basis": "provider",
        "cache_read_tokens": 40,
        "cache_write_tokens": 2,
        "secret_prompt": "do-not-persist",
    }
    resp = DriverResponse(
        text="ok",
        tokens_in=120,
        tokens_out=9,
        latency_ms=1.0,
        meta=meta,
    )
    session = SimpleNamespace(
        harness_session_id="sess-id",
        state_dir=str(tmp_path),
        config=SimpleNamespace(driver="cursor-cli:claude-fable-5-high"),
        _current_user_ordinal=lambda: 0,
    )
    record_provider_stream_receipt(session, resp, provider_step=1, provider_attempt=0)
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-id")
    assert len(rows) == 1
    row = rows[0]
    assert row["requested_model"] == "claude-fable-5-high"
    assert row["served_model"] == "Claude Fable 5 High (200K)"
    assert row["identity_status"] == "verified"
    assert row["tokens_in"] == 120
    assert row["tokens_out"] == 9
    assert row["cache_read_tokens"] == 40
    assert row["cache_write_tokens"] == 2
    assert row["token_basis"] == "provider"
    assert "secret_prompt" not in row
    cleaned = sanitize_receipt({
        **row,
        "identity_status": "forged",
        "token_basis": "invented",
        "nested": {"leak": 1},
    })
    assert cleaned is not None
    assert "identity_status" not in cleaned
    assert "token_basis" not in cleaned
    assert "nested" not in cleaned
    assert cleaned["requested_model"] == "claude-fable-5-high"
    assert cleaned["tokens_in"] == 120


def test_record_helper_swallows_sink_failure(tmp_path, monkeypatch):
    from harness.send_loop_phases import record_provider_stream_receipt

    def boom(self, session_id, receipt):
        raise RuntimeError("disk full")

    monkeypatch.setattr(StreamPerformanceReceiptStore, "record", boom)
    resp = DriverResponse(text="ok", tokens_out=1, latency_ms=1.0, meta={})
    session = SimpleNamespace(
        harness_session_id="sess-boom",
        state_dir=str(tmp_path),
        config=SimpleNamespace(driver="stub"),
        _current_user_ordinal=lambda: 0,
    )
    record_provider_stream_receipt(session, resp, provider_step=0, provider_attempt=0)


def _send_session(tmp_path, monkeypatch, pilot, *, sid="sess-send"):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

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


def test_send_records_one_receipt_per_provider_call(monkeypatch, tmp_path):
    class Once:
        name = "once"
        calls = 0

        def chat(self, messages, tools=None, system=None):
            self.calls += 1
            return DriverResponse(
                text='{"say": "hi", "actions": []}',
                tokens_out=2,
                latency_ms=1.0,
                meta={"finish_reason": "stop"},
            )

        def complete(self, prompt, system=None):
            return DriverResponse(text="summary", tokens_out=1, latency_ms=1.0)

    pilot = Once()
    session = _send_session(tmp_path, monkeypatch, pilot)
    events = list(session.send("hello there"))
    assert not any(getattr(e, "kind", None) == "error" for e in events)
    assert pilot.calls == 1
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-send")
    assert len(rows) == 1
    assert rows[0]["provider_step"] == 0
    assert rows[0]["provider_attempt"] == 0
    assert rows[0]["status"] == "success"
    assert rows[0]["schema_version"] == RECEIPT_SCHEMA_VERSION
    assert FIRST_CONTENT_CALLBACK_MS not in rows[0]["stream_performance"]
    assert PROVIDER_OUTPUT_TPS not in rows[0]["stream_performance"]
    assert PROVIDER_CALL_TOTAL_MS in rows[0]["stream_performance"]


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self._ms = int(round(start * 1000.0))

    def __call__(self) -> float:
        return self._ms / 1000.0

    def advance(self, seconds: float) -> None:
        self._ms += int(round(seconds * 1000.0))


def test_overflow_failed_and_retry_are_separate_attempts(monkeypatch, tmp_path):
    from harness.stream_performance import StreamTimingAccumulator

    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    monkeypatch.setattr(
        "harness.send_loop.make_stream_timing_accumulator",
        lambda clock=None: acc,
    )

    class OverflowThenOk:
        name = "overflow-then-ok"
        calls = 0

        def chat(self, messages, tools=None, system=None):
            self.calls += 1
            clock.advance(0.040 if self.calls == 1 else 0.020)
            if self.calls == 1:
                return DriverResponse(
                    text="",
                    error="HTTP 400: maximum context length exceeded",
                    tokens_out=1,
                    latency_ms=4.0,
                )
            return DriverResponse(
                text='{"say": "recovered", "actions": []}',
                tokens_out=2,
                latency_ms=2.0,
                meta={"finish_reason": "stop"},
            )

        def complete(self, prompt, system=None):
            return DriverResponse(text="summary", tokens_out=1, latency_ms=1.0)

    compact_calls = []

    def _spy_compact(force=False, emergency=False):
        compact_calls.append({"force": force, "emergency": emergency})
        if False:
            yield None

    session = _send_session(tmp_path, monkeypatch, OverflowThenOk(), sid="sess-ovf")
    monkeypatch.setattr(session, "_maybe_compact_history", _spy_compact)
    events = list(session.send("audit overflow receipts"))
    assert not any(getattr(e, "kind", None) == "error" for e in events)
    assert session.pilot.calls == 2
    assert any(c.get("emergency") for c in compact_calls)
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-ovf")
    assert len(rows) == 2
    first, retry = rows
    assert first["provider_step"] == retry["provider_step"] == 0
    assert first["provider_attempt"] == 0
    assert retry["provider_attempt"] == 1
    assert first["status"] == "context_overflow"
    assert retry["status"] == "success"
    assert first["stream_performance"].get(PROVIDER_CALL_TOTAL_MS) == 40.0
    assert retry["stream_performance"].get(PROVIDER_CALL_TOTAL_MS) == 20.0
    assert FIRST_CONTENT_CALLBACK_MS not in retry["stream_performance"]
    assert PROVIDER_OUTPUT_TPS not in first["stream_performance"]
    assert PROVIDER_OUTPUT_TPS not in retry["stream_performance"]


def test_multi_step_receipts_have_distinct_steps(monkeypatch, tmp_path):
    (tmp_path / "spy.txt").write_text("hello", encoding="utf-8")

    class TwoStep:
        name = "two-step"
        calls = 0

        def chat(self, messages, tools=None, system=None):
            self.calls += 1
            if self.calls == 1:
                return DriverResponse(
                    text="",
                    tokens_out=5,
                    latency_ms=1.0,
                    meta={
                        "tool_calls": [
                            {
                                "id": "call_spy_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": "spy.txt"}),
                                },
                            }
                        ],
                        "finish_reason": "tool_calls",
                    },
                )
            return DriverResponse(
                text='{"say": "done after tool", "actions": []}',
                tokens_out=5,
                latency_ms=1.0,
                meta={"tool_calls": [], "finish_reason": "stop"},
            )

        def complete(self, prompt, system=None):
            return DriverResponse(text="summary", tokens_out=1, latency_ms=1.0)

    session = _send_session(tmp_path, monkeypatch, TwoStep(), sid="sess-steps")
    list(session.send("read spy.txt then finish"))
    assert session.pilot.calls >= 2
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-steps")
    assert len(rows) == session.pilot.calls
    steps = [r["provider_step"] for r in rows]
    assert steps == list(range(len(steps)))
    assert all(r["provider_attempt"] == 0 for r in rows)


def test_sink_failure_does_not_change_send_hot_path(monkeypatch, tmp_path):
    from harness.stream_performance_store import StreamPerformanceReceiptStore as Store

    def boom(self, session_id, receipt):
        raise RuntimeError("sidecar down")

    monkeypatch.setattr(Store, "record", boom)

    class Once:
        name = "once-boom"
        calls = 0

        def chat(self, messages, tools=None, system=None):
            self.calls += 1
            return DriverResponse(
                text='{"say": "still works", "actions": []}',
                tokens_out=2,
                latency_ms=1.0,
                meta={"finish_reason": "stop"},
            )

        def complete(self, prompt, system=None):
            return DriverResponse(text="summary", tokens_out=1, latency_ms=1.0)

    session = _send_session(tmp_path, monkeypatch, Once(), sid="sess-hot")
    events = list(session.send("please work"))
    kinds = [getattr(e, "kind", None) for e in events]
    assert "error" not in kinds
    assert any(k == "message" for k in kinds)
    assert session.pilot.calls == 1


def test_receipts_never_enter_history_display_or_transcript(monkeypatch, tmp_path):
    class Once:
        name = "once-leak"
        calls = 0

        def chat(self, messages, tools=None, system=None):
            self.calls += 1
            return DriverResponse(
                text='{"say": "visible", "actions": []}',
                tokens_out=2,
                latency_ms=1.0,
                meta={"finish_reason": "stop"},
            )

        def complete(self, prompt, system=None):
            return DriverResponse(text="summary", tokens_out=1, latency_ms=1.0)

    session = _send_session(tmp_path, monkeypatch, Once(), sid="sess-leak")
    list(session.send("do not leak receipts"))
    history_blob = json.dumps(session._history, default=str)
    display_blob = json.dumps(session._display_transcript, default=str)
    outbound = json.dumps(session._messages_for_provider(), default=str)
    assert '"provider_attempt"' not in history_blob
    assert '"provider_step"' not in history_blob
    assert '"captured_at"' not in history_blob
    assert "schema_version" not in history_blob
    assert "schema_version" not in display_blob
    assert "provider_attempt" not in outbound
    exported = session.export_transcript_data()
    export_blob = json.dumps(exported, default=str)
    assert "stream_performance" not in export_blob or STREAM_PERFORMANCE_KEY not in (
        json.dumps(exported.get("history") or [], default=str)
    )
    assert "provider_attempt" not in json.dumps(exported.get("history") or [], default=str)
    assert "provider_attempt" not in json.dumps(exported.get("display") or [], default=str)
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-leak")
    assert len(rows) == 1


def _assert_no_receipt_in_surfaces(session):
    history_blob = json.dumps(session._history, default=str)
    display_blob = json.dumps(session._display_transcript, default=str)
    outbound = json.dumps(session._messages_for_provider(), default=str)
    assert '"provider_attempt"' not in history_blob
    assert '"captured_at"' not in history_blob
    assert "schema_version" not in history_blob
    assert "schema_version" not in display_blob
    assert "provider_attempt" not in outbound
    exported = session.export_transcript_data()
    assert "provider_attempt" not in json.dumps(exported.get("history") or [], default=str)
    assert "provider_attempt" not in json.dumps(exported.get("display") or [], default=str)


def test_malformed_tokens_out_records_one_receipt_and_hot_path_survives(monkeypatch, tmp_path):
    class BadTokens:
        name = "bad-tokens"
        calls = 0

        def chat(self, messages, tools=None, system=None):
            self.calls += 1
            return DriverResponse(
                text='{"say": "still honest", "actions": []}',
                tokens_out="nope",
                latency_ms=1.0,
                meta={"finish_reason": "stop"},
            )

        def complete(self, prompt, system=None):
            return DriverResponse(text="summary", tokens_out=1, latency_ms=1.0)

    session = _send_session(tmp_path, monkeypatch, BadTokens(), sid="sess-bad-tok")
    events = list(session.send("malformed tokens_out"))
    assert not any(getattr(e, "kind", None) == "error" for e in events)
    assert session.pilot.calls == 1
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-bad-tok")
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["provider_step"] == 0
    assert rows[0]["provider_attempt"] == 0
    assert PROVIDER_CALL_TOTAL_MS in rows[0]["stream_performance"]
    _assert_no_receipt_in_surfaces(session)


def test_account_provider_attempt_records_before_meter_raise(tmp_path, monkeypatch):
    from harness.send_loop_phases import account_provider_attempt

    def boom(session, resp, prompt):
        raise TypeError("tokens_out")

    monkeypatch.setattr("harness.send_loop_phases.meter_pilot_step", boom)
    resp = DriverResponse(
        text="ok",
        tokens_out="nope",
        latency_ms=1.0,
        meta={
            STREAM_PERFORMANCE_KEY: {PROVIDER_CALL_TOTAL_MS: 9.0},
            "finish_reason": "stop",
        },
    )
    session = SimpleNamespace(
        harness_session_id="sess-order",
        state_dir=str(tmp_path),
        config=SimpleNamespace(driver="stub/model-x"),
        _current_user_ordinal=lambda: 0,
    )
    with pytest.raises(TypeError):
        account_provider_attempt(session, resp, "prompt", provider_step=2, provider_attempt=1)
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-order")
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["provider_step"] == 2
    assert rows[0]["provider_attempt"] == 1
    assert rows[0]["stream_performance"][PROVIDER_CALL_TOTAL_MS] == 9.0
    assert resp.meta[STREAM_PERFORMANCE_KEY][PROVIDER_CALL_TOTAL_MS] == 9.0


def test_sync_chat_raise_records_one_error_receipt(monkeypatch, tmp_path):
    class BoomChat:
        name = "boom-chat"
        calls = 0

        def chat(self, messages, tools=None, system=None):
            self.calls += 1
            raise RuntimeError("sync chat failed")

        def complete(self, prompt, system=None):
            return DriverResponse(text="summary", tokens_out=1, latency_ms=1.0)

    session = _send_session(tmp_path, monkeypatch, BoomChat(), sid="sess-chat-raise")
    events = list(session.send("sync raise"))
    assert any(getattr(e, "kind", None) == "error" for e in events)
    assert session.pilot.calls == 1
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-chat-raise")
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["provider_step"] == 0
    assert rows[0]["provider_attempt"] == 0
    _assert_no_receipt_in_surfaces(session)


def test_complete_raise_records_one_error_receipt(monkeypatch, tmp_path):
    class BoomComplete:
        name = "boom-complete"
        calls = 0

        def complete(self, prompt, system=None):
            self.calls += 1
            raise RuntimeError("complete failed")

    session = _send_session(tmp_path, monkeypatch, BoomComplete(), sid="sess-complete-raise")
    events = list(session.send("complete raise"))
    assert any(getattr(e, "kind", None) == "error" for e in events)
    assert session.pilot.calls == 1
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-complete-raise")
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["provider_attempt"] == 0
    _assert_no_receipt_in_surfaces(session)


def test_chat_stream_raise_records_one_error_receipt(monkeypatch, tmp_path):
    class BoomStream:
        name = "boom-stream"
        supports_streaming = True
        calls = 0

        def chat(self, messages, tools=None, system=None):
            raise AssertionError("chat() must not run when streaming")

        def chat_stream(self, messages, **kwargs):
            self.calls += 1
            on_delta = kwargs.get("on_delta")
            if callable(on_delta):
                on_delta('{"say": "partial"}')
            raise RuntimeError("stream producer failed")

        def complete(self, prompt, system=None):
            return DriverResponse(text="summary", tokens_out=1, latency_ms=1.0)

    session = _send_session(tmp_path, monkeypatch, BoomStream(), sid="sess-stream-raise")
    events = list(session.send("stream raise"))
    assert any(getattr(e, "kind", None) == "error" for e in events)
    assert session.pilot.calls == 1
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-stream-raise")
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["provider_attempt"] == 0
    perf = rows[0]["stream_performance"]
    assert perf.get("content_delta_count", 0) >= 0
    _assert_no_receipt_in_surfaces(session)


def test_returned_generic_error_does_not_double_write_receipt(monkeypatch, tmp_path):
    class GenericErr:
        name = "generic-err"
        calls = 0

        def chat(self, messages, tools=None, system=None):
            self.calls += 1
            return DriverResponse(
                text="",
                error="HTTP 500: internal server error",
                tokens_out=1,
                latency_ms=1.0,
            )

        def complete(self, prompt, system=None):
            return DriverResponse(text="summary", tokens_out=1, latency_ms=1.0)

    session = _send_session(tmp_path, monkeypatch, GenericErr(), sid="sess-generic-err")
    events = list(session.send("returned error"))
    assert any(getattr(e, "kind", None) == "error" for e in events)
    assert session.pilot.calls == 1
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-generic-err")
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["provider_attempt"] == 0
    _assert_no_receipt_in_surfaces(session)


def test_persistent_overflow_records_both_attempts(monkeypatch, tmp_path):
    class DoubleOverflow:
        name = "double-overflow"
        calls = 0

        def chat(self, messages, tools=None, system=None):
            self.calls += 1
            return DriverResponse(
                text="",
                error="HTTP 400: maximum context length exceeded",
                tokens_out=1,
                latency_ms=1.0,
            )

        def complete(self, prompt, system=None):
            return DriverResponse(text="summary", tokens_out=1, latency_ms=1.0)

    compact_calls = []

    def _spy_compact(force=False, emergency=False):
        compact_calls.append({"force": force, "emergency": emergency})
        if False:
            yield None

    session = _send_session(tmp_path, monkeypatch, DoubleOverflow(), sid="sess-ovf-persist")
    monkeypatch.setattr(session, "_maybe_compact_history", _spy_compact)
    events = list(session.send("overflow twice"))
    assert any(getattr(e, "kind", None) == "error" for e in events)
    assert session.pilot.calls == 2
    assert any(c.get("emergency") for c in compact_calls)
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-ovf-persist")
    assert len(rows) == 2
    assert [r["provider_attempt"] for r in rows] == [0, 1]
    assert all(r["status"] == "context_overflow" for r in rows)
    assert all(r["provider_step"] == 0 for r in rows)
    _assert_no_receipt_in_surfaces(session)


def test_stream_success_records_one_receipt(monkeypatch, tmp_path):
    class StreamOk:
        name = "stream-ok"
        supports_streaming = True
        calls = 0

        def chat(self, messages, tools=None, system=None):
            raise AssertionError("chat() must not run when streaming")

        def chat_stream(self, messages, **kwargs):
            self.calls += 1
            on_delta = kwargs.get("on_delta")
            if callable(on_delta):
                on_delta('{"say": "streamed hi", "actions": []}')
            return DriverResponse(
                text='{"say": "streamed hi", "actions": []}',
                tokens_out=4,
                latency_ms=2.0,
                meta={"finish_reason": "stop", "stream_started": True, "stream_terminal": "stop"},
            )

        def complete(self, prompt, system=None):
            return DriverResponse(text="summary", tokens_out=1, latency_ms=1.0)

    session = _send_session(tmp_path, monkeypatch, StreamOk(), sid="sess-stream-ok")
    events = list(session.send("stream success"))
    assert not any(getattr(e, "kind", None) == "error" for e in events)
    assert session.pilot.calls == 1
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-stream-ok")
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["provider_attempt"] == 0
    assert PROVIDER_CALL_TOTAL_MS in rows[0]["stream_performance"]
    _assert_no_receipt_in_surfaces(session)


def test_complete_success_records_one_receipt(monkeypatch, tmp_path):
    class CompleteOnly:
        name = "complete-ok"
        calls = 0

        def complete(self, prompt, system=None):
            self.calls += 1
            return DriverResponse(
                text='{"say": "from complete", "actions": []}',
                tokens_out=2,
                latency_ms=1.0,
                meta={"finish_reason": "stop"},
            )

    session = _send_session(tmp_path, monkeypatch, CompleteOnly(), sid="sess-complete-ok")
    events = list(session.send("complete success"))
    assert not any(getattr(e, "kind", None) == "error" for e in events)
    assert session.pilot.calls == 1
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-complete-ok")
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert FIRST_CONTENT_CALLBACK_MS not in rows[0]["stream_performance"]
    assert PROVIDER_CALL_TOTAL_MS in rows[0]["stream_performance"]
    _assert_no_receipt_in_surfaces(session)


def test_receipt_turn_index_and_user_ordinal(monkeypatch, tmp_path):
    class Once:
        name = "ordinals"
        calls = 0

        def chat(self, messages, tools=None, system=None):
            self.calls += 1
            return DriverResponse(
                text='{"say": "turn", "actions": []}',
                tokens_out=2,
                latency_ms=1.0,
                meta={"finish_reason": "stop"},
            )

        def complete(self, prompt, system=None):
            return DriverResponse(text="summary", tokens_out=1, latency_ms=1.0)

    session = _send_session(tmp_path, monkeypatch, Once(), sid="sess-ordinal")
    list(session.send("first"))
    list(session.send("second"))
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-ordinal")
    assert len(rows) == 2
    assert rows[0]["user_ordinal"] == 0
    assert rows[0]["turn_index"] == 1
    assert rows[1]["user_ordinal"] == 1
    assert rows[1]["turn_index"] == 2
    _assert_no_receipt_in_surfaces(session)


def test_missing_session_id_skips_receipt_write(monkeypatch, tmp_path):
    from harness.send_loop_phases import record_provider_stream_receipt

    resp = DriverResponse(text="ok", tokens_out=1, latency_ms=1.0, meta={})
    session = SimpleNamespace(
        harness_session_id="",
        state_dir=str(tmp_path),
        config=SimpleNamespace(driver="stub"),
        _current_user_ordinal=lambda: 0,
    )
    record_provider_stream_receipt(session, resp, provider_step=0, provider_attempt=0)
    perf_dir = tmp_path / "stream_performance"
    assert not perf_dir.exists() or list(perf_dir.glob("*.json")) == []

    class Once:
        name = "no-sid"
        calls = 0

        def chat(self, messages, tools=None, system=None):
            self.calls += 1
            return DriverResponse(
                text='{"say": "ok", "actions": []}',
                tokens_out=2,
                latency_ms=1.0,
                meta={"finish_reason": "stop"},
            )

        def complete(self, prompt, system=None):
            return DriverResponse(text="summary", tokens_out=1, latency_ms=1.0)

    live = _send_session(tmp_path, monkeypatch, Once(), sid="")
    events = list(live.send("no session id"))
    assert not any(getattr(e, "kind", None) == "error" for e in events)
    assert live.pilot.calls == 1
    assert StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("") == []
    assert not perf_dir.exists() or list(perf_dir.glob("*.json")) == []
    _assert_no_receipt_in_surfaces(live)


def test_pre_invocation_failure_does_not_write_receipt(monkeypatch, tmp_path):
    class NeverCalled:
        name = "never"
        calls = 0

        def chat(self, messages, tools=None, system=None):
            self.calls += 1
            return DriverResponse(
                text='{"say": "nope", "actions": []}',
                tokens_out=1,
                latency_ms=1.0,
            )

        def complete(self, prompt, system=None):
            return DriverResponse(text="summary", tokens_out=1, latency_ms=1.0)

    session = _send_session(tmp_path, monkeypatch, NeverCalled(), sid="sess-pre")

    def boom():
        raise RuntimeError("tools schema failed")

    session._build_visible_tools_schema = boom
    events = list(session.send("before invoke"))
    assert any(getattr(e, "kind", None) == "error" for e in events)
    assert session.pilot.calls == 0
    assert StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-pre") == []
    _assert_no_receipt_in_surfaces(session)


def test_api_unknown_and_missing_session(tmp_path):
    sessions = SimpleNamespace(active="sess-a", rows=lambda: [{"id": "sess-a"}])
    svc = _perf_svc(state_dir=tmp_path, sessions=sessions)
    code, payload = get_session_performance({"session_id": [""]}, svc)
    assert code == 200
    assert payload["ok"] is True
    assert payload["session_id"] == "sess-a"
    assert payload["receipts"] == []
    assert payload["count"] == 0
    code, payload = get_session_performance({"session_id": ["nope"]}, svc)
    assert code == 404
    code, payload = get_session_performance(
        {"session_id": ["../../etc/passwd"]}, svc,
    )
    assert code == 404
    empty = _perf_svc(state_dir=tmp_path, sessions=SimpleNamespace(active=None, rows=lambda: []))
    code, payload = get_session_performance({}, empty)
    assert code == 400


def test_api_limit_and_known_session_scope(tmp_path):
    sessions = SimpleNamespace(
        active="sess-a",
        rows=lambda: [{"id": "sess-a"}, {"id": "sess-b"}],
    )
    store = StreamPerformanceReceiptStore(str(tmp_path))
    for i in range(4):
        store.record("sess-a", _receipt("sess-a", provider_step=i, captured_at=10.0 + i))
    store.record("sess-b", _receipt("sess-b", provider_step=99, captured_at=1.0))
    svc = _perf_svc(state_dir=tmp_path, sessions=sessions)
    code, payload = get_session_performance(
        {"session_id": ["sess-a"], "limit": ["2"]}, svc,
    )
    assert code == 200
    assert payload["ok"] is True
    assert payload["count"] == 2
    assert [r["provider_step"] for r in payload["receipts"]] == [2, 3]
    code, payload = get_session_performance(
        {"session_id": ["sess-b"], "limit": ["9999"]}, svc,
    )
    assert code == 200
    assert payload["count"] == 1
    assert payload["receipts"][0]["provider_step"] == 99
    code, payload = get_session_performance(
        {"session_id": ["sess-a"], "limit": ["-3"]}, svc,
    )
    assert code == 200
    assert payload["count"] == 1
    code, payload = get_session_performance(
        {"session_id": ["sess-a"], "limit": ["nope"]}, svc,
    )
    assert code == 200
    assert payload["count"] == 4
    assert payload["count"] <= MAX_RECEIPTS_PER_SESSION


def test_api_route_is_guarded_and_state_is_not_overloaded():
    import harness.api.static as static_api
    import harness.server as srv
    from harness.api.session_control import get_session_state

    srv._GET_ROUTES = None
    routes = srv._get_routes()
    assert "/api/session/performance" in routes
    assert "/api/session/performance" not in static_api.PUBLIC_GET_PATHS
    sessions = SimpleNamespace(active="sess-a", rows=lambda: [{"id": "sess-a"}])
    svc = _perf_svc(state_dir=os.path.join("unused"), sessions=sessions)
    svc.get_pilot = lambda: SimpleNamespace(
        state=lambda: "idle",
        has_pending_swarms=lambda: False,
        session_goal_dict=lambda: {},
        harness_session_id="sess-a",
    )
    code, state = get_session_state({"session_id": ["sess-a"]}, svc)
    assert code == 200
    assert "receipts" not in state


def test_api_http_auth_required(tmp_path):
    import json as json_mod
    import threading
    import time
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    import harness.server as srv

    srv._GET_ROUTES = None
    srv._POST_JSON_ROUTES = None
    srv._cfg.state_dir = str(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    url = f"http://127.0.0.1:{port}/api/session/performance?session_id=sess-a"
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(url, timeout=5)
        assert ei.value.code == 403
        req = urllib.request.Request(
            url, headers={"X-Harness-Token": srv._TOKEN}, method="GET",
        )
        # Authed: unknown session is a product 404, not an auth miss.
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code in (200, 400, 404)
            if exc.code != 200:
                body = json_mod.loads(exc.read())
                assert "token" not in str(body.get("error") or "").lower()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_never_writes_live_pmharness_under_pytest(tmp_path, monkeypatch):
    live = Path.home() / ".pmharness" / "stream_performance"
    store = StreamPerformanceReceiptStore(str(Path.home() / ".pmharness"))
    store.record("pytest-must-not-write", _receipt("pytest-must-not-write"))
    # If the live tree already had a file, we still must not create this sid.
    target = live / "pytest-must-not-write.json"
    assert not target.exists()


def test_never_reads_or_deletes_live_pmharness_under_pytest():
    live_root = Path.home() / ".pmharness"
    live_perf = live_root / "stream_performance"
    sid = "pytest-must-not-touch-read"
    target = live_perf / f"{sid}.json"
    existed_root = live_root.exists()
    existed_perf = live_perf.exists()
    existed_file = target.exists()
    prior = target.read_bytes() if existed_file else None
    store = StreamPerformanceReceiptStore(str(live_root))
    assert store.list_receipts(sid) == []
    store.delete_session(sid)
    assert target.exists() == existed_file
    if existed_file:
        assert target.read_bytes() == prior
    if not existed_perf:
        assert not live_perf.exists()
    if not existed_root:
        assert not live_root.exists()


def test_blank_state_dir_never_uses_tempdir(tmp_path, monkeypatch):
    sentinel = tmp_path / "fake-tmp"
    sentinel.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(sentinel))
    for raw in ("", None, "   "):
        store = StreamPerformanceReceiptStore(raw)
        store.record("sess", _receipt("sess"))
        assert store.list_receipts("sess") == []
        store.delete_session("sess")
    assert list(sentinel.iterdir()) == []
    sessions = SimpleNamespace(active="sess-a", rows=lambda: [{"id": "sess-a"}])
    svc = _perf_svc(state_dir="", sessions=sessions)
    code, payload = get_session_performance({"session_id": ["sess-a"]}, svc)
    assert code == 200
    assert payload["receipts"] == []
    assert payload["count"] == 0
    assert list(sentinel.iterdir()) == []


def test_symlink_dir_write_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "secret.txt"
    marker.write_text("do-not-touch", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    _make_symlink(outside, state / "stream_performance")
    store = StreamPerformanceReceiptStore(str(state))
    store.record("sess", _receipt("sess", provider_step=1))
    assert store.list_receipts("sess") == []
    assert not (outside / "sess.json").exists()
    assert marker.read_text(encoding="utf-8") == "do-not-touch"
    assert list(outside.glob("stream-perf-*.json")) == []


def test_symlink_file_read_delete_isolation(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secrets.json"
    target.write_text(
        json.dumps({
            "schema_version": 1,
            "session_id": "sess",
            "receipts": [{
                "session_id": "sess",
                "provider_step": 9,
                "stream_performance": {"api_key": "sk-secret"},
            }],
        }),
        encoding="utf-8",
    )
    prior = target.read_bytes()
    state = tmp_path / "state"
    perf = state / "stream_performance"
    perf.mkdir(parents=True)
    link = perf / "sess.json"
    _make_symlink(target, link)
    store = StreamPerformanceReceiptStore(str(state))
    assert store.list_receipts("sess") == []
    store.delete_session("sess")
    assert target.exists()
    assert target.read_bytes() == prior
    assert "sk-secret" not in json.dumps(store.list_receipts("sess"))


def test_lexical_alias_multiple_stores_do_not_lose_receipts(tmp_path):
    real = tmp_path / "real_state"
    real.mkdir()
    alias = tmp_path / "alias_state"
    _make_symlink(real, alias)
    store_a = StreamPerformanceReceiptStore(str(real))
    store_b = StreamPerformanceReceiptStore(str(alias))
    errors = []

    def _write(store, n):
        try:
            store.record("sess", _receipt("sess", provider_step=n, captured_at=float(n)))
        except Exception as exc:
            errors.append(exc)

    threads = []
    for i in range(12):
        store = store_a if i % 2 == 0 else store_b
        threads.append(threading.Thread(target=_write, args=(store, i)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    rows = store_a.list_receipts("sess")
    assert len(rows) == 12
    assert sorted(r["provider_step"] for r in rows) == list(range(12))
    assert len(store_b.list_receipts("sess")) == 12


def test_multiprocess_rmw_preserves_all_receipts(tmp_path):
    sid = "sess-mp"
    state = str(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    n = 4
    procs = [
        ctx.Process(target=_mp_record_one, args=(state, sid, i))
        for i in range(n)
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=20)
        assert proc.exitcode == 0
    rows = StreamPerformanceReceiptStore(state).list_receipts(sid)
    assert len(rows) == n
    assert sorted(r["provider_step"] for r in rows) == list(range(n))


def test_deep_json_and_recursion_error_fail_soft_then_repair(tmp_path, monkeypatch):
    store = StreamPerformanceReceiptStore(str(tmp_path))
    sid = "sess-deep"
    path = Path(receipt_file_path(str(tmp_path), sid))
    path.parent.mkdir(parents=True, exist_ok=True)
    depth = 8000
    path.write_text("{" + '"k":{' * depth + "}" * depth + "}", encoding="utf-8")
    assert store.list_receipts(sid) == []
    store.record(sid, _receipt(sid, provider_step=1))
    assert store.list_receipts(sid)[0]["provider_step"] == 1

    def _boom(*_a, **_k):
        raise RecursionError("too deep")

    monkeypatch.setattr(json, "loads", _boom)
    assert store.list_receipts(sid) == []
    monkeypatch.undo()
    store.record(sid, _receipt(sid, provider_step=2))
    assert [r["provider_step"] for r in store.list_receipts(sid)] == [1, 2]


def test_oversized_file_byte_capped_and_next_write_repairs(tmp_path, monkeypatch):
    store = StreamPerformanceReceiptStore(str(tmp_path))
    sid = "sess-big"
    path = Path(receipt_file_path(str(tmp_path), sid))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "session_id": sid,
        "receipts": [_receipt(sid, provider_step=99)],
    }
    path.write_text(json.dumps(payload) + (" " * (3 * 1024 * 1024)), encoding="utf-8")
    monkeypatch.setattr(
        os.path,
        "getsize",
        lambda p: 16 if os.path.basename(str(p)) == f"{sid}.json" else os.path.getsize(p),
    )
    assert store.list_receipts(sid) == []
    monkeypatch.undo()
    store.record(sid, _receipt(sid, provider_step=7))
    rows = store.list_receipts(sid)
    assert len(rows) == 1
    assert rows[0]["provider_step"] == 7


def test_known_key_dict_secret_rejected():
    copied = copy_stream_performance({
        PROVIDER_CALL_TOTAL_MS: {"api_key": "sk-secret", "nested": ["leak"]},
        "content_delta_count": {"secret": "nope"},
        "throughput_basis": {"token": "leak"},
        FIRST_CONTENT_CALLBACK_MS: ["sk-secret"],
        PROVIDER_OUTPUT_TPS: {"password": "hunter2"},
    })
    blob = json.dumps(copied)
    assert copied == {}
    assert "sk-secret" not in blob
    assert "hunter2" not in blob
    assert "secret" not in blob
    assert "leak" not in blob
    assert "password" not in blob


def test_copy_stream_performance_rejects_string_numbers_and_unlisted_basis():
    copied = copy_stream_performance({
        PROVIDER_CALL_TOTAL_MS: "12.5",
        FIRST_CONTENT_CALLBACK_MS: "80",
        PROVIDER_OUTPUT_TPS: "30.0",
        "decode_window_ms": "400",
        "content_delta_count": "3",
        THROUGHPUT_BASIS_KEY: "tokens/sec",
        "pre_request_total_ms": True,
    })
    assert copied == {}
    accepted = copy_stream_performance({
        PROVIDER_CALL_TOTAL_MS: 12.5,
        "content_delta_count": 3,
        THROUGHPUT_BASIS_KEY: THROUGHPUT_BASIS,
    })
    assert accepted[PROVIDER_CALL_TOTAL_MS] == 12.5
    assert accepted["content_delta_count"] == 3
    assert accepted[THROUGHPUT_BASIS_KEY] == THROUGHPUT_BASIS
    rejected_counts = copy_stream_performance({
        "content_delta_count": -1,
        PROVIDER_CALL_TOTAL_MS: 1.0,
    })
    assert "content_delta_count" not in rejected_counts
    huge = copy_stream_performance({"content_delta_count": 1_000_000_001})
    assert huge == {}


def test_default_cap_keeps_newest_200(tmp_path):
    store = StreamPerformanceReceiptStore(str(tmp_path))
    sid = "sess-cap"
    assert store.max_receipts == MAX_RECEIPTS_PER_SESSION
    for i in range(MAX_RECEIPTS_PER_SESSION + 5):
        store.record(sid, _receipt(sid, provider_step=i, captured_at=float(i)))
    rows = store.list_receipts(sid)
    assert len(rows) == MAX_RECEIPTS_PER_SESSION
    assert rows[0]["provider_step"] == 5
    assert rows[-1]["provider_step"] == MAX_RECEIPTS_PER_SESSION + 4


def test_api_workspace_cross_session_denied(tmp_path):
    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    ws_a.mkdir()
    ws_b.mkdir()
    row_a = {"id": "sess-a", "workspace_root": str(ws_a)}
    row_b = {"id": "sess-b", "workspace_root": str(ws_b)}
    sessions = SimpleNamespace(active="sess-a", rows=lambda: [row_a, row_b])
    store = StreamPerformanceReceiptStore(str(tmp_path))
    store.record("sess-a", _receipt("sess-a", provider_step=1))
    store.record("sess-b", _receipt("sess-b", provider_step=2))
    svc = _perf_svc(state_dir=tmp_path, sessions=sessions, repo=str(ws_a))
    code, payload = get_session_performance({"session_id": ["sess-b"]}, svc)
    assert code == 403
    assert payload["error"] == "session not visible in active workspace"
    code, payload = get_session_performance({"session_id": ["sess-a"]}, svc)
    assert code == 200
    assert payload["count"] == 1
    assert payload["receipts"][0]["provider_step"] == 1
    current = _perf_svc(
        state_dir=tmp_path,
        sessions=sessions,
        repo=str(ws_a),
        pilot=SimpleNamespace(harness_session_id="sess-b"),
    )
    code, payload = get_session_performance({"session_id": ["sess-b"]}, current)
    assert code == 200
    assert payload["count"] == 1


def test_receipt_schema_v2_fields_persist_and_old_v1_reads(tmp_path):
    from harness.send_loop_phases import record_provider_stream_receipt
    from harness.stream_performance_store import RECEIPT_SCHEMA_VERSION_V1

    meta = {
        STREAM_PERFORMANCE_KEY: {PROVIDER_CALL_TOTAL_MS: 9.0},
        "finish_reason": "stop",
        "stream_terminal": "stop",
        "stream_started": True,
        "incomplete_reason": "",
        "api_mode": "chat_completions",
        "malformed_sse_chunks": 2,
        "max_tokens": 1500,
        "requested_model": "gpt-test",
        "served_model": "gpt-test",
    }
    resp = DriverResponse(
        text="ok", tokens_in=14, tokens_out=3, latency_ms=1.0, meta=meta,
    )
    session = SimpleNamespace(
        harness_session_id="sess-v2",
        state_dir=str(tmp_path),
        config=SimpleNamespace(driver="openai/gpt-test"),
        _current_user_ordinal=lambda: 0,
        pilot=SimpleNamespace(max_tokens=1500),
    )
    record_provider_stream_receipt(session, resp, provider_step=3, provider_attempt=1)
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-v2")
    assert len(rows) == 1
    row = rows[0]
    assert row["schema_version"] == RECEIPT_SCHEMA_VERSION
    assert row["terminal_cause"] == "natural"
    assert row["finish_reason"] == "stop"
    assert row["stream_terminal"] == "stop"
    assert row["stream_started"] is True
    assert row["malformed_chunk_count"] == 2
    assert row["requested_output_cap"] == 1500
    assert row["api_mode"] == "chat_completions"
    assert row["provider_step"] == 3
    assert row["provider_attempt"] == 1
    assert row["requested_model"] == "gpt-test"
    assert row["served_model"] == "gpt-test"
    assert row["assistant_done_emitted"] is False

    StreamPerformanceReceiptStore(str(tmp_path)).patch_latest_receipt(
        "sess-v2", assistant_done_emitted=True, terminal_cause="natural",
    )
    patched = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-v2")[0]
    assert patched["assistant_done_emitted"] is True

    old_path = Path(receipt_file_path(str(tmp_path), "sess-old"))
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_text(json.dumps({
        "schema_version": RECEIPT_SCHEMA_VERSION_V1,
        "session_id": "sess-old",
        "receipts": [{
            "schema_version": RECEIPT_SCHEMA_VERSION_V1,
            "session_id": "sess-old",
            "turn_index": 1,
            "provider_step": 4,
            "provider_attempt": 0,
            "driver": "openai/gpt-test",
            "model": "gpt-test",
            "status": "success",
            "captured_at": 1000.0,
            "stream_performance": {"content_delta_count": 1},
        }],
    }), encoding="utf-8")
    old_rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-old")
    assert len(old_rows) == 1
    assert old_rows[0]["session_id"] == "sess-old"
    assert old_rows[0]["provider_step"] == 4
    assert old_rows[0]["status"] == "success"
    assert old_rows[0]["stream_performance"]["content_delta_count"] == 1
    assert "terminal_cause" not in old_rows[0] or old_rows[0].get("terminal_cause") in (
        None, "",
    )


def test_usage_percent_does_not_become_receipt_terminal_cause(tmp_path):
    from harness.send_loop_phases import record_provider_stream_receipt

    resp = DriverResponse(
        text="ok",
        tokens_in=14000,
        tokens_out=20,
        latency_ms=1.0,
        meta={
            STREAM_PERFORMANCE_KEY: {PROVIDER_CALL_TOTAL_MS: 4.0},
            "finish_reason": "stop",
            "raw_usage": {"prompt_tokens": 14000, "completion_tokens": 20},
            "context_used_pct": 14,
        },
    )
    session = SimpleNamespace(
        harness_session_id="sess-ctx",
        state_dir=str(tmp_path),
        config=SimpleNamespace(driver="openai/gpt-test"),
        _current_user_ordinal=lambda: 0,
    )
    record_provider_stream_receipt(session, resp, provider_step=0, provider_attempt=0)
    row = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-ctx")[0]
    assert row["terminal_cause"] == "natural"
    assert row["finish_reason"] == "stop"
    assert row["tokens_in"] == 14000


def test_receipt_status_follows_terminal_cause_not_only_error(tmp_path):
    from harness.send_loop_phases import (
        classify_provider_receipt_status,
        record_provider_stream_receipt,
    )

    length = DriverResponse(
        text="cut",
        tokens_out=4,
        latency_ms=1.0,
        meta={"finish_reason": "length", "stream_terminal": "length"},
    )
    assert classify_provider_receipt_status(length) == "error"
    session = SimpleNamespace(
        harness_session_id="sess-status",
        state_dir=str(tmp_path),
        config=SimpleNamespace(driver="openai/gpt-test"),
        _current_user_ordinal=lambda: 0,
    )
    record_provider_stream_receipt(session, length, provider_step=0, provider_attempt=0)
    row = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-status")[0]
    assert row["status"] == "error"
    assert row["terminal_cause"] == "length"
    assert row["finish_reason"] == "length"

    truncated = DriverResponse(
        text="",
        tokens_out=2,
        latency_ms=1.0,
        meta={
            "finish_reason": "tool_calls",
            "stream_terminal": "tool_calls",
            "tool_calls": [],
            "incomplete_tool_calls": [{
                "id": "c1",
                "function": {"name": "read_file", "arguments": '{"path":'},
            }],
        },
    )
    assert classify_provider_receipt_status(truncated) == "error"
    record_provider_stream_receipt(
        SimpleNamespace(
            harness_session_id="sess-trunc",
            state_dir=str(tmp_path),
            config=SimpleNamespace(driver="openai/gpt-test"),
            _current_user_ordinal=lambda: 0,
        ),
        truncated,
        provider_step=0,
        provider_attempt=0,
    )
    trunc_row = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-trunc")[0]
    assert trunc_row["status"] == "error"
    assert trunc_row["terminal_cause"] == "incomplete"

    unspecified = DriverResponse(text="sync only", tokens_out=1, latency_ms=1.0, meta={})
    assert classify_provider_receipt_status(unspecified) == "error"
