# Tool discovery acceptance notes

Date: 2026-07-06

## Feature

OMP-inspired on-demand tool discovery for Marionette/Puppetmaster:

- Core pilot tools remain in the model prompt by default.
- Hidden tools (MCP, browser, web, delegation) are indexed in a session-scoped `ToolCatalog`.
- The pilot calls `search_tools` to rank matches (stdlib BM25-ish) and optionally `activate` hidden tools for later turns.
- MCP `call_mcp` still works without pre-activation; successful calls auto-activate the tool for native schema exposure on subsequent turns.
- Set `HARNESS_TOOL_DISCOVERY=0` to restore the legacy "all tools in prompt" behavior.

## Files touched

- `harness/tool_discovery.py` — catalog, ranking, activation, prompt summary
- `harness/pilot.py` — `search_tools` schema + action kind
- `harness/tool_dispatch.py` — `_do_search_tools` handler
- `harness/conversation.py` — catalog wiring, visible schema assembly, MCP prompt compaction
- `tests/test_tool_discovery.py` — targeted coverage

## Verification

```bash
python -m pytest tests/test_tool_discovery.py tests/test_mcp_discovery.py tests/test_native_tools.py tests/test_worker_leaf.py -q
```

## Acceptance checklist

- [x] BM25-ish deterministic ranking over built-in + MCP descriptions
- [x] Core vs hidden tool behavior with activation
- [x] Windows path-safe MCP metadata (backslashes normalized in catalog text)
- [x] Stable bounded JSON output from `search_tools`
- [x] Existing MCP `call_mcp` path preserved (with auto-activate)
- [x] Cross-platform stdlib-only implementation
- [x] No release/push performed
