"""Tests for OMP-inspired compact tool-output savings ledger."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.parse
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


def test_jsonl_mirror_uses_lf_only(tmp_path, monkeypatch):
    """JSONL audit mirror must write LF newlines on all platforms (no CRLF)."""
    monkeypatch.setenv("HARNESS_TOOL_OUTPUT_SAVINGS_JSONL", "1")
    ledger = ToolOutputSavingsLedger(str(tmp_path))
    assert ledger.record(
        session_id="sess-lf",
        tool_call_id="tc-lf",
        original_chars=8000,
        compact_chars=1500,
        reason="persist",
    )
    path = tmp_path / "tool_output_savings.jsonl"
    assert path.exists()
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")


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

    payload = job_savings_payload(state_dir, "job_beta", price_in=3.0)
    assert payload["tool_output_tokens_saved"] == tokens_avoided(2000, 200)
    assert payload["tool_output_compactions"] == 1
    assert payload["tool_output_savings_usd"] == pytest.approx(
        savings_usd(tokens_avoided(2000, 200), 3.0)
    )


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


def _write_pm_offload_jsonl(cli_dir, *, job_id: str, tool_call_id: str,
                            original_chars: int, compact_chars: int) -> int:
    """Write a Puppetmaster-shaped tool_output_savings.jsonl row; return tokens."""
    saved = tokens_avoided(original_chars, compact_chars)
    path = os.path.join(cli_dir, "tool_output_savings.jsonl")
    rec = {
        "ts": "2026-07-10T00:00:00+00:00",
        "kind": "tool_output_offload",
        "job_id": job_id,
        "task_id": "t1",
        "tool_name": "run_terminal",
        "tool_call_id": tool_call_id,
        "original_chars": original_chars,
        "compact_chars": compact_chars,
        "tokens_saved": saved,
        "reason": "tool-output offload",
        "path": "",
    }
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return saved


def test_job_savings_payload_merges_pm_jsonl_only(tmp_path):
    """Job with only PM-state JSONL offloads still surfaces token savings + USD."""
    harness_dir = str(tmp_path / "harness")
    os.makedirs(harness_dir)
    cli_dir = str(tmp_path / "cli")
    os.makedirs(cli_dir)
    saved = _write_pm_offload_jsonl(
        cli_dir,
        job_id="pm-job-1",
        tool_call_id="pm-tc-1",
        original_chars=20_000,
        compact_chars=2_000,
    )
    payload = job_savings_payload(
        harness_dir, "pm-job-1", cli_state_dir=cli_dir, price_in=3.0
    )
    assert payload["tool_output_tokens_saved"] == saved
    assert payload["tool_output_compactions"] == 1
    assert payload["tool_output_savings_usd"] == pytest.approx(savings_usd(saved, 3.0))


def test_job_savings_payload_dedupes_shared_tool_call_id(tmp_path):
    harness_dir = str(tmp_path / "harness")
    cli_dir = str(tmp_path / "cli")
    os.makedirs(cli_dir)
    get_ledger(harness_dir).record(
        session_id="sess",
        tool_call_id="shared-tc",
        original_chars=8_000,
        compact_chars=1_000,
        reason="persist",
        job_id="job-x",
    )
    _write_pm_offload_jsonl(
        cli_dir,
        job_id="job-x",
        tool_call_id="shared-tc",
        original_chars=9_000,
        compact_chars=500,
    )
    _write_pm_offload_jsonl(
        cli_dir,
        job_id="job-x",
        tool_call_id="pm-only-tc",
        original_chars=4_000,
        compact_chars=400,
    )
    payload = job_savings_payload(
        harness_dir, "job-x", cli_state_dir=cli_dir, price_in=2.0
    )
    expected = tokens_avoided(8_000, 1_000) + tokens_avoided(4_000, 400)
    assert payload["tool_output_tokens_saved"] == expected
    assert payload["tool_output_compactions"] == 2


def test_usage_and_swarm_live_surface_pm_only_job_offloads(tmp_path, monkeypatch):
    """A CLI-store job with only PM JSONL offloads shows on /api/usage + /api/swarm/live."""
    from types import SimpleNamespace

    from harness.job_scoping import stamp_task_payload
    from puppetmaster.models import Artifact, ArtifactType, Task
    from puppetmaster.store_factory import create_store

    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_dir.mkdir()
    cli_dir = tmp_path / "cli-state"
    cli_store = create_store("sqlite", str(cli_dir))
    job = cli_store.create_job("pm offload job")
    payload = stamp_task_payload({"cwd": str(repo)}, session_id="", cwd=str(repo))
    payload["model"] = "worker-model"
    task = Task(
        job_id=job.id,
        role="implement",
        instruction="do work",
        adapter="agentic",
        payload=payload,
    )
    cli_store.save_task(task)
    cli_store.save_artifact(
        Artifact(
            job_id=job.id,
            task_id=task.id,
            type=ArtifactType.VERIFICATION,
            created_by="worker",
            payload={
                "model": "worker-model",
                "tokens_in": 1_000,
                "tokens_out": 100,
                "check": "usage",
                "result": "ok",
            },
            confidence=0.9,
            evidence=["usage"],
        )
    )
    saved = _write_pm_offload_jsonl(
        str(cli_dir),
        job_id=job.id,
        tool_call_id="pm-api-tc",
        original_chars=16_000,
        compact_chars=1_600,
    )

    httpd, port, srv_mod = _usage_server(str(harness_dir))
    try:
        monkeypatch.setattr(
            "harness.cli_job_merge.resolve_cli_state_dir",
            lambda workspace_root="": str(cli_dir),
        )
        monkeypatch.setattr(srv_mod, "_job_in_cost_window", lambda created_at: True)
        monkeypatch.setattr(
            srv_mod,
            "_swarm_registry",
            lambda: [
                SimpleNamespace(
                    id="worker-model",
                    adapter_model_name="worker-model",
                    input_per_mtok_usd=1.0,
                    output_per_mtok_usd=2.0,
                    billing="metered",
                    marginal_cost_usd=lambda tin, tout: (
                        (tin / 1e6) * 1.0 + (tout / 1e6) * 2.0
                    ),
                    estimate_cost_usd=lambda tin, tout: (
                        (tin / 1e6) * 1.0 + (tout / 1e6) * 2.0
                    ),
                )
            ],
        )
        srv_mod._cfg.repo = str(repo)
        srv_mod._BOOT_REPOS.add(str(repo))

        headers = {"X-Harness-Token": srv_mod._TOKEN}
        scoped = urllib.parse.quote(str(repo), safe="")
        usage = _get_json(port, f"/api/usage?repo={scoped}", headers=headers)
        job_rows = [j for j in usage["jobs"] if j.get("job_id") == job.id]
        assert len(job_rows) == 1
        assert job_rows[0]["tool_output_tokens_saved"] == saved
        assert job_rows[0]["tool_output_savings_usd"] > 0
        assert usage["session"]["tool_output_tokens_saved"] >= saved

        swarm = _get_json(port, f"/api/swarm/live?repo={scoped}", headers=headers)
        swarm_rows = [j for j in swarm["jobs"] if j.get("id") == job.id]
        assert len(swarm_rows) == 1
        assert swarm_rows[0]["tool_output_tokens_saved"] == saved
        assert swarm_rows[0]["tool_output_savings_usd"] > 0
    finally:
        httpd.shutdown()
        srv_mod._BOOT_REPOS.discard(str(repo))
