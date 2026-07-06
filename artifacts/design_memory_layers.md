# Design: L0-L3 memory layering study (Marionette Wave 7)

> Status: STUDY + INSTRUMENTATION (v1). Read-only layer snapshots per turn;
> no automatic tier migration, no remote storage, no Puppetmaster changes.

## Context

Marionette already offloads verbose tool output (`harness/spill_registry.py`),
journals turn configuration (`harness/turn_context.py`), records eval outcomes
(`harness/eval_history.py`), and tracks compaction savings
(`harness/tool_output_savings.py`, `harness/history_compaction_journal.py`).
Wave 6 deferred a TencentDB-style **L0-L3 memory layering study** until spill
measurements existed. Wave 7 maps what is already on disk into four layers and
adds a cheap per-turn JSON snapshot for operators and future policy work.

## Layer map

### L0 — hot context (ephemeral)

**What:** Tokens (v1: character proxy) in the active model prompt for the
current turn — conversation history slice, pending actions, uncompacted tool
results still inline.

**Typical size drivers:** Long transcripts, large inline tool results before
spill/compaction, system prompt + rules + skills + tool schema overhead.

**Existing modules:** `harness/conversation.py` (`_history`,
`_estimate_context_tokens_for_list`, `get_context_usage`), `harness/context_budget.py`.

**Non-goals for v1:** Token-exact LLM counts; durable persistence across process
restart; automatic demotion of inline content to L1.

### L1 — session durable

**What:** State-dir artifacts tied to one harness session — turn-context journal,
spill index + spilled files, eval-history DB, savings ledgers, advisor warnings
journal (if any).

**Typical size drivers:** Spilled tool outputs under `pmharness-results/`,
`turn_context.jsonl` growth, eval and savings SQLite rows.

**Existing modules:** `harness/turn_context.py`, `harness/spill_registry.py`,
`harness/eval_history.py`, `harness/tool_output_savings.py`,
`harness/advisor.py` (in-memory warnings only today).

**Non-goals for v1:** Cross-session sharing; automatic spill retention sweeps
during measurement; replacing existing journals.

### L2 — workspace durable

**What:** Repo-scoped memory the harness reuses across sessions in the same
workspace — NL memory graph (`MemoryStore` / `memory.json`), Puppetmaster job
store when bridged, `.codegraph/` context cache (read-only reference; do not
duplicate indexing here).

**Typical size drivers:** Durable user facts, swarm/job SQLite store, codegraph
index size on large repos.

**Existing modules:** `harness/memory_store.py`, `harness/state.py`,
`puppetmaster.store_factory`, `.codegraph/` (via Puppetmaster codegraph seam).

**Non-goals for v1:** Indexing or mutating `.codegraph/`; syncing workspace memory
across machines; promoting L1 spills into L2 automatically.

### L3 — cold / compacted

**What:** Intentionally shrunk or archived context — history compaction journal
output, expired spills (when retention runs), stitched summaries from completed
swarms referenced by URI only.

**Typical size drivers:** Compaction journal rows (`history_compaction.sqlite`),
spill rows past `HARNESS_SPILL_RETENTION_DAYS` (counted but not swept during
measurement), swarm summary artifacts referenced by internal URI.

**Existing modules:** `harness/history_compaction_journal.py`,
`harness/spill_registry.py` (`sweep_expired_spills`), internal URI resolution for
swarm artifacts.

**Non-goals for v1:** Running retention sweeps during snapshot; reconstructing
full cold blobs into L0; automatic compaction policy (deferred to Wave 8).

## Measurement v1

One JSON snapshot per turn appended to `{state_dir}/memory_layers.jsonl`:

```json
{
  "session_id": "default",
  "turn": 3,
  "ts": 1717700000.0,
  "snapshot": {
    "L0": {"bytes": 42000, "entries": 12},
    "L1": {
      "bytes": 850000,
      "entries": 14,
      "components": {
        "turn_context_lines": 3,
        "spill_bytes": 800000,
        "spill_entries": 2,
        "eval_entries": 5,
        "savings_entries": 4
      }
    },
    "L2": {"bytes": 12000, "entries": 8, "components": {...}},
    "L3": {"bytes": 5000, "entries": 2, "components": {...}},
    "snapshot_at": "2026-07-06T12:00:00+00:00"
  }
}
```

**Rules:**

- **L0 bytes** = sum of character lengths in the active history window the
  session would send on the next `send()` (char proxy, not billed tokens).
- **L1/L2/L3 bytes** = best-effort file/row sizes from known state-dir paths;
  approximate and cheap, not byte-for-byte LLM context accounting.
- **Per-layer failures** return `{"bytes": 0, "entries": 0, "components": {}}`
  for that layer only; recording never blocks `send()`.
- Snapshots are **read-only observability** — no automatic promotion between
  layers.

## Promotion criteria (future waves, advisory only)

| From | To | Candidate signal (not implemented in v1) |
|------|----|------------------------------------------|
| L0 | L1 | Inline tool result exceeds turn budget; spill registered |
| L1 | L3 | Spill row older than retention; compaction journal entry |
| L0 | L3 | History compaction replaces message block with summary |
| L2 | L0 | MemoryStore facts injected into system/history for a turn |

Wave 8 may use L1/L3 metrics to **hint** history compaction (env-gated,
advisory only). Wave 9 targets release 0.8.0 after CI green and installer smoke.

## Integration points

| Layer | Hook |
|-------|------|
| Snapshot | `harness/memory_layers.py` — `snapshot_memory_layers`, `record_memory_layer_snapshot` |
| Turn | `ConversationalSession.send()` — after `record_turn_context` |
| API | `GET /api/usage` — `memory_layers` key with latest snapshot |
| UI | `CostBreakdown.tsx` — compact row when L1 bytes > 0 |

## Test strategy

- Unit: `tests/test_memory_layers.py` — L0 grows with history; L1 reflects
  spill; L2/L3 empty without error; snapshot shape.
- Integration: send records journal line; usage API includes `memory_layers`;
  missing journal degrades to `{}`.

## Non-goals (this wave)

- Puppetmaster repo changes.
- Automatic promotion/demotion between layers.
- Remote blob storage or cross-machine layer sync.
- Token-exact L0 measurement.
- Replacing spill offload or turn-context journals.
- YAML anywhere.
