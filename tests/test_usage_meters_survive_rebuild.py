"""Boot cost meters must survive pilot rebuild and multi-session attach."""
from __future__ import annotations

import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from harness.session_runners import SessionRunnerRegistry

_METER_ATTRS = (
    "_tokens_used",
    "_tokens_in",
    "_tokens_out",
    "_tokens_cached",
    "_worker_cost_usd",
    "_worker_tokens_in",
    "_worker_tokens_out",
)


def _spin_server():
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return srv, httpd, port


def _get_usage(port, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/usage?token={token}",
        headers={"X-Harness-Token": token},
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read().decode("utf-8"))


def _snapshot_meters(pilot):
    return {attr: getattr(pilot, attr, 0) for attr in _METER_ATTRS}


def _restore_meters(pilot, snap):
    for attr, val in snap.items():
        setattr(pilot, attr, val)


def _zero_boot_carry(srv):
    for attr in _METER_ATTRS:
        srv._BOOT_METER_CARRY[attr] = 0.0


def test_rebuild_pilot_preserves_usage_meters():
    srv, httpd, port = _spin_server()
    saved = _snapshot_meters(srv._pilot)
    saved_carry = dict(srv._BOOT_METER_CARRY)
    try:
        _zero_boot_carry(srv)
        srv._pilot._tokens_used = 12_000
        srv._pilot._tokens_in = 8_000
        srv._pilot._tokens_out = 4_000
        srv._pilot._tokens_cached = 1_500
        srv._pilot._worker_cost_usd = 0.42
        srv._pilot._worker_tokens_in = 900
        srv._pilot._worker_tokens_out = 300

        # Display tokens_used = pilot_only + store jobs (no jobs here =>
        # raw meters minus worker in/out already folded into the pilot).
        expected_tokens = 12_000 - 900 - 300
        before = _get_usage(port, srv._TOKEN)
        assert before["session"]["tokens_used"] == expected_tokens
        assert before["session"]["est_cost_usd"] > 0

        srv._rebuild_pilot_and_session()

        after = _get_usage(port, srv._TOKEN)
        assert after["session"]["tokens_used"] == expected_tokens
        assert getattr(srv._pilot, "_tokens_used") == 12_000
        assert getattr(srv._pilot, "_tokens_in") == 8_000
        assert getattr(srv._pilot, "_tokens_out") == 4_000
        assert getattr(srv._pilot, "_tokens_cached") == 1_500
        assert getattr(srv._pilot, "_worker_cost_usd") == 0.42
        assert getattr(srv._pilot, "_worker_tokens_in") == 900
        assert getattr(srv._pilot, "_worker_tokens_out") == 300
        assert after["session"]["est_cost_usd"] > 0
    finally:
        # Global singleton -- restore so later /api/usage tests see a clean pilot.
        _restore_meters(srv._pilot, saved)
        srv._BOOT_METER_CARRY.clear()
        srv._BOOT_METER_CARRY.update(saved_carry)
        httpd.shutdown()


