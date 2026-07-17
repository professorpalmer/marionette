"""Repo-scoped /api/swarm/live session meters must not fold another workspace's pilot.

Python 3.9 safe. Hermetic HTTP against harness.server.Handler.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace


def _server(tmp_state_dir):
    import harness.server as srv

    srv._session.state_dir = tmp_state_dir
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _get(port, path, headers=None):
    req = urllib.request.Request(
        "http://127.0.0.1:%s%s" % (port, path),
        headers=headers or {},
        method="GET",
    )
    return urllib.request.urlopen(req, timeout=10)


def _post(port, path, body, headers=None):
    data = json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(
        "http://127.0.0.1:%s%s" % (port, path),
        data=data,
        headers=hdrs,
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=10)


def test_swarm_live_repo_scope_excludes_active_pilot_meters(monkeypatch):
    """?repo=A must not fold the active pilot's global meters into session spend
    when the pilot is attached to a different workspace."""
    tmp_dir = tempfile.mkdtemp()
    repo_a = tempfile.mkdtemp()
    repo_b = tempfile.mkdtemp()
    try:
        httpd, port, srv = _server(tmp_dir)
        try:
            headers = {"X-Harness-Token": srv._TOKEN}
            # Active workspace = repo_b; pilot carries large process meters.
            srv._cfg.repo = repo_b
            if not srv._sessions.active:
                srv._sessions.create(
                    "pilot on B", repo=repo_b, workspace_root=repo_b
                )
            srv._sync_pilot_session_id()
            srv._pilot._tokens_used = 500_000
            srv._pilot._tokens_in = 400_000
            srv._pilot._tokens_out = 100_000
            srv._pilot._tokens_cached = 0
            srv._pilot._worker_tokens_in = 0
            srv._pilot._worker_tokens_out = 0
            srv._pilot._worker_cost_usd = 0.0

            # Persist session-stamped spend only for repo_a.
            row_a = srv._sessions.create(
                "session on A", repo=repo_a, workspace_root=repo_a
            )
            srv._sessions.accumulate_meters(
                row_a["id"], estimated_cost_usd=0.11, input_tokens=1000, output_tokens=200
            )
            # Keep active pointer on repo_b's session (create above left it active;
            # creating A may have switched -- force B active).
            for s in srv._sessions._sessions:
                if s.get("workspace_root") == repo_b or s.get("repo") == repo_b:
                    srv._sessions._active = s["id"]
                    break
            srv._sync_pilot_session_id()

            monkeypatch.setattr(
                srv,
                "_scoped_jobs_with_stores",
                lambda repo_root=None: (
                    [{"id": "job_a1", "source": "harness", "status": "complete",
                      "goal": "a", "adapter": "agentic", "task_count": 0}],
                    SimpleNamespace(list_artifacts=lambda jid: [], list_tasks=lambda jid: []),
                    None,
                ),
            )
            monkeypatch.setattr(
                srv, "_job_swarm_accounting", lambda arts, registry: (2_000, 0.25)
            )
            monkeypatch.setattr(srv, "_job_savings_fields", lambda jid: {})
            monkeypatch.setattr(srv, "_slim_swarm_list_artifacts", lambda arts, state: [])
            monkeypatch.setattr(srv, "_task_swarm_accounting", lambda arts, registry: {})
            monkeypatch.setattr(srv, "_routing_saved_usd", lambda arts: 0.0)
            monkeypatch.setattr(srv, "_cache_saved_usd_swarm", lambda arts, registry: 0.0)
            monkeypatch.setattr(srv, "_tokens_cached_swarm", lambda arts: 0)
            monkeypatch.setattr(srv._pilot, "live_local_jobs", lambda: [])
            monkeypatch.setattr(srv, "_swarm_registry", lambda: [])
            monkeypatch.setattr(
                srv._session, "state",
                lambda: SimpleNamespace(
                    store=SimpleNamespace(
                        list_artifacts=lambda jid: [], list_tasks=lambda jid: []
                    ),
                    format_artifacts=lambda arts: [],
                ),
            )

            scoped = urllib.parse.quote(repo_a, safe="")
            live = json.loads(
                _get(port, "/api/swarm/live?repo=%s" % scoped, headers=headers)
                .read()
                .decode()
            )
            # 0.11 stamped on A + 0.25 store job -- NOT pilot B meters.
            assert abs(live["session"]["est_cost_usd"] - 0.36) < 1e-6
            assert live["session"]["tokens_used"] == 1000 + 200 + 2_000

            # Unscoped still includes the active pilot meters + active-repo jobs.
            unscoped = json.loads(
                _get(port, "/api/swarm/live", headers=headers).read().decode()
            )
            assert unscoped["session"]["tokens_used"] >= 500_000
            assert unscoped["session"]["tokens_used"] > live["session"]["tokens_used"]
            # Cost may price pilot tokens below stamped session dollars depending
            # on registry rates; token honesty is the hard contract here.
            assert unscoped["session"]["est_cost_usd"] >= 0.25 - 1e-9
        finally:
            # Process-global pilot meters would otherwise leak into later
            # /api/usage boot-pill assertions in the same pytest process.
            try:
                for attr in (
                    "_tokens_used",
                    "_tokens_in",
                    "_tokens_out",
                    "_tokens_cached",
                    "_worker_tokens_in",
                    "_worker_tokens_out",
                ):
                    setattr(srv._pilot, attr, 0)
                srv._pilot._worker_cost_usd = 0.0
            except Exception:
                pass
            httpd.shutdown()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.rmtree(repo_a, ignore_errors=True)
        shutil.rmtree(repo_b, ignore_errors=True)


def test_wiki_ingest_prepared_clears_graph_cache(monkeypatch):
    """POST /api/wiki/ingest-prepared must bust the wiki graph/status cache."""
    tmp_dir = tempfile.mkdtemp()
    try:
        httpd, port, srv = _server(tmp_dir)
        try:
            srv._wiki_graph_cache["stale"] = (999999.0, {"status": "ok"})
            monkeypatch.setattr(
                srv._pilot, "ingest_prepared_pages", lambda pages: len(pages or [])
            )
            resp = _post(
                port,
                "/api/wiki/ingest-prepared",
                {"pages": [{"title": "t", "body": "b"}]},
                headers={"X-Harness-Token": srv._TOKEN},
            )
            data = json.loads(resp.read().decode())
            assert data["ok"] is True
            assert data["ingested"] == 1
            assert not srv._wiki_graph_cache
        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
