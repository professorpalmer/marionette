"""Tests for Portable-LLM-Wiki Graph View integration."""
import json
import threading
import urllib.request
import urllib.error
import urllib.parse
from http.server import ThreadingHTTPServer

import pytest
from harness.wiki import WikiClient, parse_graph_from_response


def _server():
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {}, method="GET")
    return urllib.request.urlopen(req, timeout=10)


def test_wiki_graph_endpoint_rejected_without_token():
    httpd, port, srv = _server()
    try:
        try:
            _get(port, "/api/wiki/graph")
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()


def test_wiki_graph_endpoint_graceful_not_configured():
    httpd, port, srv = _server()
    orig_url = srv._cfg.wiki_url
    srv._cfg.wiki_url = ""  # simulate not configured
    try:
        resp = _get(port, "/api/wiki/graph", {"X-Harness-Token": srv._TOKEN})
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["configured"] is False
        assert data["status"] == "not_configured"
        assert data["nodes"] == []
        assert data["edges"] == []
    finally:
        srv._cfg.wiki_url = orig_url
        httpd.shutdown()


def test_wiki_status_endpoint_counts_only_not_configured():
    """State pane uses /api/wiki/status for counts -- no nodes/edges arrays."""
    httpd, port, srv = _server()
    orig_url = srv._cfg.wiki_url
    srv._cfg.wiki_url = ""
    try:
        resp = _get(port, "/api/wiki/status", {"X-Harness-Token": srv._TOKEN})
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["configured"] is False
        assert data["status"] == "not_configured"
        assert data["page_count"] == 0
        assert data["link_count"] == 0
        assert "nodes" not in data
        assert "edges" not in data
    finally:
        srv._cfg.wiki_url = orig_url
        httpd.shutdown()


def test_wiki_status_reuses_graph_cache_counts():
    httpd, port, srv = _server()
    # Seed the shared graph cache as if /api/wiki/graph already ran.
    fake_url = "https://wiki-status-test.example"

    class _FakeClient:
        def __init__(self, *a, **k):
            self.base_url = fake_url
            self.token = ""
        def graph(self):
            raise AssertionError("should use cache, not fetch")
        def manifest_meta(self):
            return {}

    cache_key = srv._wiki_cache_key(_FakeClient())
    srv._wiki_graph_cache[cache_key] = (
        __import__("time").monotonic() + 60.0,
        {
            "configured": True,
            "status": "ok",
            "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "edges": [{"source": "a", "target": "b"}],
            "base_url": fake_url,
        },
    )
    orig_url = srv._cfg.wiki_url
    import harness.wiki as wiki_mod
    orig_cls = wiki_mod.WikiClient
    wiki_mod.WikiClient = _FakeClient
    srv._cfg.wiki_url = fake_url
    try:
        resp = _get(port, "/api/wiki/status", {"X-Harness-Token": srv._TOKEN})
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["status"] == "ok"
        assert data["page_count"] == 3
        assert data["link_count"] == 1
        assert "nodes" not in data
        assert "edges" not in data
    finally:
        wiki_mod.WikiClient = orig_cls
        srv._cfg.wiki_url = orig_url
        srv._wiki_graph_cache.pop(cache_key, None)
        httpd.shutdown()


def test_wiki_client_parse_already_normalized():
    data = {
        "nodes": [
            {"id": "index", "title": "Index Page", "section": "main"},
            {"id": "about", "title": "About Us"}
        ],
        "edges": [
            {"source": "index", "target": "about"}
        ]
    }
    parsed = parse_graph_from_response(data)
    assert parsed["nodes"] == [
        {"id": "index", "title": "Index Page", "section": "main", "tags": None},
        {"id": "about", "title": "About Us", "section": None, "tags": None}
    ]
    assert parsed["edges"] == [
        {"source": "index", "target": "about"}
    ]


def test_wiki_client_parse_manifest_pages():
    data = [
        {
            "slug": "page-one",
            "title": "Page One",
            "section": "docs",
            "tags": ["guide"],
            "links": ["page-two"]
        },
        {
            "slug": "page-two",
            "title": "Page Two",
            "content": "Referencing [[page-one]] and [[non-existent-page|Cool Page]]."
        }
    ]
    parsed = parse_graph_from_response(data)
    assert len(parsed["nodes"]) == 2
    assert parsed["nodes"][0]["id"] == "page-one"
    assert parsed["nodes"][0]["section"] == "docs"
    assert parsed["nodes"][0]["tags"] == ["guide"]
    
    # page-one has explicit link to page-two
    # page-two has content referencing page-one and non-existent-page (which is slugified to non-existent-page)
    # So we should have edges from page-one -> page-two and page-two -> page-one, and page-two -> non-existent-page
    edges = parsed["edges"]
    assert {"source": "page-one", "target": "page-two"} in edges
    assert {"source": "page-two", "target": "page-one"} in edges
    assert {"source": "page-two", "target": "non-existent-page"} in edges


