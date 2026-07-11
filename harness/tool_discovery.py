from __future__ import annotations

"""On-demand tool discovery (OMP-inspired): keep a small core toolset in the
pilot prompt and let the agent search/activate the rest via ``search_tools``.

Uses deterministic stdlib-only BM25-ish ranking over built-in pilot tools plus
connected MCP tool descriptions. Cross-platform: Windows path separators in
MCP metadata are normalized for stable JSON output.
"""

import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set

from .pilot import build_tools_schema

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\(?:[^\\/\s\"']|\\/)+")
_MAX_DESC_CHARS = 160
_DEFAULT_LIMIT = 10
_MAX_LIMIT = 25
_AVG_DOC_LEN = 48.0
_BM25_K1 = 1.2
_BM25_B = 0.75

# Always-visible built-ins (plus ``search_tools`` when discovery is on).
CORE_ALWAYS: Set[str] = {
    "read_file",
    "write_file",
    "edit_file",
    "run_command",
    "list_dir",
    "search_tools",
}


def _core_always() -> Set[str]:
    core = set(CORE_ALWAYS)
    try:
        from .hash_edit import hash_edit_enabled
        if hash_edit_enabled():
            core.add("hash_edit")
    except Exception:
        pass
    return core

# Main interactive pilot extras that stay visible without activation.
# The moat tools (delegation verbs, CodeGraph, wiki) MUST be here: the swarm
# gate blocks broad-intent turns until run_swarm/run_implement/run_parallel is
# dispatched, so hiding those verbs behind search_tools activation deadlocks
# the pilot into suppression loops -- and every model, regardless of tool-use
# quality, should see the durable-orchestration surface out of the gate.
_PILOT_EXTRAS: Set[str] = {
    "search_codegraph",
    "search_files",
    "memory",
    "open_project",
    "relocate_session",
    "session_bank",
    "route_task",
    "run_swarm",
    "run_implement",
    "run_parallel",
    "query_wiki",
}

# Worker leaf extras (no delegation tools).
_WORKER_EXTRAS: Set[str] = {"route_task"}


def core_visible_names(no_delegation: bool = False) -> Set[str]:
    base = _core_always()
    return base | (_WORKER_EXTRAS if no_delegation else _PILOT_EXTRAS)


# Backward-compatible aliases (static; prefer core_visible_names() at runtime).
CORE_PILOT: Set[str] = CORE_ALWAYS | _PILOT_EXTRAS
CORE_WORKER: Set[str] = CORE_ALWAYS | _WORKER_EXTRAS


def discovery_enabled() -> bool:
    """Return False when ``HARNESS_TOOL_DISCOVERY=0|false|no``."""
    raw = os.environ.get("HARNESS_TOOL_DISCOVERY", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _normalize_path_text(text: str) -> str:
    """Normalize Windows paths for stable, cross-platform catalog text."""
    if not text:
        return ""

    def _repl(match: re.Match) -> str:
        return match.group(0).replace("\\", "/")

    return _WIN_PATH_RE.sub(_repl, text.replace("\\", "/"))


def _truncate(text: str, limit: int = _MAX_DESC_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


@dataclass
class CatalogEntry:
    tool_id: str
    name: str
    qualified: str
    description: str
    source: str  # "builtin" | "mcp"
    hidden: bool
    schema_entry: dict = field(repr=False)
    search_text: str = ""

    def to_search_row(self, *, activated: bool) -> dict:
        return {
            "tool_id": self.tool_id,
            "name": self.name,
            "qualified": self.qualified,
            "source": self.source,
            "hidden": self.hidden,
            "activated": activated,
            "description": _truncate(self.description),
        }


@dataclass
class SearchHit:
    entry: CatalogEntry
    score: float


class ToolCatalog:
    """Session-scoped catalog of pilot + MCP tools with on-demand activation."""

    def __init__(self) -> None:
        self._activated: Set[str] = set()
        self._entries: Dict[str, CatalogEntry] = {}
        self._doc_freq: Dict[str, int] = {}
        self._num_docs = 0
        self._last_mcp_tools: Optional[Sequence] = None
        self._last_no_delegation = False
        self._last_browser_enabled = True

    @property
    def activated(self) -> Set[str]:
        return set(self._activated)

    def refresh(
        self,
        *,
        mcp_tools: Optional[Sequence] = None,
        no_delegation: bool = False,
        browser_enabled: bool = True,
    ) -> None:
        """Rebuild the search index from the current built-in + MCP tool set."""
        if mcp_tools is not None:
            self._last_mcp_tools = list(mcp_tools)
        mcp_tools = self._last_mcp_tools
        self._last_no_delegation = no_delegation
        self._last_browser_enabled = browser_enabled
        core = core_visible_names(no_delegation)
        schema = build_tools_schema(
            mcp_tools,
            no_delegation=no_delegation,
            browser_enabled=browser_enabled,
            include_search_tools=discovery_enabled(),
        )
        entries: Dict[str, CatalogEntry] = {}
        for item in schema:
            fn = item.get("function") or {}
            name = fn.get("name") or ""
            if not name:
                continue
            desc = _normalize_path_text(fn.get("description") or "")
            if name.startswith("mcp_"):
                parts = name.split("_", 2)
                server = parts[1] if len(parts) > 1 else ""
                tool_name = parts[2] if len(parts) > 2 else name[4:]
                qualified = f"{server}.{tool_name}"
                tool_id = f"mcp:{qualified}"
                source = "mcp"
            else:
                qualified = name
                tool_id = f"builtin:{name}"
                source = "builtin"
            hidden = discovery_enabled() and name not in core
            search_text = _normalize_path_text(
                " ".join([name, qualified, desc, source, tool_id.replace(":", " ")])
            )
            entries[tool_id] = CatalogEntry(
                tool_id=tool_id,
                name=name,
                qualified=qualified,
                description=desc,
                source=source,
                hidden=hidden,
                schema_entry=item,
                search_text=search_text,
            )

        self._entries = entries
        self._rebuild_idf()

    def _rebuild_idf(self) -> None:
        df: Dict[str, int] = {}
        for entry in self._entries.values():
            seen = set(_tokenize(entry.search_text))
            for tok in seen:
                df[tok] = df.get(tok, 0) + 1
        self._doc_freq = df
        self._num_docs = max(len(self._entries), 1)

    def _score(self, query_tokens: Sequence[str], entry: CatalogEntry) -> float:
        if not query_tokens:
            return 0.0
        doc_tokens = _tokenize(entry.search_text)
        if not doc_tokens:
            return 0.0
        tf = {}
        for tok in doc_tokens:
            tf[tok] = tf.get(tok, 0) + 1
        doc_len = len(doc_tokens)
        score = 0.0
        for qt in query_tokens:
            if qt not in tf:
                continue
            freq = tf[qt]
            df = self._doc_freq.get(qt, 0)
            idf = math.log(1.0 + (self._num_docs - df + 0.5) / (df + 0.5))
            denom = freq + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * doc_len / _AVG_DOC_LEN)
            score += idf * (freq * (_BM25_K1 + 1.0)) / max(denom, 1e-9)
            if qt == entry.name.lower() or qt == entry.qualified.lower():
                score += 3.0
        return score

    def search(self, query: str, *, limit: int = _DEFAULT_LIMIT) -> List[SearchHit]:
        query = (query or "").strip()
        limit = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_LIMIT))
        q_tokens = _tokenize(query)
        hits: List[SearchHit] = []
        for entry in self._entries.values():
            score = self._score(q_tokens, entry)
            if score <= 0.0 and q_tokens:
                continue
            if not q_tokens:
                score = 0.0
            hits.append(SearchHit(entry=entry, score=score))
        hits.sort(key=lambda h: (-h.score, h.entry.tool_id))
        if not q_tokens:
            # Empty query: stable catalog slice ordered by tool_id.
            rows = sorted(self._entries.values(), key=lambda e: e.tool_id)
            return [SearchHit(entry=e, score=0.0) for e in rows[:limit]]
        return hits[:limit]

    def resolve_activation_keys(self, keys: Iterable[str]) -> List[str]:
        """Map user-supplied names/ids to canonical tool_ids."""
        resolved: List[str] = []
        by_name = {e.name: e.tool_id for e in self._entries.values()}
        by_qualified = {e.qualified: e.tool_id for e in self._entries.values()}
        for raw in keys:
            key = (raw or "").strip()
            if not key:
                continue
            if key in self._entries:
                resolved.append(key)
                continue
            if key in by_name:
                resolved.append(by_name[key])
                continue
            if key in by_qualified:
                resolved.append(by_qualified[key])
                continue
            # Accept mcp_server_tool without prefix.
            if key.startswith("mcp_") and key in by_name:
                resolved.append(by_name[key])
        # Stable dedupe
        out: List[str] = []
        seen: Set[str] = set()
        for tid in resolved:
            if tid not in seen:
                seen.add(tid)
                out.append(tid)
        return out

    def activate(self, keys: Iterable[str]) -> List[str]:
        activated = self.resolve_activation_keys(keys)
        self._activated.update(activated)
        return activated

    def is_visible(self, tool_id: str) -> bool:
        if not discovery_enabled():
            return True
        entry = self._entries.get(tool_id)
        if not entry:
            return False
        if not entry.hidden:
            return True
        return tool_id in self._activated

    def visible_schema(
        self,
        *,
        mcp_tools: Optional[Sequence] = None,
        no_delegation: Optional[bool] = None,
        browser_enabled: Optional[bool] = None,
    ) -> List[dict]:
        """Build the tool schema exposed to the pilot for this turn."""
        self.refresh(
            mcp_tools=mcp_tools,
            no_delegation=self._last_no_delegation if no_delegation is None else no_delegation,
            browser_enabled=self._last_browser_enabled if browser_enabled is None else browser_enabled,
        )
        if not discovery_enabled():
            return [e.schema_entry for e in sorted(self._entries.values(), key=lambda x: x.name)]

        visible = [
            e.schema_entry
            for e in sorted(self._entries.values(), key=lambda x: x.name)
            if self.is_visible(e.tool_id)
        ]
        # Safety: never return an empty schema.
        if not visible:
            return build_tools_schema(
                self._last_mcp_tools,
                no_delegation=self._last_no_delegation,
                browser_enabled=self._last_browser_enabled,
                include_search_tools=True,
            )
        return visible

    def format_search_response(
        self,
        query: str,
        *,
        limit: int = _DEFAULT_LIMIT,
        activate: Optional[Sequence[str]] = None,
    ) -> str:
        """Stable-size JSON payload for ``search_tools`` results."""
        newly = self.activate(activate or [])
        hits = self.search(query, limit=limit)
        rows = []
        for hit in hits:
            entry = hit.entry
            rows.append(
                {
                    **entry.to_search_row(activated=entry.tool_id in self._activated),
                    "score": round(hit.score, 4),
                }
            )
        payload = {
            "query": query or "",
            "count": len(rows),
            "activated": newly,
            "results": rows,
        }
        text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        # Hard cap for prompt stability regardless of catalog size.
        if len(text) > 8000:
            payload["results"] = rows[: max(1, limit // 2)]
            payload["truncated"] = True
            text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        return text

    def mcp_prompt_summary(self) -> str:
        """Compact MCP section for the system prompt when discovery is on."""
        mcp_entries = [e for e in self._entries.values() if e.source == "mcp"]
        if not mcp_entries:
            return ""
        active = [e.qualified for e in mcp_entries if e.tool_id in self._activated]
        active.sort()
        lines = [
            "## Connected MCP tools (on-demand discovery)",
            f"{len(mcp_entries)} MCP tool(s) connected. Use search_tools to find and activate hidden tools.",
            'Call MCP tools via native mcp_<server>_<tool> names or {"kind":"call_mcp","tool":"<server>.<tool>","arguments":{...}}.',
        ]
        if active:
            lines.append("Currently activated: " + ", ".join(active[:20]))
            if len(active) > 20:
                lines.append(f"(+{len(active) - 20} more activated)")
        return "\n".join(lines)
