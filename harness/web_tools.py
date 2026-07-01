from __future__ import annotations

import os
import re
import urllib.request
import urllib.parse
import html.parser
from typing import Optional

from harness.url_safety import is_safe_url, normalize_url_for_request

WEB_FETCH_LIMIT = 16000


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
    try:
        real_p = os.path.realpath(path)
        real_parent = os.path.realpath(parent)
        return os.path.commonpath([real_parent, real_p]) == real_parent
    except ValueError:
        return False


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
        ok, reason = is_safe_url(url)
        if not ok:
            return f"Refused to search the web: unsafe URL ({reason})."
        url = normalize_url_for_request(url)
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        })
        with urllib.request.urlopen(req, timeout=timeout) as response:
            html_content = response.read().decode("utf-8", errors="replace")
        
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
    ok, reason = is_safe_url(url)
    if not ok:
        return f"Refused to fetch web page: unsafe URL ({reason})."
    url = normalize_url_for_request(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    })
    with urllib.request.urlopen(req, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "").lower()

        if "application/pdf" in content_type or url.lower().split("?")[0].endswith(".pdf"):
            pdf_data = response.read()
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(pdf_data)
                tmp_path = tmp.name
            try:
                return read_pdf(tmp_path)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

        raw_bytes = response.read()
        if "application/json" in content_type:
            text = raw_bytes.decode("utf-8", errors="replace")
            return _truncate(text, url)

        # Raw text/markdown (e.g. raw.githubusercontent.com) needs no HTML parse.
        is_markup = "text/html" in content_type or "xml" in content_type
        text_body = raw_bytes.decode("utf-8", errors="replace")
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
        try:
            req = urllib.request.Request(path_or_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
            })
            with urllib.request.urlopen(req, timeout=12) as response:
                pdf_data = response.read()
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
