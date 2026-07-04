import os
import sys
import tempfile
import urllib.request
import pytest

from harness.web_tools import web_search, web_fetch, read_pdf, is_safe_path


class FakeResponse:
    def __init__(self, content: bytes, headers: dict = None):
        self.content = content
        self.headers = headers if headers is not None else {}

    def read(self, amt=None):
        # Mirror http.client.HTTPResponse.read(amt=None): an optional byte cap.
        # web_tools now does bounded reads (response.read(MAX_BYTES + 1)) to
        # guard against memory exhaustion, so the mock must accept the arg.
        if amt is None:
            return self.content
        return self.content[:amt]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


@pytest.fixture
def mock_urlopen(monkeypatch):
    """Fixture to mock urllib.request.urlopen."""
    calls = []

    def _mock(req, timeout=None):
        # web_tools routes all fetches through _safe_urlopen(req, timeout) (an
        # SSRF-safe opener that re-validates redirect hops). Accept timeout
        # positionally-or-by-keyword so this mock matches that call shape.
        url = req.full_url if hasattr(req, "full_url") else req
        calls.append(url)
        
        if "duckduckgo.com" in url:
            html = """
            <html>
            <body>
            <div class="result results_links results_links_deep web-result">
                <h2 class="result__title">
                    <a class="result__a" href="https://example1.com">Example 1</a>
                </h2>
                <span class="result__snippet">This is the first mock search result snippet.</span>
            </div>
            <div class="result results_links results_links_deep web-result">
                <h2 class="result__title">
                    <a class="result__a" href="https://example2.com">Example 2</a>
                </h2>
                <span class="result__snippet">This is the second mock search result snippet.</span>
            </div>
            </body>
            </html>
            """
            return FakeResponse(html.encode("utf-8"), {"Content-Type": "text/html"})
        elif "testpage.com" in url:
            html = """
            <html>
            <head><title>Test Page</title><style>body { color: red; }</style></head>
            <body>
            <script>console.log("hello");</script>
            <h1>Main Heading</h1>
            <p>This is a paragraph of text on the test page.</p>
            </body>
            </html>
            """
            return FakeResponse(html.encode("utf-8"), {"Content-Type": "text/html"})
        elif "testpdf.com" in url:
            return FakeResponse(b"%PDF-1.4 mock pdf data", {"Content-Type": "application/pdf"})
        elif "testjson.com" in url:
            return FakeResponse(b'{"key": "value"}', {"Content-Type": "application/json"})
        else:
            return FakeResponse(b"Bare text response", {"Content-Type": "text/plain"})

    import harness.web_tools as _wt; monkeypatch.setattr(_wt, "_safe_urlopen", _mock)
    return calls


def test_web_search_success(mock_urlopen):
    res = web_search("test query")
    assert "Example 1" in res
    assert "https://example1.com" in res
    assert "This is the first mock search result snippet." in res
    assert "Example 2" in res
    assert "https://example2.com" in res


def test_web_search_empty_or_blocked(monkeypatch):
    def _mock_blocked(req, timeout=None):
        return FakeResponse(b"<html>Blocked or Bot Challenge</html>", {"Content-Type": "text/html"})
    
    import harness.web_tools as _wt; monkeypatch.setattr(_wt, "_safe_urlopen", _mock_blocked)
    res = web_search("blocked query")
    assert "No results found" in res


def test_web_fetch_html(mock_urlopen):
    res = web_fetch("http://testpage.com")
    assert "Main Heading" in res
    assert "This is a paragraph" in res
    assert "color: red" not in res
    assert "console.log" not in res


def test_web_fetch_json(mock_urlopen):
    res = web_fetch("http://testjson.com")
    assert '{"key": "value"}' in res


def test_read_pdf_path_confinement():
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        assert is_safe_path(os.path.join(real_tmp, "foo.pdf"), real_tmp) is True
        assert is_safe_path(os.path.join(real_tmp, "../outside.pdf"), real_tmp) is False
        assert is_safe_path("/etc/passwd", real_tmp) is False


def test_read_pdf_confinement_logic():
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        res = read_pdf("../outside.pdf", workspace_repo=real_tmp)
        assert "Path traversal attempt rejected" in res


def test_read_pdf_extraction(monkeypatch):
    class FakePdfPage:
        def __init__(self, text):
            self.text = text
        def extract_text(self):
            return self.text

    class FakePdfReader:
        def __init__(self, path_or_stream):
            self.pages = [
                FakePdfPage("First page text content."),
                FakePdfPage("Second page text content.")
            ]

    from types import ModuleType
    fake_pypdf = ModuleType("pypdf")
    setattr(fake_pypdf, "PdfReader", FakePdfReader)
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(b"%PDF-1.4 fake")
        tmp_path = tmp.name

    try:
        res = read_pdf(tmp_path)
        assert "First page text content." in res
        assert "Second page text content." in res
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_read_pdf_missing_pypdf(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)
    
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(b"%PDF-1.4 fake")
        tmp_path = tmp.name

    try:
        res = read_pdf(tmp_path)
        assert "pypdf library is not installed" in res
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_web_fetch_pdf_routing(mock_urlopen, monkeypatch):
    class FakePdfPage:
        def extract_text(self):
            return "This is text extracted from a routed PDF."

    class FakePdfReader:
        def __init__(self, path_or_stream):
            self.pages = [FakePdfPage()]

    monkeypatch.setitem(sys.modules, "pypdf", None)
    res = web_fetch("http://testpdf.com")
    assert "pypdf library is not installed" in res

    from types import ModuleType
    fake_pypdf = ModuleType("pypdf")
    setattr(fake_pypdf, "PdfReader", FakePdfReader)
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    res = web_fetch("http://testpdf.com")
    assert "This is text extracted from a routed PDF." in res
