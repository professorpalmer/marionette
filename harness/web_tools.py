from __future__ import annotations

import http.client
import os
import re
import socket
import urllib.request
import urllib.parse
import urllib.error
import html.parser
from typing import Optional

from harness.url_safety import is_safe_url_pinned, normalize_url_for_request
from harness.paths import path_within

WEB_FETCH_LIMIT = 16000
WEB_FETCH_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB


class _PinnedIP:
    """Mutable pinned-IP holder shared by redirect + transport handlers.

    Redirects re-resolve and update ``.ip`` so the next hop connects to the
    newly validated address rather than the original pin (DNS-rebinding /
    cross-host redirect safety).
    """

    __slots__ = ("ip",)

    def __init__(self, ip: Optional[str] = None):
        self.ip = ip


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate EVERY redirect hop with is_safe_url_pinned.

    urlopen()'s default opener silently follows 30x redirects, so a
    safe-looking URL can 302 to an internal address (cloud metadata
    169.254.169.254, localhost, RFC-1918) and defeat the initial check.
    Each hop is re-validated with a fresh DNS resolve; when a shared
    ``_PinnedIP`` is present the pin is updated so the next connection
    matches the validated target.
    """

    def __init__(self, pin: Optional[_PinnedIP] = None, *args, **kwargs):
        self._pin = pin
        super().__init__(*args, **kwargs)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, reason, pinned_ip = is_safe_url_pinned(newurl)
        if not ok:
            raise urllib.error.HTTPError(
                newurl, code, f"unsafe redirect target ({reason})", headers, fp
            )
        if self._pin is not None and pinned_ip:
            self._pin.ip = pinned_ip
        # Normalize the vetted target the same way direct fetches are normalized.
        newurl = normalize_url_for_request(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Module-level opener that re-checks redirect hops for SSRF safety. Used for all
# outbound fetches instead of the bare urllib.request.urlopen.
_SAFE_OPENER = urllib.request.build_opener(_SafeRedirectHandler)


class _PinnedIPHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects to *pinned_ip* instead of the hostname.

    The original hostname is kept for the Host header (set automatically by
    http.client based on ``self.host``).
    """

    def __init__(self, *args, pinned_ip=None, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def connect(self):
        if self._pinned_ip:
            self.sock = socket.create_connection(
                (self._pinned_ip, self.port), self.timeout, self.source_address,
            )
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        else:
            super().connect()


class _PinnedIPHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that connects to *pinned_ip* instead of the hostname.

    The original hostname is kept for the Host header (auto-set by http.client)
    and for TLS SNI / certificate verification via *server_hostname*.
    """

    def __init__(self, *args, pinned_ip=None, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def connect(self):
        if self._pinned_ip:
            self.sock = socket.create_connection(
                (self._pinned_ip, self.port), self.timeout, self.source_address,
            )
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self._tunnel_host:
                self._tunnel()
            # Use the *original* hostname for TLS SNI / cert verification
            self.sock = self._context.wrap_socket(
                self.sock, server_hostname=self.host,
            )
        else:
            super().connect()


class _PinnedIPHTTPHandler(urllib.request.HTTPHandler):
    """HTTPHandler that injects a pinned-IP transport."""

    def __init__(self, pin: Optional[_PinnedIP] = None, *args, **kwargs):
        self._pin = pin if pin is not None else _PinnedIP()
        super().__init__(*args, **kwargs)

    def http_open(self, req):
        return self.do_open(
            lambda *a, **kw: _PinnedIPHTTPConnection(
                *a, pinned_ip=self._pin.ip, **kw
            ),
            req,
        )


class _PinnedIPHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPSHandler that injects a pinned-IP transport."""

    def __init__(self, pin: Optional[_PinnedIP] = None, *args, **kwargs):
        self._pin = pin if pin is not None else _PinnedIP()
        super().__init__(*args, **kwargs)

    def https_open(self, req):
        return self.do_open(
            lambda *a, **kw: _PinnedIPHTTPSConnection(
                *a, pinned_ip=self._pin.ip, **kw
            ),
            req,
        )


def _make_pinned_opener(pinned_ip: str):
    """Build an opener that connects to *pinned_ip* while preserving the
    original hostname in HTTP Host headers and HTTPS SNI / certificate
    verification. Redirect hops re-validate with is_safe_url_pinned and
    update the shared pin so validation and connection always match.
    """
    pin = _PinnedIP(pinned_ip)
    return urllib.request.build_opener(
        _PinnedIPHTTPHandler(pin=pin),
        _PinnedIPHTTPSHandler(pin=pin),
        _SafeRedirectHandler(pin=pin),
    )


def _safe_urlopen(req, timeout, pinned_ip=None):
    """urlopen replacement that validates redirect targets (SSRF guard).

    When *pinned_ip* is provided the opener connects to that IP instead of
    re-resolving the hostname, closing the TOCTOU DNS-rebinding window.
    """
    if pinned_ip:
        opener = _make_pinned_opener(pinned_ip)
        return opener.open(req, timeout=timeout)
    return _SAFE_OPENER.open(req, timeout=timeout)


def github_fetch_candidates(url: str) -> list[str]:
    """Rewrite a GitHub web-UI URL to raw.githubusercontent.com candidates.

    The github.com HTML pages are almost entirely navigation chrome, so fetching
    them for their text yields noise and blows the truncation budget. The raw
    host returns the actual file, which is what a fetch of a repo/file wants.
    Returns an ordered list to try; the original URL is always the last fallback.
    """
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+)(/.*)?$", url)
    if not match:
        return [url]
    owner, repo = match.group(1), match.group(2)
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    rest = (match.group(3) or "").rstrip("/")

    file_match = re.match(r"/(?:blob|raw)/([^/]+)/(.+)$", rest)
    if file_match:
        ref, path = file_match.group(1), file_match.group(2)
        return [f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}", url]

    tree_match = re.match(r"/tree/([^/]+)/?$", rest)
    refs = [tree_match.group(1)] if tree_match else ["main", "master"]
    candidates = [
        f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/README.md" for ref in refs
    ]
    candidates.append(url)
    return candidates


