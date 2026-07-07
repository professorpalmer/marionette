"""Spill registry: index spilled tool outputs and resolve them as spill:// URIs."""
import os
import tempfile

from harness.context_budget import BudgetConfig, maybe_persist_result
from harness.internal_uri import (
    InternalUriContext,
    InternalUriError,
    is_internal_uri,
    resolve_internal_uri,
    search_internal_uris,
)
from harness.spill_registry import (
    list_spills,
    register_spill,
    resolve_spill,
    spill_uri,
    sweep_expired_spills,
)

import pytest

_GATE_FLOOR_CHARS = 12_500


def test_register_and_resolve_round_trip():
    with tempfile.TemporaryDirectory() as tmpdir:
        assert register_spill(tmpdir, "sess1", "call_a", "/x/pmharness-results/call_a.txt", 5000)
        row = resolve_spill(tmpdir, "sess1", "call_a")
        assert row is not None
        assert row["path"] == "/x/pmharness-results/call_a.txt"
        assert row["chars"] == 5000
        assert resolve_spill(tmpdir, "sess1", "missing") is None
    # TemporaryDirectory cleanup succeeding proves the handle is closed (Windows).


def test_reregister_replaces_previous_row():
    with tempfile.TemporaryDirectory() as tmpdir:
        register_spill(tmpdir, "sess1", "call_a", "/old.txt", 100)
        register_spill(tmpdir, "sess1", "call_a", "/new.txt", 200)
        rows = list_spills(tmpdir, session_id="sess1")
        assert len(rows) == 1
        assert rows[0]["path"] == "/new.txt"
        assert rows[0]["chars"] == 200


def test_spill_uri_rejects_unsafe_segments():
    assert spill_uri("sess1", "call_a") == "spill://sess1/call_a"
    assert spill_uri("bad/session", "call_a") is None
    assert spill_uri("sess1", "call with spaces") is None


def test_registration_failure_is_silent():
    assert register_spill("", "s", "t", "/p", 1) is False
    assert register_spill("/tmp", "", "t", "/p", 1) is False
    assert resolve_spill("", "s", "t") is None
    assert list_spills("") == []


def test_maybe_persist_result_emits_spill_uri():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = BudgetConfig(max_result_chars=10, turn_budget_chars=50)
        large = "x" * _GATE_FLOOR_CHARS
        msg = maybe_persist_result(
            large, "call_big", tmpdir, config, spill_session_id="sess1"
        )
        assert "spill://sess1/call_big" in msg
        assert "pmharness-results/call_big.txt" in msg
        row = resolve_spill(tmpdir, "sess1", "call_big")
        assert row is not None
        assert row["chars"] == _GATE_FLOOR_CHARS


def test_maybe_persist_result_without_session_omits_uri():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = BudgetConfig(max_result_chars=10, turn_budget_chars=50)
        msg = maybe_persist_result("y" * _GATE_FLOOR_CHARS, "call_plain", tmpdir, config)
        assert "spill://" not in msg
        assert resolve_spill(tmpdir, "default", "call_plain") is None


def test_spill_uri_resolves_full_content():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = BudgetConfig(max_result_chars=10, turn_budget_chars=50)
        large = "line one\n" * 2000
        maybe_persist_result(large, "call_read", tmpdir, config, spill_session_id="sess1")

        ctx = InternalUriContext(state_dir=tmpdir)
        assert is_internal_uri("spill://sess1/call_read")
        resource = resolve_internal_uri("spill://sess1/call_read", ctx)
        assert resource.content == large

        sliced = resolve_internal_uri("spill://sess1/call_read:2-3", ctx)
        assert "[lines 2-3 of 2000]" in sliced.content


def test_spill_directory_listings():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = BudgetConfig(max_result_chars=10, turn_budget_chars=50)
        maybe_persist_result("a" * _GATE_FLOOR_CHARS, "call_1", tmpdir, config, spill_session_id="sess1")
        maybe_persist_result("b" * _GATE_FLOOR_CHARS, "call_2", tmpdir, config, spill_session_id="sess2")

        ctx = InternalUriContext(state_dir=tmpdir)
        root = resolve_internal_uri("spill://", ctx)
        assert root.is_directory
        assert "sess1/call_1" in root.content
        assert "sess2/call_2" in root.content

        per_session = resolve_internal_uri("spill://sess1", ctx)
        assert per_session.is_directory
        assert "call_1" in per_session.content
        assert "call_2" not in per_session.content


