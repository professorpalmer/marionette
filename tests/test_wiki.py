"""Wiki integration: client config, digest rendering, slug safety, ingest payload."""
import json
from harness.wiki import WikiClient, session_digest, _safe_slug


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
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    c = WikiClient(base_url="https://wiki.example.com", token="secret")
    r = c.ingest("My Slug", "body text", note="n")
    assert r.ok and r.rel_path.endswith("x.md")
    assert captured["url"] == "https://wiki.example.com/owner/ingest"
    assert captured["body"]["slug"] == "my-slug"
    assert captured["body"]["content"] == "body text"
    assert captured["body"]["run_orchestrator"] is False
    assert captured["auth"] == "Bearer secret"
