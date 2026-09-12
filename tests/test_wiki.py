"""Wiki integration: client config, digest rendering, slug safety, ingest payload."""
import json
from harness.wiki import WikiClient, session_digest, _safe_slug, _wiki_base_url_allowed


def test_not_configured_without_url_token(monkeypatch):
    monkeypatch.delenv("HARNESS_WIKI_URL", raising=False)
    monkeypatch.delenv("HARNESS_WIKI_TOKEN", raising=False)
    assert WikiClient().configured is False


def test_configured_with_env(monkeypatch):
    monkeypatch.setenv("HARNESS_WIKI_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("HARNESS_WIKI_TOKEN", "tok")
    assert WikiClient().configured is True


def test_rejects_http_non_loopback_base_url(monkeypatch):
    monkeypatch.delenv("HARNESS_WIKI_URL", raising=False)
    monkeypatch.delenv("WIKI_API_BASE", raising=False)
    c = WikiClient(base_url="http://evil.example.com:8000", token="tok")
    assert c.base_url == ""
    assert c.configured is False


def test_rejects_metadata_https_base_url(monkeypatch):
    monkeypatch.delenv("HARNESS_WIKI_URL", raising=False)
    monkeypatch.delenv("WIKI_API_BASE", raising=False)
    assert _wiki_base_url_allowed("https://169.254.169.254/latest") is False
    assert _wiki_base_url_allowed("https://metadata.google.internal/") is False
    c = WikiClient(base_url="https://169.254.169.254/latest", token="tok")
    assert c.base_url == ""
    assert c.configured is False


def test_accepts_https_base_url():
    c = WikiClient(base_url="https://wiki.example.com", token="tok")
    assert c.base_url == "https://wiki.example.com"
    assert c.configured is True


def test_accepts_http_loopback_base_url():
    c = WikiClient(base_url="http://127.0.0.1:8000", token="tok")
    assert c.base_url == "http://127.0.0.1:8000"
    assert c.configured is True


def test_ingest_unconfigured_returns_error(monkeypatch):
    monkeypatch.delenv("HARNESS_WIKI_URL", raising=False)
    monkeypatch.delenv("HARNESS_WIKI_TOKEN", raising=False)
    r = WikiClient().ingest("slug", "content")
    assert not r.ok and "not configured" in r.error


def test_safe_slug():
    assert _safe_slug("How does Auth WORK?!") == "how-does-auth-work"
    assert _safe_slug("") == "harness-session"


def test_session_digest_includes_findings():
    arts = [{"type": "finding", "headline": "Auth uses JWT in middleware.py"},
            {"type": "risk", "headline": "Token not rotated"}]
    d = session_digest("How does auth work?", ["I checked the middleware."], arts)
    assert "How does auth work?" in d
    assert "Auth uses JWT" in d and "Token not rotated" in d
    assert "[finding]" in d and "[risk]" in d


def test_session_digest_dedupes_findings():
    arts = [{"type": "finding", "headline": "same"},
            {"type": "finding", "headline": "same"}]
    d = session_digest("q", [], arts)
    assert d.count("same") == 1


def test_ingest_posts_correct_payload(monkeypatch):
    captured = {}
    class FakeResp:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"rel_path": "raw/conversations/x.md"}).encode()
    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        captured["auth"] = req.headers.get("Authorization")
        return FakeResp()
    monkeypatch.setattr("harness.wiki._wiki_safe_urlopen", fake_urlopen)
    c = WikiClient(base_url="https://wiki.example.com", token="secret")
    r = c.ingest("My Slug", "body text", note="n")
    assert r.ok and r.rel_path.endswith("x.md")
    assert captured["url"] == "https://wiki.example.com/owner/ingest"
    assert captured["body"]["slug"] == "my-slug"
    assert captured["body"]["content"] == "body text"
    assert captured["body"]["run_orchestrator"] is False
    assert captured["auth"] == "Bearer secret"


def test_strip_cross_host_auth_headers():
    import urllib.request
    from harness.wiki import _strip_cross_host_auth_headers

    req = urllib.request.Request(
        "https://sink.example/path",
        headers={
            "Authorization": "Bearer secret",
            "X-Share-Token": "secret",
            "Accept": "application/json",
        },
    )
    out = _strip_cross_host_auth_headers(
        "https://wiki.example/start", "https://sink.example/path", req
    )
    assert out.get_header("Authorization") is None
    assert out.get_header("X-share-token") is None
    assert out.get_header("Accept") == "application/json"

    same = urllib.request.Request(
        "https://wiki.example/next",
        headers={"Authorization": "Bearer keep"},
    )
    kept = _strip_cross_host_auth_headers(
        "https://wiki.example/start", "https://wiki.example/next", same
    )
    assert kept.get_header("Authorization") == "Bearer keep"


