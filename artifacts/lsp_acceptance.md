# LSP code intelligence — acceptance notes

## Summary

First-pass local LSP tooling for Marionette pilots: status, diagnostics, and a
modest references mode. Uses pyright (Python) and tsc (TypeScript) CLI tools when
present; degrades gracefully when absent. References mode prefers CodeGraph and
falls back to a word-boundary text scan.

## Behavior

- **lsp / mode=status**: reports which local tools are on PATH.
- **lsp / mode=diagnostics** (default): runs pyright `--outputjson` and/or
  `tsc --noEmit`, parses output into a compact text report.
- **lsp / mode=references**: requires `symbol`. Tries CodeGraph query first;
  on failure falls back to repository text scan with word-boundary matching.
- **Tool discovery**: with `HARNESS_TOOL_DISCOVERY=1`, `lsp` is hidden until
  activated via `search_tools` (same as round-1 OMP behavior).

## Files touched

- `harness/lsp_code_intelligence.py` — tool discovery, diagnostics parsers,
  references hybrid (CodeGraph + text scan)
- `harness/tool_dispatch.py` — `_do_lsp` dispatcher
- `harness/pilot.py` — `lsp` tool schema (status, diagnostics, references)
- `harness/conversation.py` — action-loop branch
- `tests/test_lsp_code_intelligence.py` — focused tests

## Verification

```bash
python -m pytest tests/test_lsp_code_intelligence.py -q
```

Expected: diagnostics parsers, timeout handling, references text scan,
graceful empty results, and schema coverage for the `symbol` parameter.

## Explicitly deferred

- Rename / refactor operations (edit-producing; needs checkpoint integration)
- Long-lived LSP server processes (pyright-langserver, tsserver sessions)
- References via true LSP `textDocument/references` protocol
- Languages beyond Python and TypeScript diagnostics CLI surfaces
