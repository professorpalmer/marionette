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
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional


@dataclass
class WikiResult:
    ok: bool
    rel_path: str = ""
    error: str = ""
    status: int = 0


class WikiClient:
    def __init__(self, base_url: str = "", token: str = "",
                 subdir: str = "conversations", timeout: int = 20) -> None:
        self.base_url = (base_url or os.environ.get("HARNESS_WIKI_URL", "")).rstrip("/")
        self.token = token or os.environ.get("HARNESS_WIKI_TOKEN", "")
        self.subdir = subdir or os.environ.get("HARNESS_WIKI_SUBDIR", "conversations")
        self.timeout = timeout

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
