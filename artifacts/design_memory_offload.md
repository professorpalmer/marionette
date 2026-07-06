# Design: verbose-output offload to durable internal URIs

## Problem

Large tool outputs are already spilled by `maybe_persist_result` in
`harness/context_budget.py` (`spill_to_disk` → `{state_dir}/pmharness-results/{id}.txt`)
and summarized in chat with a filesystem path string. Agents must `read_file` that
path; the savings ledger (`harness/tool_output_savings.py`) counts compaction but
the spilled blob is **not** addressable through the internal URI surface
(`job://`, `artifact://`, `agent://`, `conflict://` in `harness/internal_uri.py`).
Tencent-style offload means: spill once, hand the model a stable **`artifact://`**
(or dedicated `spill://`) URI, searchable via `search_state`, auditable alongside
Puppetmaster artifacts.

## Proposed shape

**URI scheme (v1 extension):** `spill://session/{session_id}/{tool_call_id}` or
reuse `artifact://session/{session_id}/tool_output/{tool_call_id}` if we register
spilled outputs in SQLite alongside PM artifacts (preferred: single resolver).

**Retention:** files under `{state_dir}/pmharness-results/`; index row in
`{state_dir}/spill_index.sqlite` (session_id, tool_call_id, path, chars, ts,
content_hash). TTL optional via `HARNESS_SPILL_RETENTION_DAYS` (default: session lifetime).

**Agent-visible message** (replaces raw path in `build_persisted_message`):

```
[Output persisted: 842,000 chars → artifact://session/default/tool_output/call_abc]
Preview: ...
Use read_file on the URI or search_state to locate related spills.
```

**Dedupe:** existing `dedupe=True` content-hash suffix in `spill_to_disk` maps to
one index row; URI stays stable per `(session_id, tool_call_id)`.

## Integration points

| Layer | Hook |
|-------|------|
| Spill | `context_budget.spill_to_disk` — after write, call `register_spill(...)` (new module `harness/spill_registry.py`). |
| Message | `build_persisted_message` — emit internal URI instead of bare filesystem path when `HARNESS_SPILL_URI=1`. |
| Resolve | `harness/internal_uri.py` — add `artifact` subpath or `spill` scheme handler reading index + file; wire into `resolve_internal_uri` / `search_internal_uris`. |
| Tool dispatch | `harness/tool_dispatch.py` `_do_read_file` — already routes `is_internal_uri`; no new tool required. |
| Savings ledger | `tool_output_savings.try_record` — optional `spill_uri` column for UI drill-down (Task D pattern). |
| Session API | `GET /api/context/usage` — count of active spills + bytes offloaded (mirror compaction fields). |

## Test strategy

- Unit: `tests/test_spill_registry.py` — register, resolve URI, dedupe, missing URI error.
- URI: extend `tests/test_internal_uri.py` — read spilled content via `artifact://.../tool_output/...`.
- Integration: force large tool result in conversation test; assert history contains URI not absolute path; `read_file` on URI returns full content.
- Windows: index uses open/write/close per call (same lesson as `history_compaction_journal.py`).

## Non-goals (this tranche)

- Uploading spills to remote blob storage.
- Mutating spilled content (read-only refs).
- Cross-session spill sharing or global dedupe across repos.
- Replacing Puppetmaster durable artifacts for worker outputs (only harness tool results).
- Automatic summarization of spill contents into wiki/memory graph.
