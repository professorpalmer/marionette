"""Hermetic tests for optional observability side export."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.history_compaction_journal import record_history_compaction
from harness.observability_export import (
    _reset_for_tests,
    build_envelope,
    emit_event,
    export_enabled,
    export_routing_savings,
    export_tool_output_savings,
)
from harness.tool_output_savings import ToolOutputSavingsLedger, get_ledger, try_record


def _wait_for_file(path, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.isfile(path):
            return True
        time.sleep(0.02)
    return os.path.isfile(path)


def _wait_for_http(count_holder, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if count_holder["n"] > 0:
            return True
        time.sleep(0.02)
    return count_holder["n"] > 0


@pytest.fixture(autouse=True)
def _reset_observability():
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_export_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("HARNESS_OBSERVABILITY_EXPORT", raising=False)
    out = tmp_path / "obs.jsonl"
    monkeypatch.setenv("HARNESS_OBSERVABILITY_EXPORT_FILE", str(out))
    assert not export_enabled()
    emit_event("harness.test", {"x": 1})
    export_tool_output_savings(
        session_id="s",
        tool_call_id="tc",
        original_chars=1000,
        compact_chars=100,
        tokens_saved=225,
    )
    time.sleep(0.05)
    assert not out.exists()


def test_export_enabled_requires_destination(monkeypatch):
    monkeypatch.setenv("HARNESS_OBSERVABILITY_EXPORT", "1")
    monkeypatch.delenv("HARNESS_OBSERVABILITY_EXPORT_FILE", raising=False)
    monkeypatch.delenv("HARNESS_OBSERVABILITY_EXPORT_ENDPOINT", raising=False)
    assert not export_enabled()


def test_envelope_json_schema():
    env = build_envelope(
        "harness.tool_output.savings",
        {
            "session_id": "sess-1",
            "tokens_saved": 42,
            "basis": "measured",
        },
    )
    assert env["name"] == "harness.tool_output.savings"
    assert env["kind"] == "event"
    assert env["resource"]["attributes"]["service.name"] == "marionette-harness"
    assert env["instrumentation_scope"]["name"] == "harness.observability_export"
    assert env["time_unix_nano"].isdigit()
    assert env["attributes"]["session_id"] == "sess-1"
    assert env["attributes"]["tokens_saved"] == 42
    assert env["attributes"]["basis"] == "measured"


def test_file_export_success(monkeypatch, tmp_path):
    out = tmp_path / "events.jsonl"
    monkeypatch.setenv("HARNESS_OBSERVABILITY_EXPORT", "1")
    monkeypatch.setenv("HARNESS_OBSERVABILITY_EXPORT_FILE", str(out))
    emit_event("harness.test.event", {"alpha": "beta"})
    assert _wait_for_file(out)
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["name"] == "harness.test.event"
    assert rec["attributes"]["alpha"] == "beta"
    assert b"\r\n" not in out.read_bytes()


def test_http_export_success(monkeypatch):
    received = {"n": 0, "body": None}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            received["body"] = self.rfile.read(length)
            received["n"] += 1
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("HARNESS_OBSERVABILITY_EXPORT", "1")
        monkeypatch.setenv(
            "HARNESS_OBSERVABILITY_EXPORT_ENDPOINT",
            f"http://127.0.0.1:{port}/v1/logs",
        )
        emit_event("harness.http.event", {"job_id": "local-1"})
        assert _wait_for_http(received)
        payload = json.loads(received["body"].decode("utf-8"))
        assert payload["name"] == "harness.http.event"
        assert payload["attributes"]["job_id"] == "local-1"
    finally:
        httpd.shutdown()


def test_http_failure_never_raises(monkeypatch):
    monkeypatch.setenv("HARNESS_OBSERVABILITY_EXPORT", "1")
    monkeypatch.setenv(
        "HARNESS_OBSERVABILITY_EXPORT_ENDPOINT",
        "http://127.0.0.1:1/unreachable",
    )
    monkeypatch.setenv("HARNESS_OBSERVABILITY_EXPORT_TIMEOUT_MS", "20")
    export_routing_savings(
        job_id="j1",
        routing_saved_usd=0.05,
        routing_savings_basis="estimated",
    )


def test_redaction_and_attribute_caps(monkeypatch):
    monkeypatch.setenv("HARNESS_OBSERVABILITY_EXPORT_MAX_ATTRS", "4")
    monkeypatch.setenv("HARNESS_OBSERVABILITY_EXPORT_MAX_ATTR_LEN", "8")
    env = build_envelope(
        "harness.redact",
        {
            "ok": "short",
            "api_key": "sk-abcdefghijklmnop",
            "authorization": "Bearer secret-token-value",
            "long_text": "x" * 40,
            "extra": "drop-me",
        },
    )
    attrs = env["attributes"]
    assert len(attrs) <= 4
    assert attrs["api_key"] == "[REDACTED]"
    assert attrs["authorization"] == "[REDACTED]"
    assert attrs["long_text"] == "xxxxx..."
    assert "extra" not in attrs


def test_basis_preserved_in_tool_output_wire(monkeypatch, tmp_path):
    out = tmp_path / "obs.jsonl"
    state_dir = str(tmp_path / "state")
    monkeypatch.setenv("HARNESS_OBSERVABILITY_EXPORT", "1")
    monkeypatch.setenv("HARNESS_OBSERVABILITY_EXPORT_FILE", str(out))
    ledger = ToolOutputSavingsLedger(state_dir)
    assert ledger.record(
        session_id="basis-sess",
        tool_call_id="tc-basis",
        original_chars=8000,
        compact_chars=1500,
        reason="persist",
    )
    assert _wait_for_file(out)
    rec = json.loads(out.read_text(encoding="utf-8").strip())
    assert rec["name"] == "harness.tool_output.savings"
    assert rec["attributes"]["basis"] == "measured"
    assert rec["attributes"]["tokens_saved"] > 0
    assert rec["attributes"]["reason"] == "persist"


def test_history_compaction_export_basis(monkeypatch, tmp_path):
    out = tmp_path / "obs.jsonl"
    state_dir = str(tmp_path / "state")
    monkeypatch.setenv("HARNESS_OBSERVABILITY_EXPORT", "1")
    monkeypatch.setenv("HARNESS_OBSERVABILITY_EXPORT_FILE", str(out))
    record_history_compaction(
        state_dir,
        "sess-compact",
        messages_compacted=5,
        chars_before=10_000,
        chars_after=2_000,
        summary_preview="summary",
        tokens_before=2500,
        tokens_after=500,
        estimated_cost_usd=0.012,
        savings_pct=0.8,
        compact_policy="warm",
    )
    assert _wait_for_file(out)
    rec = json.loads(out.read_text(encoding="utf-8").strip())
    assert rec["name"] == "harness.history.compaction"
    assert rec["attributes"]["basis"] == "measured"
    assert rec["attributes"]["estimated_cost_usd"] == pytest.approx(0.012)
    assert rec["attributes"]["savings_pct"] == pytest.approx(0.8)


def test_no_duplicate_ledger_records_on_export(monkeypatch, tmp_path):
    """Observability export must not add extra SQLite ledger rows."""
    state_dir = str(tmp_path / "state")
    out = tmp_path / "obs.jsonl"
    monkeypatch.setenv("HARNESS_OBSERVABILITY_EXPORT", "1")
    monkeypatch.setenv("HARNESS_OBSERVABILITY_EXPORT_FILE", str(out))
    try_record(
        state_dir=state_dir,
        session_id="dedupe",
        tool_call_id="tc-dedupe",
        original_chars=6000,
        compact_chars=1200,
        reason="persist",
    )
    db_path = os.path.join(state_dir, "tool_output_savings.sqlite")
    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM tool_output_savings").fetchone()[0]
    finally:
        conn.close()
    assert count == 1
    summary = get_ledger(state_dir).summarize(session_id="dedupe")
    assert summary.record_count == 1
    assert _wait_for_file(out)


def test_emit_event_never_raises_on_bad_serialization(monkeypatch, tmp_path):
    out = tmp_path / "obs.jsonl"
    monkeypatch.setenv("HARNESS_OBSERVABILITY_EXPORT", "1")
    monkeypatch.setenv("HARNESS_OBSERVABILITY_EXPORT_FILE", str(out))

    class Bad:
        def __str__(self):
            raise RuntimeError("boom")

    emit_event("harness.bad", {"obj": Bad()})
