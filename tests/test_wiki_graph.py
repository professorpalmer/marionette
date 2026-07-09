"""Tests for Portable-LLM-Wiki Graph View integration."""
import json
import threading
import urllib.request
import urllib.error
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
    srv._wiki_graph_cache[fake_url] = (
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
    # Point config at the cached base so the handler hits the cache path.
    # WikiClient may still construct; monkeypatch by setting wiki_url and
    # ensuring cache key matches client.base_url.
    class _FakeClient:
        def __init__(self, *a, **k):
            self.base_url = fake_url
        def graph(self):
            raise AssertionError("should use cache, not fetch")

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
        srv._wiki_graph_cache.pop(fake_url, None)
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