def test_usage_sums_across_sessions_and_survives_reattach(tmp_path):
    """Spend on A, attach B in another repo, spend on B, switch back -- pill = A+B."""
    srv, httpd, port = _spin_server()
    old_runners = srv._runners
    old_pilot = srv._pilot
    old_repo = srv._cfg.repo
    old_env = os.environ.get("HARNESS_REPO")
    saved_carry = dict(srv._BOOT_METER_CARRY)
    saved_repos = set(srv._BOOT_REPOS)
    try:
        _zero_boot_carry(srv)
        srv._BOOT_REPOS.clear()

        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        repo_a.mkdir()
        repo_b.mkdir()

        reg = SessionRunnerRegistry(
            max_concurrent_sessions=3,
            on_drop=srv._fold_runner_meters_into_boot_carry,
        )
        sess_a = srv._sessions.create(
            title="A", repo=str(repo_a), workspace_root=str(repo_a)
        )
        sess_b = srv._sessions.create(
            title="B", repo=str(repo_b), workspace_root=str(repo_b)
        )
        sid_a, sid_b = sess_a["id"], sess_b["id"]

        srv._runners = reg
        srv._cfg.repo = str(repo_a)
        os.environ["HARNESS_REPO"] = str(repo_a)
        srv._note_boot_repo(str(repo_a))
        srv._sessions.switch(sid_a)
        srv._attach_view(sid_a)
        runner_a = srv._pilot
        runner_a._tokens_used = 5_000
        runner_a._tokens_in = 3_000
        runner_a._tokens_out = 2_000
        runner_a._tokens_cached = 0
        runner_a._worker_cost_usd = 0.0
        runner_a._worker_tokens_in = 0
        runner_a._worker_tokens_out = 0

        usage_a = _get_usage(port, srv._TOKEN)
        assert usage_a["session"]["tokens_used"] == 5_000

        # Attach session B in another repo -- new runner starts at zero meters.
        srv._cfg.repo = str(repo_b)
        os.environ["HARNESS_REPO"] = str(repo_b)
        srv._note_boot_repo(str(repo_b))
        srv._sessions.switch(sid_b)
        srv._attach_view(sid_b)
        runner_b = srv._pilot
        assert runner_b is not runner_a
        assert getattr(runner_b, "_tokens_used", 0) == 0

        usage_after_attach_b = _get_usage(port, srv._TOKEN)
        assert usage_after_attach_b["session"]["tokens_used"] == 5_000

        runner_b._tokens_used = 7_000
        runner_b._tokens_in = 4_000
        runner_b._tokens_out = 3_000

        usage_both = _get_usage(port, srv._TOKEN)
        assert usage_both["session"]["tokens_used"] == 12_000

        # Switch view back to A -- pill must still show A+B.
        srv._cfg.repo = str(repo_a)
        os.environ["HARNESS_REPO"] = str(repo_a)
        srv._sessions.switch(sid_a)
        srv._attach_view(sid_a)
        assert srv._pilot is runner_a

        usage_back = _get_usage(port, srv._TOKEN)
        assert usage_back["session"]["tokens_used"] == 12_000
        assert usage_back["session"]["est_cost_usd"] > 0
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        srv._cfg.repo = old_repo
        if old_env is None:
            os.environ.pop("HARNESS_REPO", None)
        else:
            os.environ["HARNESS_REPO"] = old_env
        srv._BOOT_METER_CARRY.clear()
        srv._BOOT_METER_CARRY.update(saved_carry)
        srv._BOOT_REPOS.clear()
        srv._BOOT_REPOS.update(saved_repos)
        httpd.shutdown()


