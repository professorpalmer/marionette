"""Tests for OMP-inspired compact tool-output savings ledger."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import ThreadingHTTPServer

import pytest

from harness.tool_output_savings import (
    CHARS_PER_TOKEN,
    ToolOutputSavingsLedger,
    aggregate_jsonl_records,
    estimate_tokens,
    get_ledger,
    job_savings_payload,
    parse_jsonl_records,
    savings_usd,
    session_savings_payload,
    tokens_avoided,
    try_record,
)


def test_tokens_avoided_is_deterministic():
    assert estimate_tokens(100) == 25
    assert tokens_avoided(10_000, 1_500) == estimate_tokens(10_000) - estimate_tokens(1_500)
    assert tokens_avoided(100, 100) == 0
    assert tokens_avoided(50, 200) == 0


def test_savings_usd_uses_input_price():
    assert savings_usd(100_000, 3.0) == pytest.approx(0.3)


def test_sqlite_dedupe_by_session_and_tool_call_id(tmp_path):
    ledger = ToolOutputSavingsLedger(str(tmp_path))
    assert ledger.record(
        session_id="sess-a",
        tool_call_id="tc-1",
        original_chars=8000,
        compact_chars=1500,
        reason="persist",
    )
    assert not ledger.record(
        session_id="sess-a",
        tool_call_id="tc-1",
        original_chars=9000,
        compact_chars=1000,
        reason="persist",
    )
    summary = ledger.summarize(session_id="sess-a")
    assert summary.record_count == 1
    assert summary.tokens_saved == tokens_avoided(8000, 1500)


def test_concurrent_append_safety(tmp_path):
    """Many threads appending distinct tool_call ids must not corrupt SQLite."""
    state_dir = str(tmp_path)
    n = 24

    def _worker(i: int) -> bool:
        return get_ledger(state_dir).record(
            session_id="concurrent",
            tool_call_id=f"tc-{i}",
            original_chars=5000 + i,
            compact_chars=1200,
            reason="persist",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_worker, i) for i in range(n)]
        results = [f.result() for f in as_completed(futures)]

    assert sum(1 for r in results if r) == n
    summary = get_ledger(state_dir).summarize(session_id="concurrent")
    assert summary.record_count == n


def test_malformed_jsonl_lines_are_skipped(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(
        "\n".join(
            [
                "",
                "{not json",
                json.dumps(
                    {
                        "session_id": "s1",
                        "tool_call_id": "a",
                        "original_chars": 4000,
                        "compact_chars": 1000,
                        "tokens_saved": tokens_avoided(4000, 1000),
                        "reason": "persist",
                    }
                ),
                json.dumps(
                    {
                        "session_id": "s1",
                        "tool_call_id": "a",
                        "original_chars": 9000,
                        "compact_chars": 500,
                        "tokens_saved": tokens_avoided(9000, 500),
                        "reason": "persist",
                    }
                ),
                json.dumps(
                    {
                        "session_id": "s1",
                        "tool_call_id": "b",
                        "original_chars": 2000,
                        "compact_chars": 800,
                        "tokens_saved": tokens_avoided(2000, 800),
                        "reason": "turn_budget",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    records = parse_jsonl_records(path)
    assert len(records) == 3
    summary = aggregate_jsonl_records(records, session_id="s1")
    assert summary.record_count == 2
    assert summary.tokens_saved == tokens_avoided(4000, 1000) + tokens_avoided(2000, 800)


def test_try_record_never_raises_on_bad_state_dir():
    try_record(
        state_dir="/nonexistent\0bad",
        session_id="s",
        tool_call_id="tc",
        original_chars=5000,
        compact_chars=1000,
        reason="persist",
    )


def test_session_savings_payload_shape(tmp_path):
    ledger = ToolOutputSavingsLedger(str(tmp_path))
    ledger.record(
        session_id="sess-x",
        tool_call_id="tc-99",
        original_chars=12_000,
        compact_chars=2000,
        reason="persist",
    )
    payload = session_savings_payload(str(tmp_path), "sess-x", price_in=2.0)
    assert payload["tool_output_compactions"] == 1
    assert payload["tool_output_tokens_saved"] > 0
    assert payload["tool_output_savings_usd"] > 0


def _usage_server(tmp_state_dir):
    import harness.server as srv

    srv._session.state_dir = tmp_state_dir
    srv._pilot.state_dir = tmp_state_dir
    srv._pilot.harness_session_id = "usage-test-session"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _get_json(port, path, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers=headers or {},
        method="GET",
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read().decode())


def test_usage_api_includes_tool_output_savings_fields():
    tmp_dir = tempfile.mkdtemp()
    try:
        httpd, port, srv = _usage_server(tmp_dir)
        try:
            from harness.tool_output_savings import get_ledger

            get_ledger(tmp_dir).record(
                session_id="usage-test-session",
                tool_call_id="api-tc-1",
                original_chars=20_000,
                compact_chars=2500,
                reason="persist",
            )
            headers = {"X-Harness-Token": srv._TOKEN}
            usage = _get_json(port, "/api/usage", headers=headers)
            session = usage["session"]
            assert session["tool_output_tokens_saved"] > 0
            assert session["tool_output_savings_usd"] > 0
            assert session["tool_output_compactions"] == 1

            swarm = _get_json(port, "/api/swarm/live", headers=headers)
            assert swarm["session"]["tool_output_tokens_saved"] == session["tool_output_tokens_saved"]
        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_context_usage_api_includes_tool_output_savings():
    tmp_dir = tempfile.mkdtemp()
    try:
        httpd, port, srv = _usage_server(tmp_dir)
        try:
            from harness.tool_output_savings import get_ledger

            get_ledger(tmp_dir).record(
                session_id="usage-test-session",
                tool_call_id="ctx-tc-1",
                original_chars=16_000,
                compact_chars=1800,
                reason="persist",
            )
            headers = {"X-Harness-Token": srv._TOKEN}
            ctx = _get_json(port, "/api/context/usage", headers=headers)
            assert ctx["tool_output_tokens_saved"] > 0
            assert ctx["tool_output_savings_usd"] >= 0
            assert ctx["tool_output_compactions"] == 1
        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_integration_maybe_persist_records_savings(tmp_path):
    from harness.context_budget import BudgetConfig, maybe_persist_result

    state_dir = str(tmp_path)
    session_id = "integ-session"
    tc_id = "read_file_1"
    large = "x" * 12_500
    config = BudgetConfig(max_result_chars=800, preview_chars=400)

    from harness.tool_output_savings import make_compaction_callback

    cb = make_compaction_callback(
        state_dir=state_dir,
        session_id=session_id,
        tool_call_id=tc_id,
    )
    result = maybe_persist_result(
        large,
        tc_id,
        state_dir,
        config,
        on_compaction=cb,
    )
    assert len(result) < len(large)
    summary = get_ledger(state_dir).summarize(session_id=session_id)
    assert summary.record_count == 1
    assert summary.tokens_saved == tokens_avoided(len(large), len(result))


def test_job_id_migration_and_filter(tmp_path):
    state_dir = str(tmp_path)
    ledger = get_ledger(state_dir)
    ledger.record(
        session_id="sess",
        tool_call_id="tc1",
        original_chars=4000,
        compact_chars=400,
        reason="persist",
        job_id="job_alpha",
    )
    ledger.record(
        session_id="sess",
        tool_call_id="tc2",
        original_chars=2000,
        compact_chars=200,
        reason="persist",
        job_id="job_beta",
    )
    alpha = ledger.summarize(job_id="job_alpha")
    assert alpha.record_count == 1
    assert alpha.tokens_saved == tokens_avoided(4000, 400)

    payload = job_savings_payload(state_dir, "job_beta")
    assert payload["tool_output_tokens_saved"] == tokens_avoided(2000, 200)
    assert payload["tool_output_compactions"] == 1


def test_job_id_column_added_to_legacy_db(tmp_path):
    state_dir = str(tmp_path)
    db_path = tmp_path / "tool_output_savings.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE tool_output_savings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            session_id TEXT NOT NULL,
            tool_call_id TEXT NOT NULL,
            original_chars INTEGER NOT NULL,
            compact_chars INTEGER NOT NULL,
            tokens_saved INTEGER NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            UNIQUE(session_id, tool_call_id)
        );
        """
    )
    conn.execute(
        "INSERT INTO tool_output_savings "
        "(ts, session_id, tool_call_id, original_chars, compact_chars, tokens_saved, reason) "
        "VALUES (?,?,?,?,?,?,?)",
        (1.0, "legacy", "tc0", 1000, 100, tokens_avoided(1000, 100), "persist"),
    )
    conn.commit()
    conn.close()

    ledger = get_ledger(state_dir)
    ledger.record(
        session_id="legacy",
        tool_call_id="tc1",
        original_chars=800,
        compact_chars=80,
        reason="persist",
        job_id="job_legacy",
    )
    summary = ledger.summarize(job_id="job_legacy")
    assert summary.record_count == 1


@pytest.mark.parametrize("chars_per_token", [CHARS_PER_TOKEN])
def test_cross_platform_char_token_ratio_documented(chars_per_token):
    """Guard the deterministic ratio used on Windows and macOS alike."""
    assert chars_per_token == 4