def is_safe_path(path: str, parent: str) -> bool:
    """True if ``path`` is inside ``parent`` (the root itself counts as safe).
    Shared confinement primitive; see harness.paths."""
    return path_within(path, parent, allow_equal=True)


class DDGParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self.current_result = None
        self.stack = []  # stack of (tag, attrs_dict)

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        self.stack.append((tag, attrs_dict))
        cls = str(attrs_dict.get("class") or "")

        # html.duckduckgo.com check
        if tag == "div" and "result" in cls.split():
            if self.current_result:
                self.results.append(self.current_result)
            self.current_result = {"title": "", "url": "", "snippet": "", "_title_chunks": [], "_snippet_chunks": []}
        # lite.duckduckgo.com check
        elif tag == "table" and "result-table" in cls.split():
            if self.current_result:
                self.results.append(self.current_result)
            self.current_result = {"title": "", "url": "", "snippet": "", "_title_chunks": [], "_snippet_chunks": []}

        if self.current_result:
            if tag == "a":
                href = str(attrs_dict.get("href") or "")
                if href.startswith("/l/") or "uddg=" in href:
                    parsed = urllib.parse.urlparse(href)
                    qs = urllib.parse.parse_qs(str(parsed.query))
                    if "uddg" in qs:
                        href = qs["uddg"][0]
                if "result__a" in cls.split() or "result-link" in cls.split():
                    self.current_result["url"] = href

    def handle_data(self, data):
        if not self.current_result or not self.stack:
            return
        
        for tag, attrs in reversed(self.stack):
            cls = attrs.get("class", "")
            if tag == "a" and ("result__a" in cls.split() or "result-link" in cls.split()):
                self.current_result["_title_chunks"].append(data)
                break
            elif "result__snippet" in cls.split() or "result-snippet" in cls.split():
                self.current_result["_snippet_chunks"].append(data)
                break

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()

    def get_results(self):
        if self.current_result and self.current_result not in self.results:
            self.results.append(self.current_result)
        
        final_results = []
        for r in self.results:
            title = "".join(r.get("_title_chunks", [])).strip()
            snippet = "".join(r.get("_snippet_chunks", [])).strip()
            if title or r.get("url"):
                final_results.append({
                    "title": title or "No Title",
                    "url": r.get("url", ""),
                    "snippet": snippet or "No Snippet"
                })
        return final_results


class HTMLToTextParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.ignore_stack = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "noscript", "iframe"):
            self.ignore_stack.append(tag)
        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "li"):
            self.text_parts.append("\n")
        elif tag == "br":
            self.text_parts.append("\n")

    def handle_data(self, data):
        if not self.ignore_stack:
            self.text_parts.append(data)

    def handle_endtag(self, tag):
        if self.ignore_stack and self.ignore_stack[-1] == tag:
            self.ignore_stack.pop()
        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "li"):
            self.text_parts.append("\n")

    def get_text(self) -> str:
        raw_text = "".join(self.text_parts)
        lines = []
        for line in raw_text.splitlines():
            cleaned = line.strip()
            if cleaned:
                lines.append(cleaned)
        return "\n".join(lines)


def web_search(query: str, timeout: int = 10) -> str:
    try:
        query_encoded = urllib.parse.quote_plus(query)
        url = f"https://html.duckduckgo.com/html/?q={query_encoded}"
        ok, reason, pinned_ip = is_safe_url_pinned(url)
        if not ok:
            return f"Refused to search the web: unsafe URL ({reason})."
        url = normalize_url_for_request(url)
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        })
        with _safe_urlopen(req, timeout, pinned_ip=pinned_ip) as response:
            # Cap the read to WEB_FETCH_MAX_BYTES to prevent memory exhaustion.
            raw_bytes = response.read(WEB_FETCH_MAX_BYTES + 1)
            if len(raw_bytes) > WEB_FETCH_MAX_BYTES:
                raw_bytes = raw_bytes[:WEB_FETCH_MAX_BYTES]
                html_content = raw_bytes.decode("utf-8", errors="replace")
                html_content += f"\n\n... (truncated to {WEB_FETCH_MAX_BYTES} bytes)"
            else:
                html_content = raw_bytes.decode("utf-8", errors="replace")
        
        parser = DDGParser()
        parser.feed(html_content)
        parser.close()
        results = parser.get_results()[:5]
        if not results:
            return "No results found. (DuckDuckGo may have rate-limited or blocked this request. Please try again or use another search query.)"
        
        lines = []
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. Title: {r['title']}")
            lines.append(f"   URL: {r['url']}")
            lines.append(f"   Snippet: {r['snippet']}")
            lines.append("")
        return "\n".join(lines).strip()
    except Exception as e:
        return f"Error searching the web: {e}"


def _truncate(text: str, source_url: str) -> str:
    if len(text) <= WEB_FETCH_LIMIT:
        return text
    hint = (
        f"\n\n... (truncated to {WEB_FETCH_LIMIT} chars). To read more, fetch a more "
        "specific URL -- e.g. a raw file on raw.githubusercontent.com, or a "
        "documentation subpage -- rather than re-fetching the same page."
    )
    return text[:WEB_FETCH_LIMIT] + hint