def test_wiki_client_graph_prefers_direct_graph_endpoint(monkeypatch):
    # Current portable-llm-wiki exposes the full owner-visible graph at
    # /wiki/graph. Marionette should use it before falling back to the older
    # manifest + per-slug neighborhood flow.
    class FakeResp:
        def __init__(self, payload):
            self._payload = payload
            self.status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps(self._payload).encode()

    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        assert req.headers.get("Authorization") == "Bearer mysecret"
        if req.full_url.endswith("/wiki/graph"):
            return FakeResp({
                "nodes": [{"slug": "a", "title": "A"}],
                "edges": [{"source": "a", "target": "b"}],
            })
        raise AssertionError("unexpected url " + req.full_url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = WikiClient(base_url="https://mywiki.example.com", token="mysecret")
    res = client.graph()
    assert res["error"] is None
    assert res["nodes"] == [{"id": "a", "title": "A", "section": None, "tags": None}]
    assert res["edges"] == [{"source": "a", "target": "b"}]
    assert calls == ["https://mywiki.example.com/wiki/graph"]


def test_wiki_client_graph_live_mocked(monkeypatch):
    # Legacy fallback: GET /wiki/manifest.json for nodes, then
    # GET /wiki/graph/<slug>?hops=1 for edges.
    class FakeResp:
        def __init__(self, payload):
            self._payload = payload
            self.status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps(self._payload).encode()

    manifest = {
        "pages": [
            {"slug": "a", "title": "A", "section": "root", "tags": []},
            {"slug": "b", "title": "B", "section": "root", "tags": []},
        ]
    }
    graph_a = {"edges": [{"source": "a", "target": "b"}]}
    graph_b = {"edges": [{"source": "b", "target": "a"}]}  # reverse -> deduped undirected

    def fake_urlopen(req, timeout=0):
        url = req.full_url
        assert req.headers.get("Authorization") == "Bearer mysecret"
        if url.endswith("/wiki/graph"):
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        if url.endswith("/wiki/manifest.json"):
            return FakeResp(manifest)
        if "/wiki/graph/a" in url:
            return FakeResp(graph_a)
        if "/wiki/graph/b" in url:
            return FakeResp(graph_b)
        raise AssertionError("unexpected url " + url)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = WikiClient(base_url="https://mywiki.example.com", token="mysecret")
    res = client.graph()
    assert res["error"] is None
    assert len(res["nodes"]) == 2
    ids = {n["id"] for n in res["nodes"]}
    assert ids == {"a", "b"}
    # a<->b edge is collected once (undirected dedupe across both slugs'
    # neighborhoods). The collection order of the single edge is not
    # deterministic (dict/set iteration), so compare it order-independently by
    # its unordered endpoint pair -- otherwise this test flakes red at random.
    assert len(res["edges"]) == 1
    edge = res["edges"][0]
    assert {edge["source"], edge["target"]} == {"a", "b"}


def test_wiki_status_extras_private_share_token_not_needs_auth():
    """Personal LLM share tokens are not owner; must not force needs_auth."""
    import harness.server as srv

    class _Client:
        base_url = "https://api.portablellm.wiki/t/acme"
        token = "share-tok"

        def manifest_meta(self):
            return {
                "page_count": 120,
                "viewer_tier": "private",
                "viewer_is_owner": False,
            }

    extras = srv._wiki_status_extras(_Client())
    assert extras.get("status") != "needs_auth"
    assert extras.get("viewer_tier") == "private"
    assert extras.get("viewer_is_owner") is False


def test_wiki_status_extras_public_with_token_needs_auth():
    import harness.server as srv

    class _Client:
        base_url = "https://api.portablellm.wiki/t/acme"
        token = "bad-tok"

        def manifest_meta(self):
            return {
                "page_count": 12,
                "viewer_tier": "public",
                "viewer_is_owner": False,
            }

    extras = srv._wiki_status_extras(_Client())
    assert extras.get("status") == "needs_auth"
    assert "Disconnect" in (extras.get("hint") or "")


def test_wiki_cache_key_changes_with_token():
    import harness.server as srv

    class _C:
        def __init__(self, tok):
            self.base_url = "https://api.portablellm.wiki/t/acme"
            self.token = tok

    assert srv._wiki_cache_key(_C("")) != srv._wiki_cache_key(_C("secret"))
    assert srv._wiki_cache_key(_C("a")) == srv._wiki_cache_key(_C("a"))


def test_wiki_connect_and_disconnect_endpoints(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    httpd, port, srv = _server()
    try:
        # Seed a stale public cache entry that must clear on connect/disconnect.
        srv._wiki_graph_cache["stale"] = (999999.0, {"status": "needs_auth"})
        nonce = srv._mint_wiki_connect_nonce()
        personal = "https://portablellm.wiki/acme/llm?t=fresh-token"
        connect_path = (
            "/api/wiki/connect?nonce=%s&url=%s"
            % (nonce, urllib.parse.quote(personal, safe=""))
        )
        resp = _get(port, connect_path)
        assert resp.status == 200
        body = resp.read().decode()
        assert "Wiki linked" in body
        assert not srv._wiki_graph_cache
        cfg = json.loads((state / "wiki.json").read_text(encoding="utf-8"))
        assert cfg["api_base"] == "https://api.portablellm.wiki/t/acme"
        assert cfg["owner_token"] == "fresh-token"

        # Replayed nonce must fail.
        try:
            _get(port, connect_path)
            assert False, "replay should 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        # Disconnect clears config.
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/wiki/disconnect",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-Harness-Token": srv._TOKEN,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        assert data["api_base"] == ""
        assert data["has_token"] is False
        on_disk = json.loads((state / "wiki.json").read_text(encoding="utf-8"))
        assert on_disk == {}
    finally:
        httpd.shutdown()


def test_wiki_connect_rejects_expired_nonce(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    httpd, port, srv = _server()
    try:
        import harness.api.wiki as wiki_api

        # Freeze monotonic so we can deterministically expire the one-shot nonce.
        now = [100.0]
        monkeypatch.setattr(wiki_api.time, "monotonic", lambda: now[0])

        nonce = srv._mint_wiki_connect_nonce()
        personal = "https://portablellm.wiki/acme/llm?t=fresh-token"
        connect_path = (
            "/api/wiki/connect?nonce=%s&url=%s"
            % (nonce, urllib.parse.quote(personal, safe=""))
        )

        # Expire beyond TTL so consume_wiki_connect_nonce must fail.
        now[0] += wiki_api.WIKI_CONNECT_NONCE_TTL + 1.0
        try:
            _get(port, connect_path)
            assert False, "expected 403 for expired nonce"
        except urllib.error.HTTPError as e:
            assert e.code == 403
            body = e.read().decode("utf-8", errors="ignore")
            assert "Link expired" in body or "expired" in body.lower()
    finally:
        httpd.shutdown()


def test_wiki_connect_rejects_non_loopback_host_even_pre_auth(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    httpd, port, srv = _server()
    try:
        nonce = srv._mint_wiki_connect_nonce()
        personal = "https://portablellm.wiki/acme/llm?t=fresh-token"
        connect_path = (
            "/api/wiki/connect?nonce=%s&url=%s"
            % (nonce, urllib.parse.quote(personal, safe=""))
        )
        # Wiki connect is pre-auth, but still must be loopback-only.
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{connect_path}",
            headers={"Host": "evil.com"},
            method="GET",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "expected 403 for non-loopback Host"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()


def test_wiki_handoff_returns_loopback_setup_url():
    httpd, port, srv = _server()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/wiki/handoff",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-Harness-Token": srv._TOKEN,
                "Host": f"127.0.0.1:{port}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        assert data["ok"] is True
        assert data["nonce"]
        assert data["return_url"] == f"http://127.0.0.1:{port}/api/wiki/connect"
        assert "connect/marionette" in data["setup_url"]
        assert "client=marionette" in data["setup_url"]
        assert "return=" in data["setup_url"]
    finally:
        httpd.shutdown()


def test_in_process_ingest_prepared_pages_clears_graph_cache(monkeypatch):
    """Direct pilot.ingest_prepared_pages must bust graph cache like the HTTP route."""
    from harness.wiki import WikiResult

    httpd, port, srv = _server()
    try:
        srv._wiki_graph_cache["stale"] = (999999.0, {"status": "ok"})
        pilot = srv._pilot
        pilot._wiki.base_url = "https://wiki.example.com"
        pilot._wiki.token = "tok"
        monkeypatch.setattr(
            pilot._wiki, "ingest", lambda *a, **k: WikiResult(True, rel_path="x.md")
        )
        count = pilot.ingest_prepared_pages(
            [{"kind": "concept", "title": "t", "body": "b"}]
        )
        assert count == 1
        assert not srv._wiki_graph_cache
    finally:
        httpd.shutdown()


def test_maybe_ingest_clears_graph_cache(monkeypatch):
    """Auto _maybe_ingest must bust graph cache after a successful wiki write."""
    from harness.wiki import WikiResult

    httpd, port, srv = _server()
    try:
        srv._wiki_graph_cache["stale"] = (999999.0, {"status": "ok"})
        pilot = srv._pilot
        pilot._wiki.base_url = "https://wiki.example.com"
        pilot._wiki.token = "tok"
        pilot._wiki_auto = True
        monkeypatch.setattr(
            pilot._wiki, "ingest", lambda *a, **k: WikiResult(True, rel_path="x.md")
        )
        pilot._maybe_ingest(
            "How does auth work?",
            ["checked middleware"],
            [{"type": "finding", "headline": "Auth uses JWT"}],
        )
        assert not srv._wiki_graph_cache
    finally:
        httpd.shutdown()