def test_tool_output_savings_survive_attach_to_other_session(tmp_path):
    """Boot-pill tool-output savings stay process-wide across session/repo attach."""
    srv, httpd, port = _spin_server()
    old_runners = srv._runners
    old_pilot = srv._pilot
    old_repo = srv._cfg.repo
    old_state = getattr(srv._pilot, "state_dir", None)
    old_sid = getattr(srv._pilot, "harness_session_id", "")
    old_env = os.environ.get("HARNESS_REPO")
    saved_carry = dict(srv._BOOT_METER_CARRY)
    saved_repos = set(srv._BOOT_REPOS)
    try:
        _zero_boot_carry(srv)
        srv._BOOT_REPOS.clear()

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        repo_a = tmp_path / "repo-a"
        repo_b = tmp_path / "repo-b"
        repo_a.mkdir()
        repo_b.mkdir()

        from harness.tool_output_savings import get_ledger

        get_ledger(str(state_dir)).record(
            session_id="sess-a-savings",
            tool_call_id="tc-a-1",
            original_chars=20_000,
            compact_chars=2500,
            reason="persist",
        )

        reg = SessionRunnerRegistry(
            max_concurrent_sessions=3,
            on_drop=srv._fold_runner_meters_into_boot_carry,
        )
        sess_a = srv._sessions.create(
            title="A", repo=str(repo_a), workspace_root=str(repo_a)
        )
        sess_b = srv._sessions.create(
            title="B", repo=str(repo_b), workspace_root=str(repo_b)
        )
        sid_a, sid_b = sess_a["id"], sess_b["id"]

        srv._runners = reg
        srv._cfg.repo = str(repo_a)
        os.environ["HARNESS_REPO"] = str(repo_a)
        srv._note_boot_repo(str(repo_a))
        srv._sessions.switch(sid_a)
        srv._attach_view(sid_a)
        srv._pilot.state_dir = str(state_dir)
        srv._pilot.harness_session_id = "sess-a-savings"
        srv._pilot._tokens_used = 1_000

        usage_a = _get_usage(port, srv._TOKEN)
        saved_a = usage_a["session"]["tool_output_tokens_saved"]
        assert saved_a > 0
        assert usage_a["session"]["tool_output_compactions"] >= 1

        # Attach B (different session id) -- savings must not drop to zero.
        srv._cfg.repo = str(repo_b)
        os.environ["HARNESS_REPO"] = str(repo_b)
        srv._note_boot_repo(str(repo_b))
        srv._sessions.switch(sid_b)
        srv._attach_view(sid_b)
        srv._pilot.state_dir = str(state_dir)
        srv._pilot.harness_session_id = "sess-b-other"

        usage_b = _get_usage(port, srv._TOKEN)
        assert usage_b["session"]["tool_output_tokens_saved"] == saved_a
        assert usage_b["session"]["tool_output_compactions"] >= 1
        assert usage_b["session"]["tokens_used"] == 1_000
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        if old_state is not None:
            try:
                srv._pilot.state_dir = old_state
            except Exception:
                pass
        try:
            srv._pilot.harness_session_id = old_sid
        except Exception:
            pass
        srv._cfg.repo = old_repo
        if old_env is None:
            os.environ.pop("HARNESS_REPO", None)
        else:
            os.environ["HARNESS_REPO"] = old_env
        srv._BOOT_METER_CARRY.clear()
        srv._BOOT_METER_CARRY.update(saved_carry)
        srv._BOOT_REPOS.clear()
        srv._BOOT_REPOS.update(saved_repos)
        httpd.shutdown()


def test_drop_folds_meters_into_boot_carry():
    """Evicting a runner folds its meters so /api/usage still counts them."""
    srv, httpd, port = _spin_server()
    old_runners = srv._runners
    old_pilot = srv._pilot
    saved_carry = dict(srv._BOOT_METER_CARRY)
    try:
        _zero_boot_carry(srv)
        reg = SessionRunnerRegistry(
            max_concurrent_sessions=3,
            on_drop=srv._fold_runner_meters_into_boot_carry,
        )
        sess = srv._sessions.create(title="DropMe")
        sid = sess["id"]
        srv._runners = reg
        srv._sessions.switch(sid)
        srv._attach_view(sid)
        srv._pilot._tokens_used = 9_000
        srv._pilot._tokens_in = 6_000
        srv._pilot._tokens_out = 3_000

        before = _get_usage(port, srv._TOKEN)
        assert before["session"]["tokens_used"] == 9_000

        dropped = reg.drop(sid)
        assert dropped is not None
        assert float(srv._BOOT_METER_CARRY["_tokens_used"]) == 9_000

        # No live runners with meters; carry alone must still report spend.
        after = _get_usage(port, srv._TOKEN)
        assert after["session"]["tokens_used"] == 9_000
    finally:
        srv._runners = old_runners
        srv._pilot = old_pilot
        srv._BOOT_METER_CARRY.clear()
        srv._BOOT_METER_CARRY.update(saved_carry)
        httpd.shutdown()