def _fetch_one(url: str, timeout: int) -> str:
    ok, reason, pinned_ip = is_safe_url_pinned(url)
    if not ok:
        return f"Refused to fetch web page: unsafe URL ({reason})."
    url = normalize_url_for_request(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    })
    with _safe_urlopen(req, timeout, pinned_ip=pinned_ip) as response:
        content_type = response.headers.get("Content-Type", "").lower()

        if "application/pdf" in content_type or url.lower().split("?")[0].endswith(".pdf"):
            # Cap the read to WEB_FETCH_MAX_BYTES to prevent memory exhaustion.
            pdf_data = response.read(WEB_FETCH_MAX_BYTES + 1)
            if len(pdf_data) > WEB_FETCH_MAX_BYTES:
                return f"Refused to fetch PDF: exceeds {WEB_FETCH_MAX_BYTES // 1024 // 1024} MiB size cap."
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(pdf_data)
                tmp_path = tmp.name
            try:
                return read_pdf(tmp_path)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

        # Cap the read to WEB_FETCH_MAX_BYTES to prevent memory exhaustion.
        raw_bytes = response.read(WEB_FETCH_MAX_BYTES + 1)
        truncation_note = ""
        if len(raw_bytes) > WEB_FETCH_MAX_BYTES:
            raw_bytes = raw_bytes[:WEB_FETCH_MAX_BYTES]
            truncation_note = f"\n\n... (truncated to {WEB_FETCH_MAX_BYTES} bytes)"

        if "application/json" in content_type:
            text = raw_bytes.decode("utf-8", errors="replace") + truncation_note
            return _truncate(text, url)

        # Raw text/markdown (e.g. raw.githubusercontent.com) needs no HTML parse.
        is_markup = "text/html" in content_type or "xml" in content_type
        text_body = raw_bytes.decode("utf-8", errors="replace") + truncation_note
        if not is_markup:
            return _truncate(text_body, url)

        parser = HTMLToTextParser()
        parser.feed(text_body)
        parser.close()
        return _truncate(parser.get_text(), url)


def web_fetch(url: str, timeout: int = 12) -> str:
    candidates = github_fetch_candidates(url)
    last_error = None
    for candidate in candidates:
        try:
            text = _fetch_one(candidate, timeout)
            if text and text.strip():
                return text
        except Exception as e:  # try the next candidate (e.g. 404 on main -> master)
            last_error = e
    return f"Error fetching web page: {last_error}" if last_error else "Error fetching web page: empty response"


def read_pdf(path_or_url: str, workspace_repo: Optional[str] = None) -> str:
    if path_or_url.startswith(("http://", "https://")):
        ok, reason, pinned_ip = is_safe_url_pinned(path_or_url)
        if not ok:
            return f"Refused to fetch PDF: unsafe URL ({reason})."
        try:
            req = urllib.request.Request(path_or_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
            })
            with _safe_urlopen(req, 12, pinned_ip=pinned_ip) as response:
                # Cap the read to WEB_FETCH_MAX_BYTES to prevent memory exhaustion.
                pdf_data = response.read(WEB_FETCH_MAX_BYTES + 1)
                if len(pdf_data) > WEB_FETCH_MAX_BYTES:
                    return f"Refused to fetch PDF: exceeds {WEB_FETCH_MAX_BYTES // 1024 // 1024} MiB size cap."
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(pdf_data)
                tmp_path = tmp.name
            try:
                text = read_pdf(tmp_path, workspace_repo=None)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            return text
        except Exception as e:
            return f"Error downloading/reading remote PDF: {e}"

    if workspace_repo:
        target_path = path_or_url
        if not os.path.isabs(target_path):
            target_path = os.path.join(workspace_repo, target_path)
        if not is_safe_path(target_path, workspace_repo):
            return f"Error: Path traversal attempt rejected: {path_or_url}"
        path_or_url = target_path

    try:
        import pypdf
    except ImportError:
        return "Error: pypdf library is not installed."

    try:
        if not os.path.exists(path_or_url):
            return f"Error: File not found: {path_or_url}"
        if os.path.isdir(path_or_url):
            return f"Error: Path is a directory: {path_or_url}"

        reader = pypdf.PdfReader(path_or_url)
        text_parts = []
        total_chars = 0
        is_truncated = False
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text() or ""
            if total_chars + len(page_text) > 12000:
                allowed_len = 12000 - total_chars
                text_parts.append(page_text[:allowed_len])
                is_truncated = True
                break
            text_parts.append(page_text)
            total_chars += len(page_text)
        
        extracted = "\n".join(text_parts)
        if is_truncated:
            extracted += "\n\n... (PDF content truncated to 12000 characters) ..."
        return extracted
    except Exception as e:
        return f"Error extracting PDF: {e}"
