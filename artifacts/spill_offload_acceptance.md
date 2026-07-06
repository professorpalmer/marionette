# Spill offload to spill:// URIs — acceptance notes

## Scope

Implements `artifacts/design_memory_offload.md`: oversized tool outputs that
`maybe_persist_result` spills to `{state_dir}/pmharness-results/` are now
indexed and addressable as `spill://{session_id}/{tool_call_id}` internal URIs.

## Behavior

- **Registry:** `harness/spill_registry.py` writes `{state_dir}/spill_index.sqlite`
  (open/write/close per call — Windows file-lock discipline). Re-spills of the
  same tool call replace the prior row. Registration failures are silent; the
  filesystem-path fallback in the persisted message always remains usable.
- **Message:** the `<persisted-output>` block now carries
  `Also addressable as: spill://...` in addition to the raw path, whenever the
  caller supplies a `spill_session_id` and both ids are URI-safe
  (`[A-Za-z0-9._-]`). No flag; the line is additive.
- **Resolution:** `spill://` joins the internal URI schemes in
  `harness/internal_uri.py`. `spill://` and `spill://{session}` list entries;
  `spill://{session}/{tool_call}` returns full content; `:N-M` line selectors
  work. Rows pointing outside `pmharness-results/` are rejected even if the
  index db is poisoned.
- **Search:** `search_state` covers the `spill` scheme; store-backed schemes
  are now built lazily so spill/conflict searches work without the
  Puppetmaster job store.
- **Wiring:** `ConversationalSession._append_action_result` and
  `enforce_turn_budget` (via `savings_session_id`) pass the session id through.

## Tests

```bash
python -m pytest tests/test_spill_registry.py tests/test_internal_uri.py tests/test_context_budget.py -q
```

Covers round-trip, replace-on-respill, unsafe-id rejection, URI emission,
full-content and line-sliced resolution, directory listings, poisoned-path
rejection, missing row/file errors, and store-free search.

## Non-goals honored

No remote storage, no mutation of spills, no cross-session dedupe, no TTL yet
(`HARNESS_SPILL_RETENTION_DAYS` deferred), no change to Puppetmaster artifacts.