def test_spill_resolution_rejects_escaped_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        outside = os.path.join(tmpdir, "secret.txt")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("secret")
        # A poisoned db row pointing outside pmharness-results must not be served.
        register_spill(tmpdir, "sess1", "evil", outside, 6)
        ctx = InternalUriContext(state_dir=tmpdir)
        with pytest.raises(InternalUriError):
            resolve_internal_uri("spill://sess1/evil", ctx)


def test_spill_resolution_missing_row_and_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = InternalUriContext(state_dir=tmpdir)
        with pytest.raises(InternalUriError):
            resolve_internal_uri("spill://sess1/nothing", ctx)

        config = BudgetConfig(max_result_chars=10, turn_budget_chars=50)
        msg = maybe_persist_result("z" * _GATE_FLOOR_CHARS, "call_gone", tmpdir, config, spill_session_id="sess1")
        assert "spill://sess1/call_gone" in msg
        os.remove(os.path.join(tmpdir, "pmharness-results", "call_gone.txt"))
        with pytest.raises(InternalUriError):
            resolve_internal_uri("spill://sess1/call_gone", ctx)


def test_search_state_finds_spills_without_store():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = BudgetConfig(max_result_chars=10, turn_budget_chars=50)
        maybe_persist_result("q" * _GATE_FLOOR_CHARS, "call_find_me", tmpdir, config, spill_session_id="sess1")

        ctx = InternalUriContext(state_dir=tmpdir)
        # Scheme-scoped search must not require the Puppetmaster job store.
        out = search_internal_uris("find_me", ctx, scheme="spill")
        assert "spill://sess1/call_find_me" in out

        miss = search_internal_uris("no-such-spill", ctx, scheme="spill")
        assert "no internal URI matches" in miss


def test_sweep_removes_old_rows_and_files():
    import sqlite3
    import time as time_mod

    with tempfile.TemporaryDirectory() as tmpdir:
        results_dir = os.path.join(tmpdir, "pmharness-results")
        os.makedirs(results_dir, exist_ok=True)
        old_path = os.path.join(results_dir, "old.txt")
        with open(old_path, "w", encoding="utf-8") as f:
            f.write("old content")
        register_spill(tmpdir, "sess1", "old_call", old_path, 100)

        conn = sqlite3.connect(os.path.join(tmpdir, "spill_index.sqlite"))
        try:
            conn.execute(
                "UPDATE spills SET ts = ? WHERE tool_call_id = ?",
                (time_mod.time() - (86400 * 30), "old_call"),
            )
            conn.commit()
        finally:
            conn.close()

        new_path = os.path.join(results_dir, "new.txt")
        with open(new_path, "w", encoding="utf-8") as f:
            f.write("new content")
        register_spill(tmpdir, "sess1", "new_call", new_path, 50)

        removed = sweep_expired_spills(tmpdir, 7)
        assert removed == 1
        assert not os.path.isfile(old_path)
        assert os.path.isfile(new_path)
        assert resolve_spill(tmpdir, "sess1", "old_call") is None
        assert resolve_spill(tmpdir, "sess1", "new_call") is not None


def test_sweep_retention_zero_keeps_all():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "pmharness-results", "keep.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("keep")
        register_spill(tmpdir, "sess1", "keep_call", path, 10)
        assert sweep_expired_spills(tmpdir, 0) == 0
        assert resolve_spill(tmpdir, "sess1", "keep_call") is not None


def test_usage_api_includes_spill_fields():
    import json
    import shutil
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from harness.context_budget import BudgetConfig, maybe_persist_result

    tmp_dir = tempfile.mkdtemp()
    try:
        import harness.server as srv

        srv._session.state_dir = tmp_dir
        srv._pilot.state_dir = tmp_dir
        srv._pilot.harness_session_id = "usage-spill-session"
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            config = BudgetConfig(max_result_chars=10, turn_budget_chars=50)
            maybe_persist_result(
                "x" * _GATE_FLOOR_CHARS,
                "spill_usage_call",
                tmp_dir,
                config,
                spill_session_id="usage-spill-session",
            )
            headers = {"X-Harness-Token": srv._TOKEN}
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/usage",
                headers=headers,
                method="GET",
            )
            usage = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
            session = usage["session"]
            assert session["spill_count"] >= 1
            assert session["spill_chars"] >= _GATE_FLOOR_CHARS
        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
