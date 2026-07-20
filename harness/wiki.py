from __future__ import annotations

"""Portable-LLM-Wiki integration (durable KNOWLEDGE state, out of the box).

The harness kernel already keeps durable EXECUTION state (PM's store). This wires
the harness to a portable-llm-wiki instance so investigation findings/decisions
become durable KNOWLEDGE that compounds across sessions and is queryable by any
LLM later -- the same durable-state thesis, one layer up.

Design decision (INTEGRATE, not rebuild): we point at an EXISTING wiki via its
HTTP API (POST /owner/ingest), reusing everything already built and deployed
(interlinking, share tiers, the /llm handshake). We do NOT reimplement the wiki.

Config (env or HarnessConfig):
  HARNESS_WIKI_URL    base URL of the wiki backend (e.g. http://127.0.0.1:8000)
  HARNESS_WIKI_TOKEN  owner bearer token (required to ingest)
  HARNESS_WIKI_AUTO   "1" to auto-ingest a session digest when a pilot turn ends
  HARNESS_WIKI_SUBDIR raw/ subdir (default "conversations")

Auto-ingest is OFF by default and never fires the (token-spending) orchestrator
unless explicitly asked -- mirrors the careful default elsewhere.
"""

import json
import os
import re
import time
import ipaddress
import urllib.request
import urllib.parse
import urllib.error
from dataclasses import dataclass
from typing import Optional

from .diag import note as _diag