def test_wiki_safe_urlopen_blocks_metadata_literal():
    import pytest
    import urllib.error
    import urllib.request
    from harness.wiki import _wiki_safe_urlopen

    req = urllib.request.Request("https://169.254.169.254/latest/meta-data/")
    with pytest.raises(urllib.error.URLError):
        _wiki_safe_urlopen(req, timeout=1)


def test_search_hit_snippet_maps_excerpt_and_aliases():
    from harness.wiki import search_hit_snippet

    assert search_hit_snippet({"excerpt": "  protocol text  "}) == "protocol text"
    assert search_hit_snippet({"snippet": "old", "excerpt": "new"}) == "old"
    assert search_hit_snippet({"description": "compat"}) == "compat"
    assert search_hit_snippet({"body": "full"}) == "full"
    assert search_hit_snippet({}) == ""
    assert search_hit_snippet("nope") == ""


def test_query_relevant_passage_picks_match_past_prefix():
    from harness.wiki import query_relevant_passage

    body = (
        "Introduction to the incident.\n"
        + ("A" * 3500)
        + "\nThe prevention is reserving a unique path for each worker.\n"
        + ("Z" * 2000)
    )
    query = "what prevention did we choose for unique worker paths?"
    window = query_relevant_passage(body, query, 400)
    assert "prevention is reserving a unique path" in window
    assert window[:400] != body[:400]


def test_query_relevant_passage_head_fallback_when_no_body_terms():
    from harness.wiki import query_relevant_passage

    body = ("alpha " * 200) + "omega"
    window = query_relevant_passage(body, "zzzz notpresent", 80)
    assert window == body[:80]


def test_wiki_client_search_pages_maps_excerpt(monkeypatch):
    captured = {}

    class FakeResp:
        status = 200

        def read(self):
            return json.dumps({
                "results": [
                    {
                        "title": "Incident",
                        "slug": "incident",
                        "excerpt": "The prevention is reserving a unique path.",
                    }
                ]
            }).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=20):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        return FakeResp()

    monkeypatch.setattr("harness.wiki._wiki_safe_urlopen", fake_urlopen)
    client = WikiClient(base_url="https://wiki.example.com", token="secret")
    hits = client.search_pages("prevention", limit=3)
    assert hits == [{
        "title": "Incident",
        "slug": "incident",
        "snippet": "The prevention is reserving a unique path.",
    }]
    assert "/wiki/search?q=" in captured["url"]
    assert captured["auth"] == "Bearer secret"


def test_wiki_client_page_body_reads_body_not_excerpt(monkeypatch):
    captured = {}

    class FakeResp:
        status = 200

        def read(self):
            return json.dumps({
                "slug": "incident",
                "excerpt": "intro only",
                "body": "full page body with the prevention below the fold",
            }).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=20):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        return FakeResp()

    monkeypatch.setattr("harness.wiki._wiki_safe_urlopen", fake_urlopen)
    client = WikiClient(base_url="https://wiki.example.com", token="secret")
    assert client.page_body("decisions/incident page") == (
        "full page body with the prevention below the fold"
    )
    assert captured["url"] == (
        "https://wiki.example.com/wiki/page/decisions%2Fincident%20page"
    )
    assert captured["auth"] == "Bearer secret"


def test_wiki_client_page_body_ignores_excerpt_only_payload(monkeypatch):
    class FakeResp:
        status = 200

        def read(self):
            return json.dumps({"excerpt": "not a full page"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("harness.wiki._wiki_safe_urlopen", lambda req, timeout=20: FakeResp())
    client = WikiClient(base_url="https://wiki.example.com", token="secret")
    assert client.page_body("incident") == ""


def test_wiki_client_query_search_fallback_maps_excerpt(monkeypatch):
    class FakeResp:
        status = 200

        def read(self):
            return json.dumps({
                "results": [
                    {"title": "T", "slug": "t", "excerpt": "excerpt text"},
                ]
            }).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=20):
        if "/wiki/search" in req.full_url:
            return FakeResp()
        raise RuntimeError("no rag")

    monkeypatch.setattr("harness.wiki._wiki_safe_urlopen", fake_urlopen)
    client = WikiClient(base_url="https://wiki.example.com", token="secret")
    out = client.query("prevention")
    assert "excerpt text" in out