def _wiki_base_url_allowed(url: str) -> bool:
    """Accept https anywhere, or http only to loopback (local wiki backend)."""
    if not url:
        return True
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    scheme = parsed.scheme.lower()
    if scheme == "https":
        return True
    if scheme != "http":
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in {"localhost", "ip6-localhost", "ip6-loopback"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class WikiResult:
    ok: bool
    rel_path: str = ""
    error: str = ""
    status: int = 0


class WikiClient:
    def __init__(self, base_url: str = "", token: str = "",
                 subdir: str = "conversations", timeout: int = 20) -> None:
        # Owner/gated surface (same as the portable-llm-wiki MCP uses): WIKI_API_BASE +
        # WIKI_OWNER_TOKEN reach the tenant manifest/graph behind the share-tier gating.
        # Fall back to the public HARNESS_WIKI_URL / HARNESS_WIKI_TOKEN.
        self.base_url = (base_url or os.environ.get("WIKI_API_BASE", "") or os.environ.get("HARNESS_WIKI_URL", "")).rstrip("/")
        if self.base_url and not _wiki_base_url_allowed(self.base_url):
            _diag("wiki.insecure_base_url", msg=self.base_url)
            self.base_url = ""
        self.token = token or os.environ.get("WIKI_OWNER_TOKEN", "") or os.environ.get("HARNESS_WIKI_TOKEN", "")
        self.subdir = subdir or os.environ.get("HARNESS_WIKI_SUBDIR", "conversations")
        self.timeout = timeout

    def _auth_headers(self, extra: dict | None = None) -> dict:
        """Headers for wiki reads: Bearer + X-Share-Token.

        Owner tokens authenticate via Bearer; personal LLM share tokens are
        accepted on either Bearer or X-Share-Token. Send both so a share token
        never silently degrades to public tier.
        """
        headers = {"Accept": "application/json"}
        if extra:
            headers.update(extra)
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-Share-Token"] = self.token
        return headers

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def health(self) -> bool:
        if not self.base_url:
            return False
        try:
            req = urllib.request.Request(f"{self.base_url}/healthz")
            with urllib.request.urlopen(req, timeout=6) as r:
                return r.status == 200
        except Exception:
            return False

    def manifest_meta(self) -> dict:
        """Best-effort page_count / viewer_tier / viewer_is_owner from manifest.

        Hosted portablellm.wiki returns public-tier pages when the owner token
        is missing; page_count from the manifest is the authoritative count
        (not len(graph nodes) after a partial neighborhood fetch).
        """
        out = {
            "page_count": None,
            "viewer_tier": None,
            "viewer_is_owner": None,
        }
        if not self.base_url:
            return out
        try:
            url = f"{self.base_url}/wiki/manifest.json"
            headers = self._auth_headers()
            req = urllib.request.Request(url, method="GET", headers=headers)
            with urllib.request.urlopen(req, timeout=min(self.timeout, 8)) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            if not isinstance(data, dict):
                return out
            if "page_count" in data:
                try:
                    out["page_count"] = int(data["page_count"])
                except (TypeError, ValueError):
                    pass
            elif isinstance(data.get("pages"), list):
                out["page_count"] = len(data["pages"])
            tier = data.get("viewer_tier") or data.get("tier")
            if isinstance(tier, str) and tier.strip():
                out["viewer_tier"] = tier.strip()
            vio = data.get("viewer_is_owner")
            if isinstance(vio, bool):
                out["viewer_is_owner"] = vio
        except Exception:
            return out
        return out

    def _tier_caveat(self) -> str:
        """One-line warning when manifest reports public-tier visibility."""
        try:
            meta = self.manifest_meta()
            tier = (meta.get("viewer_tier") or "").strip().lower()
            if tier != "public":
                return ""
            if self.token:
                return (
                    "WARNING: wiki is answering at public tier only (token present but not elevating). "
                    "Treat results as public; reconnect/paste a personal LLM URL or owner token for private pages."
                )
            return (
                "WARNING: wiki is answering at public tier only. "
                "Treat results as public; connect with a personal LLM URL or owner token for private pages."
            )
        except Exception:
            return ""

    def _prepend_tier_caveat(self, answer: str) -> str:
        caveat = self._tier_caveat()
        if not caveat:
            return answer
        return f"{caveat}\n\n{answer}"

    def ingest(self, slug: str, content: str, *, note: str = "",
               run_orchestrator: bool = False) -> WikiResult:
        """Ingest a markdown source into the wiki (POST /owner/ingest)."""
        if not self.configured:
            return WikiResult(False, error="wiki not configured (set HARNESS_WIKI_URL + HARNESS_WIKI_TOKEN)")
        body = json.dumps({
            "slug": _safe_slug(slug), "content": content, "subdir": self.subdir,
            "note": note, "run_orchestrator": bool(run_orchestrator),
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/owner/ingest", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode())
            return WikiResult(True, rel_path=data.get("rel_path", ""), status=r.status)
        except urllib.error.HTTPError as e:
            return WikiResult(False, error=f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}",
                              status=e.code)
        except Exception as e:
            return WikiResult(False, error=repr(e))

    def search_pages(self, query: str, *, limit: int = 5) -> list[dict]:
        """Retrieval-only wiki search (GET /wiki/search). No RAG LLM call.

        Returns a list of ``{"title", "slug", "snippet"}`` dicts. Never raises.
        """
        if not self.configured:
            return []
        q = (query or "").strip()
        if not q:
            return []
        try:
            url = f"{self.base_url}/wiki/search?q=" + urllib.parse.quote(q)
            headers = self._auth_headers()
            req = urllib.request.Request(url, method="GET", headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                if r.status != 200:
                    return []
                data = json.loads(r.read().decode("utf-8", "replace"))
            results = data.get("results", []) if isinstance(data, dict) else []
            hits: list[dict] = []
            for hit in results[: max(1, int(limit))]:
                if not isinstance(hit, dict):
                    continue
                slug = str(hit.get("slug") or "")
                title = str(hit.get("title") or slug)
                snippet = (
                    hit.get("snippet")
                    or hit.get("description")
                    or hit.get("body")
                    or ""
                )
                hits.append({
                    "title": title,
                    "slug": slug,
                    "snippet": str(snippet).strip(),
                })
            return hits
        except Exception:
            return []

    def query(self, question: str) -> str:
        """Query the wiki's LLM query/search surface.

        Uses the portable-llm-wiki public read API, verified against its OpenAPI
        spec: POST /wiki/query (RAG answer), then POST /wiki/chat, then the
        GET /wiki/search retrieval fallback. If those all fail, fall back to
        the manifest index summary. Cap result to ~4000 chars.
        """
        if not self.configured:
            return "wiki not configured"

        # Real portable-llm-wiki endpoints (see GET /openapi.json).
        endpoints = [
            ("/wiki/query", "POST", {"question": question}),
            ("/wiki/chat", "POST", {"message": question}),
        ]
        # The RAG answer endpoint invokes an LLM server-side, so it can be slow;
        # give it more room than the default (health/ingest) timeout.
        query_timeout = max(self.timeout, 60)

        for path, method, payload in endpoints:
            url = f"{self.base_url}{path}"
            headers = self._auth_headers(extra={
                "Content-Type": "application/json",
            })

            body = json.dumps(payload).encode()
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=query_timeout) as r:
                    if r.status == 200:
                        res = r.read().decode("utf-8", "replace")
                        try:
                            data = json.loads(res)
                            # Extract answer from response
                            if isinstance(data, dict):
                                answer = (data.get("answer") or data.get("response") or 
                                          data.get("result") or data.get("content"))
                                if answer:
                                    return self._prepend_tier_caveat(str(answer)[:4000])
                        except Exception:
                            pass
                        # If raw string returned, return it
                        return self._prepend_tier_caveat(res[:4000])
            except Exception:
                continue

        # Retrieval fallback: GET /wiki/search returns matching pages even when
        # the RAG/chat LLM surface is unavailable.
        try:
            url = f"{self.base_url}/wiki/search?q=" + urllib.parse.quote(question)
            headers = self._auth_headers()
            req = urllib.request.Request(url, method="GET", headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                if r.status == 200:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                    results = data.get("results", []) if isinstance(data, dict) else []
                    if results:
                        lines = ["Wiki search results:"]
                        for hit in results[:8]:
                            if isinstance(hit, dict):
                                title = hit.get("title") or hit.get("slug", "")
                                slug = hit.get("slug", "")
                                snip = hit.get("snippet") or hit.get("description") or ""
                                lines.append(f"- {title} ({slug}): {snip}")
                        return self._prepend_tier_caveat("\n".join(lines)[:4000])
        except Exception:
            pass

        # Fallback to fetching manifest + returning a helpful summary if no query endpoint succeeded
        try:
            url = f"{self.base_url}/wiki/manifest.json"
            headers = self._auth_headers()
            req = urllib.request.Request(url, method="GET", headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                if r.status == 200:
                    manifest = json.loads(r.read().decode("utf-8", "replace"))
                    pages = manifest.get("pages", []) if isinstance(manifest, dict) else []
                    summary_lines = ["No direct wiki query endpoint succeeded. Fallback wiki index summary:"]
                    for p in pages[:15]:
                        if isinstance(p, dict):
                            slug = p.get("slug", "")
                            title = p.get("title", slug)
                            desc = p.get("description") or p.get("note") or ""
                            summary_lines.append(f"- {title} ({slug}): {desc}")
                    return self._prepend_tier_caveat("\n".join(summary_lines)[:4000])
        except Exception as e:
            return f"wiki query failed and fallback failed: {repr(e)}"

        return "wiki query returned empty result"

    def graph(self) -> dict:
        """Fetch the wiki graph via the gated owner surface the portable-llm-wiki
        MCP uses. Newer wiki backends expose GET /wiki/graph directly; older
        builds need GET /wiki/manifest.json plus per-page neighborhoods.
        Returns: {"nodes": [...], "edges": [...], "error": Optional[str]}
        """
        if not self.base_url:
            return {"nodes": [], "edges": [], "error": "Wiki base URL not set"}

        def _get(path):
            url = f"{self.base_url}{path}"
            headers = self._auth_headers()
            req = urllib.request.Request(url, method="GET", headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                if r.status != 200:
                    raise RuntimeError(f"{path} status {r.status}")
                return json.loads(r.read().decode("utf-8", "replace"))

        # Preferred current portable-llm-wiki endpoint: one request returns the
        # full owner-visible graph. This is faster and avoids looking
        # "disconnected" when per-slug neighborhood routes differ by backend
        # version.
        try:
            direct = _get("/wiki/graph")
            parsed = parse_graph_from_response(direct)
            if parsed.get("nodes") or parsed.get("edges"):
                parsed["error"] = None
                return parsed
        except Exception:
            pass

        # Legacy fallback: nodes from the manifest.
        try:
            manifest = _get("/wiki/manifest.json")
        except Exception as e:
            return {"nodes": [], "edges": [], "error": f"manifest fetch failed: {repr(e)}"}

        pages = manifest.get("pages", []) if isinstance(manifest, dict) else []
        nodes = []
        slugs = []
        for p in pages:
            if not isinstance(p, dict):
                continue
            slug = p.get("slug")
            if not slug:
                continue
            slugs.append(slug)
            nodes.append({
                "id": slug,
                "title": p.get("title") or slug,
                "section": p.get("section"),
                "tags": p.get("tags"),
            })

        # 2. edges via the per-slug graph neighborhood (1 hop), de-duplicated +
        # undirected-deduped. The manifest already proved the wiki is reachable
        # (nodes are populated), so edges are BEST-EFFORT: we use a short per-call
        # timeout and a hard time budget so a large wiki can't make Refresh hang
        # or time out and look "disconnected". Partial edges are fine.
        # Fetch the per-slug neighborhoods CONCURRENTLY within the same time
        # budget. Serial fetching meant a large wiki spent the whole budget on the
        # first few slugs and returned mostly-empty edges (looked "disconnected").
        # A small pool collects real edges from many slugs inside the 6s window;
        # the per-request timeout still keeps one slow page from stalling refresh.
        import time as _t
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from concurrent.futures import TimeoutError as _FutureTimeout

        edges = []
        seen = set()
        node_ids = set(slugs)
        _edge_deadline = _t.monotonic() + min(float(self.timeout), 6.0)
        _edge_timeout = 2.5  # per-request; keep one slow page from stalling refresh

        def _fetch_neighborhood(slug):
            url = f"{self.base_url}/wiki/graph/{urllib.parse.quote(slug)}?hops=1"
            headers = self._auth_headers()
            req = urllib.request.Request(url, method="GET", headers=headers)
            with urllib.request.urlopen(req, timeout=_edge_timeout) as r:
                if r.status != 200:
                    return {}
                return json.loads(r.read().decode("utf-8", "replace"))

        executor = ThreadPoolExecutor(max_workers=min(8, max(1, len(slugs))))
        try:
            futures = {executor.submit(_fetch_neighborhood, s): s for s in slugs}
            try:
                for fut in as_completed(futures, timeout=max(0.0, _edge_deadline - _t.monotonic())):
                    try:
                        g = fut.result()
                    except Exception as e:
                        _diag("wiki.edge_fetch", e, msg=f"slug={futures.get(fut)}")
                        continue
                    for e in (g.get("edges", []) if isinstance(g, dict) else []):
                        if not isinstance(e, dict):
                            continue
                        src = e.get("source"); tgt = e.get("target")
                        if not src or not tgt or src not in node_ids or tgt not in node_ids:
                            continue
                        key = tuple(sorted((src, tgt)))
                        if key in seen:
                            continue
                        seen.add(key)
                        edges.append({"source": src, "target": tgt})
            except _FutureTimeout:
                pass  # time budget spent -> return the edges gathered so far (still "ok")
        finally:
            # Don't block the response waiting on stragglers; their 2.5s socket
            # timeout bounds them and the pool threads exit on their own.
            executor.shutdown(wait=False, cancel_futures=True)

        return {"nodes": nodes, "edges": edges, "error": None}

def parse_graph_from_response(data) -> dict:
    # If it is already a dict with nodes and edges
    if isinstance(data, dict) and "nodes" in data:
        # It's already in a graph-like format!
        raw_nodes = data.get("nodes") or []
        raw_edges = data.get("edges") or []
        nodes = []
        edges = []
        # Normalize nodes
        if isinstance(raw_nodes, list):
            for n in raw_nodes:
                if not isinstance(n, dict):
                    continue
                node_id = n.get("id") or n.get("slug")
                if not node_id:
                    continue
                nodes.append({
                    "id": node_id,
                    "title": n.get("title") or node_id,
                    "section": n.get("section"),
                    "tags": n.get("tags")
                })
        elif isinstance(raw_nodes, dict):
            for node_id, n in raw_nodes.items():
                if not isinstance(n, dict):
                    n = {"title": str(n)}
                nodes.append({
                    "id": node_id,
                    "title": n.get("title") or node_id,
                    "section": n.get("section"),
                    "tags": n.get("tags")
                })
        # Normalize edges
        if isinstance(raw_edges, list):
            for e in raw_edges:
                if not isinstance(e, dict):
                    continue
                src = e.get("source") or e.get("from")
                tgt = e.get("target") or e.get("to")
                if src and tgt:
                    edges.append({"source": src, "target": tgt})
        return {"nodes": nodes, "edges": edges}

    # If it is a list of pages (or a dict of pages)
    pages = []
    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        if "pages" in data and isinstance(data["pages"], list):
            pages = data["pages"]
        elif "pages" in data and isinstance(data["pages"], dict):
            # dict of pages
            for k, v in data["pages"].items():
                if isinstance(v, dict):
                    if "slug" not in v and "id" not in v:
                        v["slug"] = k
                    pages.append(v)
        else:
            # Maybe the top-level dict is a dict of pages (slug -> page_data)
            for k, v in data.items():
                if isinstance(v, dict):
                    if "slug" not in v and "id" not in v:
                        v["slug"] = k
                    pages.append(v)

    nodes = []
    edges = []
    seen_edges = set()

    for page in pages:
        if not isinstance(page, dict):
            continue
        page_id = page.get("slug") or page.get("id")
        if not page_id:
            continue
        nodes.append({
            "id": page_id,
            "title": page.get("title") or page_id,
            "section": page.get("section"),
            "tags": page.get("tags")
        })

        # Look for explicit links/references
        links = []
        for key in ["links", "references", "targets", "wikilinks", "refs", "out_links", "outbound"]:
            if key in page and isinstance(page[key], list):
                for l in page[key]:
                    if isinstance(l, str):
                        links.append(l)
                    elif isinstance(l, dict):
                        target_id = l.get("slug") or l.get("id") or l.get("target")
                        if target_id:
                            links.append(target_id)
                break

        # Also look in content/body for [[wikilinks]] if present
        content = page.get("content") or page.get("body") or ""
        if isinstance(content, str) and content:
            found = re.findall(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", content)
            for f in found:
                links.append(f.strip())

        for link in links:
            link_slug = _safe_slug(link)
            edge_key = (page_id, link_slug)
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                edges.append({"source": page_id, "target": link_slug})

    return {"nodes": nodes, "edges": edges}


def _safe_slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return (s or "harness-session")[:120]


def session_digest(user_message: str, pilot_messages: list, artifacts: list) -> str:
    """Render a compact markdown digest of a pilot session turn for ingest.
    Findings/decisions become durable knowledge; raw transcript is summarized."""
    lines = ["# Harness Session Findings", ""]
    lines.append(f"**Question:** {user_message}".strip())
    lines.append("")
    if pilot_messages:
        lines.append("## Pilot summary")
        for m in pilot_messages[-3:]:
            lines.append(f"- {m.strip()}")
        lines.append("")
    if artifacts:
        lines.append("## Findings (durable)")
        seen = set()
        for a in artifacts:
            head = (a.get("headline") or "").strip()
            if not head or head in seen:
                continue
            seen.add(head)
            lines.append(f"- [{a.get('type','finding')}] {head}")
        lines.append("")
    lines.append(f"_Captured by pm-harness on {time.strftime('%Y-%m-%d')}._")
    return "\n".join(lines)
